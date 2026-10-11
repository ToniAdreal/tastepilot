"""Browser-supplied plan attachments (POST /api/plan imports/timetables):
the hosted demo offers the CLI's taste-file and timetable-file import
paths with no upload store — the page reads file contents in the
browser, the server parses them with the same byte parsers for this
plan only, and nothing is persisted. Malformed attachments are
categorised 400s that never echo the submitted content, and the plain
16 KiB body cap still applies to any body without attachments."""
import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.server import (MAX_BODY_BYTES, MAX_PLAN_ATTACHMENTS,
                               MAX_PLAN_BODY_BYTES, SECURITY_HEADERS,
                               make_handler, parse_plan_attachments)

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"

WATCHLIST_CSV = "Name,Year\nSpirited Away,2001\nA Quiet Imported Film,2020\n"
SERVICES_JSON = json.dumps([{
    "from": "Kyoto", "to": "Paris", "duration_hours": 11,
    "mode": "flight", "service": "Demo Air 101"}])
SENTINEL = "SENTINEL-CONTENT-9f3b"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post_json(url, obj):
    return _request(url, method="POST",
                    body=json.dumps(obj).encode(),
                    headers={"Content-Type": "application/json"})


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# --- the honest paths -------------------------------------------------------

def test_plan_with_import_content(server_url):
    status, headers, raw = _post_json(server_url + "/api/plan", {
        "text": "a slow weekend in Kyoto",
        "imports": [{"filename": "watchlist.csv", "content": WATCHLIST_CSV}]})
    assert status == 200
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value
    assert headers.get("Cache-Control") == "no-store"
    plan = json.loads(raw)
    assert plan["imports"] == [{
        "source_file": "watchlist.csv", "format": "letterboxd_csv",
        "items": plan["imports"][0]["items"]}]
    assert len(plan["imports"][0]["items"]) == 2
    assert plan["checker"]["passed"] is True
    assert plan["checker"]["chain_valid"] is True
    assert plan["items"]  # imported seeds produced a real plan


def test_plan_with_timetable_content_flips_transfer(server_url):
    req = "a trip: Kyoto Nov 3-5, then Paris Nov 5, I love Amelie"
    status, _, raw = _post_json(server_url + "/api/plan", {
        "text": req,
        "timetables": [{"filename": "services.json",
                        "content": SERVICES_JSON}]})
    assert status == 200
    plan = json.loads(raw)
    assert plan["timetable"][0]["format"] == "timetable_json"
    assert plan["timetable"][0]["entries"] == 1
    transfer = plan["transfers"][0]
    assert transfer["source"] == "timetable_file"
    assert transfer["status"] == "tight"  # 11h scheduled vs 15.3h estimate
    assert plan["checker"]["passed"] is True


def test_plan_without_attachments_unchanged(server_url):
    status, _, raw = _post_json(server_url + "/api/plan",
                                {"text": "a slow weekend in Kyoto"})
    assert status == 200
    plan = json.loads(raw)
    assert plan["imports"] == [] and plan["timetable"] == []


def test_helper_parses_and_reports_verbatim_seed_names():
    imported, timetables = parse_plan_attachments({
        "imports": [{"filename": "watchlist.csv",
                     "content": WATCHLIST_CSV}]})
    assert timetables == []
    assert imported[0]["seed_names"] == ["Spirited Away",
                                         "A Quiet Imported Film"]
    assert imported[0]["source_file"] == "watchlist.csv"
    assert parse_plan_attachments({}) == ([], [])


# --- hostile / malformed attachments ---------------------------------------

def test_bad_import_is_categorised_400_without_content_echo(server_url):
    status, _, raw = _post_json(server_url + "/api/plan", {
        "text": "a slow weekend in Kyoto",
        "imports": [{"filename": "broken.json",
                     "content": "{not json " + SENTINEL}]})
    assert status == 400
    body = raw.decode()
    assert "import failed" in body and "bad_json" in body
    assert SENTINEL not in body
    assert "Traceback" not in body


def test_bad_timetable_is_categorised_400(server_url):
    status, _, raw = _post_json(server_url + "/api/plan", {
        "text": "a slow weekend in Kyoto",
        "timetables": [{"filename": "services.txt",
                         "content": "Kyoto to Paris somehow"}]})
    assert status == 400
    assert "timetable failed" in raw.decode()


def test_attachment_shape_gates(server_url):
    base = {"text": "a slow weekend in Kyoto"}
    status, _, _ = _post_json(server_url + "/api/plan",
                              {**base, "imports": "watchlist.csv"})
    assert status == 400
    status, _, raw = _post_json(server_url + "/api/plan", {
        **base, "imports": [{"filename": "watchlist.csv"}]})
    assert status == 400 and "bad_attachment" in raw.decode()
    status, _, raw = _post_json(server_url + "/api/plan", {
        **base, "imports": [{"filename": "w.csv", "content": WATCHLIST_CSV}
                            ] * (MAX_PLAN_ATTACHMENTS + 1)})
    assert status == 400 and "too_many" in raw.decode()


def test_declared_oversize_plan_body_rejected_413(server_url):
    parts = urllib.request.urlparse(server_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port,
                                      timeout=10)
    conn.putrequest("POST", "/api/plan")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(MAX_PLAN_BODY_BYTES + 1))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    assert json.loads(resp.read())["error"] == "request body too large"
    conn.close()


def test_plain_oversize_body_still_capped_at_plain_limit(server_url):
    body = {"text": "kyoto " + "x" * MAX_BODY_BYTES}
    raw_body = json.dumps(body).encode()
    assert len(raw_body) > MAX_BODY_BYTES
    status, _, raw = _request(server_url + "/api/plan", method="POST",
                              body=raw_body,
                              headers={"Content-Type": "application/json"})
    assert status in (400, 413)  # rejected, never planned
    if status == 413:
        assert json.loads(raw)["error"] in (
            "request body too large", "text too long")


def test_large_attachment_body_within_plan_cap_accepted(server_url):
    lines = "\n".join(
        f"Imported Title Number {i} With A Longer Descriptive Name"
        for i in range(400))
    assert len(lines) > MAX_BODY_BYTES  # beyond the plain cap...
    status, _, raw = _post_json(server_url + "/api/plan", {
        "text": "a slow weekend in Kyoto",
        "imports": [{"filename": "big.txt", "content": lines}]})
    assert status == 200  # ...but inside the attachment plan cap
    plan = json.loads(raw)
    assert plan["imports"][0]["format"] == "text_list"
    assert len(plan["imports"][0]["items"]) == 200  # parser item cap


# --- UI + docs wiring --------------------------------------------------------

def test_ui_carries_attachment_wiring():
    page = INDEX.read_text(encoding="utf-8")
    assert 'id="impfiles"' in page and 'id="ttfiles"' in page
    assert "readAttachments" in page
    assert "body.imports = imports" in page
    assert "body.timetables = timetables" in page
    assert "never stored" in page


def test_docs_describe_browser_attachments():
    readme = (APP_ROOT / "README.md").read_text(encoding="utf-8")
    submission = (APP_ROOT / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "browser" in readme.lower() and "attach" in readme.lower()
    assert "attach" in submission.lower()
