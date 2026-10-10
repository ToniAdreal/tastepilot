"""Shareable verification links (POST /api/share, GET /api/share/<id>,
GET /api/share/<id>/verify): a plan's audit travels with the plan.

Storing a plan grants it no trust — the id is content-addressed, every
fetch-by-link verification is recomputed from the sealed record (a
tampered stored plan still FAILs), the store is bounded, and the share
endpoints hold untrusted input to the same caps/shape gate as verify.
"""
import copy
import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.server import (MAX_SHARE_BODY_BYTES, SECURITY_HEADERS,
                               make_handler, share_id_for,
                               validate_plan_payload, verification_report)

APP_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = APP_ROOT / "samples" / "plan_weekend_kyoto.json"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post_raw(url, raw: bytes):
    return _request(url, method="POST", body=raw,
                    headers={"Content-Type": "application/json"})


def _post_json(url, obj):
    return _post_raw(url, json.dumps(obj).encode())


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def server_url():
    server, url = _serve(make_handler(TastePilotAgent()))
    yield url
    server.shutdown()


@pytest.fixture()
def sample_plan():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# --- the honest path --------------------------------------------------------

def test_share_round_trip_and_verify_by_link(server_url, sample_plan):
    status, headers, raw = _post_json(server_url + "/api/share", sample_plan)
    assert status == 201
    assert headers.get("Cache-Control") == "no-store"
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value
    created = json.loads(raw)
    share_id = created["share_id"]
    assert share_id == share_id_for(sample_plan)
    assert created["url"] == f"/?share={share_id}"
    assert created["verify_url"] == f"/api/share/{share_id}/verify"

    status, headers, raw = _request(server_url + f"/api/share/{share_id}")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"
    assert json.loads(raw) == sample_plan

    status, _, raw = _request(server_url + created["verify_url"])
    assert status == 200
    data = json.loads(raw)
    assert data["share_id"] == share_id
    assert data["passed"] is True and data["chain_valid"] is True
    assert data["item_count"] == len(sample_plan["items"]) > 0
    assert all(r["admissible"] and r["reason"] == "ok"
               for r in data["items"])
    # Same recomputation the paste-it-yourself endpoint performs.
    assert data["violations"] == verification_report(sample_plan)["violations"]


def test_share_id_is_content_addressed_and_idempotent(server_url,
                                                      sample_plan):
    _, _, raw1 = _post_json(server_url + "/api/share", sample_plan)
    _, _, raw2 = _post_json(server_url + "/api/share",
                            copy.deepcopy(sample_plan))
    assert json.loads(raw1)["share_id"] == json.loads(raw2)["share_id"]
    other = copy.deepcopy(sample_plan)
    other["items"][0]["why"] = "a different explanation"
    _, _, raw3 = _post_json(server_url + "/api/share", other)
    assert json.loads(raw3)["share_id"] != json.loads(raw1)["share_id"]


def test_fresh_server_plan_can_be_shared(server_url):
    status, _, raw = _post_json(server_url + "/api/plan",
                                {"text": "a first date in NYC, "
                                         "we love Kind of Blue"})
    assert status == 200
    plan = json.loads(raw)
    status, _, raw = _post_json(server_url + "/api/share", plan)
    assert status == 201
    share_id = json.loads(raw)["share_id"]
    status, _, raw = _request(server_url + f"/api/share/{share_id}/verify")
    data = json.loads(raw)
    assert data["passed"] is True and data["item_count"] == len(plan["items"])


# --- verification by link is recomputed, never trusted ----------------------

def test_tampered_shared_plan_still_fails_by_link(server_url, sample_plan):
    plan = copy.deepcopy(sample_plan)
    plan["items"][0]["affinity"] += 0.01
    assert plan["evidence"]["verdict"] == "PASS"  # the claim travels too
    status, _, raw = _post_json(server_url + "/api/share", plan)
    assert status == 201
    share_id = json.loads(raw)["share_id"]
    status, _, raw = _request(server_url + f"/api/share/{share_id}/verify")
    assert status == 200
    data = json.loads(raw)
    assert data["passed"] is False and data["chain_valid"] is True
    assert {"item": data["items"][0]["item"],
            "reason": "affinity_mismatch"} in data["violations"]
    assert data["record_verdict"] == "PASS"  # echoed as a claim only


def test_unknown_share_id_404s(server_url):
    for suffix in ("", "/verify"):
        status, _, raw = _request(
            server_url + "/api/share/" + "0" * 32 + suffix)
        assert status == 404
        assert json.loads(raw)["error"] == "unknown share id"


# --- untrusted-input handling: same caps and shape gate as verify -----------

def test_share_malformed_payloads_are_400(server_url):
    status, _, raw = _post_raw(server_url + "/api/share", b"{not json")
    assert status == 400
    assert json.loads(raw)["error"] == "invalid JSON body"
    status, _, _ = _post_json(server_url + "/api/share", [1, 2, 3])
    assert status == 400
    status, _, raw = _post_json(server_url + "/api/share", {"items": []})
    assert status == 400
    assert "evidence" in json.loads(raw)["error"]
    status, _, _ = _post_json(server_url + "/api/share",
                              {"evidence": {"entries": []}, "items": "nope"})
    assert status == 400


def test_share_oversize_body_rejected_413(server_url):
    # Declared-length rejection, exactly like /api/verify: declare the
    # oversize length and send almost nothing.
    host = server_url.removeprefix("http://")
    hostname, port = host.rsplit(":", 1)
    conn = http.client.HTTPConnection(hostname, int(port), timeout=10)
    try:
        conn.putrequest("POST", "/api/share")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_SHARE_BODY_BYTES + 1))
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        raw = resp.read()
        assert resp.status == 413
        assert json.loads(raw)["error"] == "plan too large"
    finally:
        conn.close()


def test_share_store_is_bounded_oldest_evicted(sample_plan):
    server, url = _serve(make_handler(TastePilotAgent(), max_shared_plans=2))
    try:
        ids = []
        for n in range(3):
            plan = copy.deepcopy(sample_plan)
            plan["items"][0]["why"] = f"variant {n}"
            status, _, raw = _post_json(url + "/api/share", plan)
            assert status == 201
            ids.append(json.loads(raw)["share_id"])
        status, _, _ = _request(url + f"/api/share/{ids[0]}")
        assert status == 404  # evicted
        for share_id in ids[1:]:
            status, _, _ = _request(url + f"/api/share/{share_id}")
            assert status == 200
    finally:
        server.shutdown()


# --- helpers and UI wiring ----------------------------------------------------

def test_validate_and_report_helpers(sample_plan):
    assert validate_plan_payload(sample_plan) is None
    assert validate_plan_payload([1]) == "plan must be a JSON object"
    assert validate_plan_payload({"items": []}) == \
        "evidence record missing or malformed"
    report = verification_report(sample_plan)
    assert report["passed"] is True
    assert report["record_verdict"] == sample_plan["evidence"]["verdict"]


def test_ui_page_carries_share_wiring(server_url):
    status, _, raw = _request(server_url + "/")
    assert status == 200
    page = raw.decode()
    assert 'id="sharebtn"' in page and 'id="sbtn"' in page
    assert "/api/share" in page and "share" in page
    assert "URLSearchParams" in page  # opening /?share=<id> auto-verifies
