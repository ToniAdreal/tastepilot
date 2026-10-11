"""Live calendar subscription feeds for branch timelines
(SUBMISSION "What's next", built in generic form).

Doctrine under test: a feed is a stable URL whose content is
re-rendered from the stored timeline on every fetch — updating the
feed (a branch re-sealed after another swap) changes what the same
URL serves, and every fetch recomputes verdicts, so a tampered
branch's series reads NOT VERIFIED, never PASS. A dateless timeline
cannot become a feed (no_dated_legs, the ics gate). Feeds expire,
can be revoked, can be owner-managed (only the owner's token may
update or revoke), and persist across restarts in file mode with
only the token's hash ever written to disk.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.server import (DEFAULT_FEED_TTL_SECONDS, TimelineFeedStore,
                               make_handler)
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"
TOKEN = "feed-owner-token-0123456789"
WRONG = "feed-other-token-9876543210"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _payload(text=DATED):
    base = TastePilotAgent().run(text)
    branches = [{"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]
    return {"base": base, "branches": branches}


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _post(url, obj, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, method="POST",
                    body=json.dumps(obj).encode(), headers=headers)


# -- the store -----------------------------------------------------------


def test_feed_store_create_get_update_revoke():
    clock = FakeClock()
    store = TimelineFeedStore(ttl_seconds=100, clock=clock)
    payload = _payload()
    meta = store.create(payload)
    feed_id = meta["feed_id"]
    assert len(feed_id) == 32 and meta["columns"] == 2
    assert meta["branches"] == ["Museum"] and meta["owned"] is False
    assert store.get(feed_id) == payload
    # Ids are stable per feed and unique per creation (not content ids).
    other = store.create(payload)
    assert other["feed_id"] != feed_id
    # Deep-copy isolation: mutating the fetched copy changes nothing.
    fetched = store.get(feed_id)
    fetched["base"]["items"] = []
    assert store.get(feed_id) == payload
    # Update replaces in place, renews expiry, keeps created_at.
    clock.advance(50)
    newer = _payload()
    status, meta2 = store.update_status(feed_id, newer)
    assert status == "updated" and meta2["feed_id"] == feed_id
    assert meta2["created_at"] == meta["created_at"]
    assert meta2["updated_at"] != meta["updated_at"]
    assert meta2["expires_at"] > meta["expires_at"]
    assert store.get(feed_id) == newer
    assert store.update_status("missing", newer)[0] == "not_found"
    assert store.revoke(feed_id) is True
    assert store.get(feed_id) is None
    assert store.revoke(feed_id) is False


def test_feed_store_expiry_and_bounds():
    clock = FakeClock()
    store = TimelineFeedStore(max_feeds=2, ttl_seconds=100, clock=clock)
    first = store.create(_payload())["feed_id"]
    clock.advance(50)
    second = store.create(_payload())["feed_id"]  # expires at 150
    clock.advance(25)
    third = store.create(_payload())["feed_id"]  # expires at 175,
    # and evicts the oldest (first) from the bounded store
    assert store.get(first) is None
    assert store.get(second) is not None
    clock.advance(80)  # t=155: second is past its TTL, third is not
    assert store.get(second) is None
    assert store.get(third) is not None
    assert store.info()["count"] == 1


def test_feed_store_ownership_rules():
    store = TimelineFeedStore()
    meta = store.create(_payload(), owner_token=TOKEN)
    feed_id = meta["feed_id"]
    assert meta["owned"] is True
    assert meta["owner_fingerprint"] is not None
    assert TOKEN not in json.dumps(meta)
    assert store.update_status(feed_id, _payload())[0] == "forbidden"
    assert store.update_status(feed_id, _payload(),
                               owner_token=WRONG)[0] == "forbidden"
    assert store.update_status(feed_id, _payload(),
                               owner_token=TOKEN)[0] == "updated"
    assert store.revoke_status(feed_id) == "forbidden"
    assert store.revoke_status(feed_id, owner_token=TOKEN) == "revoked"


def test_feed_store_persistence_round_trip(tmp_path):
    path = tmp_path / "feeds.json"
    clock = FakeClock()
    store = TimelineFeedStore(path=path, clock=clock)
    meta = store.create(_payload(), owner_token=TOKEN)
    feed_id = meta["feed_id"]
    raw = path.read_text(encoding="utf-8")
    assert TOKEN not in raw  # only the token's hash is ever persisted
    revived = TimelineFeedStore(path=path, clock=clock)
    assert revived.get(feed_id) is not None
    assert revived.meta(feed_id)["owned"] is True
    # A persisted revocation stays revoked after another restart.
    assert revived.revoke(feed_id, owner_token=TOKEN) is True
    assert TimelineFeedStore(path=path, clock=clock).get(feed_id) is None
    # A corrupt feed file starts an empty store instead of crashing.
    path.write_text("{not json", encoding="utf-8")
    assert TimelineFeedStore(path=path, clock=clock).info()["count"] == 0
    with pytest.raises(ValueError):
        TimelineFeedStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        TimelineFeedStore(max_feeds=0)


# -- the HTTP surface ------------------------------------------------------


def test_feed_http_create_fetch_update_revoke():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/feeds", _payload())
        assert status == 201
        created = json.loads(body)
        assert created["all_passed"] is True
        url = base_url + created["url"]
        assert url.endswith(".ics")
        status, headers, body = _request(url)
        assert status == 200
        assert "text/calendar" in headers.get("Content-Type", "")
        # A subscription must be re-pollable, not no-store.
        assert headers.get("Cache-Control") == "public, max-age=300"
        text = body.decode().replace("\r\n ", "")
        assert text.count("BEGIN:VEVENT") == 4
        assert "TastePilot [Museum]: Kyoto" in text
        assert "NOT VERIFIED" not in text
        # Health names the feed store.
        status, _h, body = _request(f"{base_url}/api/health")
        health = json.loads(body)
        assert health["feeds"]["count"] == 1
        assert health["feeds"]["ttl_seconds"] == DEFAULT_FEED_TTL_SECONDS
        # Update in place: a different dated timeline, same URL —
        # the served series now names the new branch only.
        base2 = TastePilotAgent().run(
            "a slow weekend in Kyoto Nov 3-5, "
            "I love Spirited Away and Kind of Blue")
        payload2 = {"base": base2, "branches": [
            {"name": "Museum", "plan": swap_plan(base2, PRIMARY, ALT)}]}
        feed_id = created["feed_id"]
        status, _h, body = _post(f"{base_url}/api/feeds/{feed_id}",
                                 payload2)
        assert status == 200 and json.loads(body)["updated"] is True
        status, _h, body = _request(url)
        assert body.decode().count("BEGIN:VEVENT") == 2  # one leg now
        # Meta recomputes the verdict.
        status, _h, body = _request(f"{base_url}/api/feeds/{feed_id}")
        assert status == 200 and json.loads(body)["all_passed"] is True
        # Revoke: the URL 404s from then on, for everyone.
        feed_url = f"{base_url}/api/feeds/{feed_id}"
        status, _h, _b = _request(feed_url, method="DELETE")
        assert status == 200
        status, _h, _b = _request(url)
        assert status == 404
        status, _h, _b = _request(feed_url, method="DELETE")
        assert status == 404
    finally:
        server.shutdown()


def test_feed_http_tampered_branch_not_verified():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        payload = _payload()
        payload["branches"][0]["plan"]["items"][0]["affinity"] = 0.01
        status, _h, body = _post(f"{base_url}/api/feeds", payload)
        assert status == 201  # tampering is a verdict, not a refusal
        created = json.loads(body)
        assert created["all_passed"] is False
        status, _h, body = _request(base_url + created["url"])
        assert status == 200
        text = body.decode().replace("\r\n ", "")
        assert "NOT VERIFIED" in text and "Branch: Museum (FAIL" in text
        assert "Branch: As planned (PASS" in text
    finally:
        server.shutdown()


def test_feed_http_gates():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        # Dateless timeline -> the ics gate refuses categorically.
        plain = TastePilotAgent().run(
            "a slow weekend in Kyoto, I love Spirited Away")
        payload = {"base": plain, "branches": [
            {"name": "Museum", "plan": swap_plan(plain, PRIMARY, ALT)}]}
        status, _h, body = _post(f"{base_url}/api/feeds", payload)
        assert status == 400 and "no_dated_legs" in body.decode()
        # Malformed shapes -> 400, never a traceback.
        for bad in ({"base": {}}, {"branches": []}, {"base": {}, "branches": {}},
                    ["not", "a", "dict"]):
            status, _h, _b = _post(f"{base_url}/api/feeds", bad)
            assert status == 400
        status, _h, body = _request(f"{base_url}/api/feeds",
                                    method="POST", body=b"{oops",
                                    headers={"Content-Type":
                                             "application/json"})
        assert status == 400 and "invalid JSON" in body.decode()
        # Unknown ids -> 404 on every route.
        status, _h, _b = _request(f"{base_url}/api/feeds/nope.ics")
        assert status == 404
        status, _h, _b = _post(f"{base_url}/api/feeds/nope", _payload())
        assert status == 404
        # DELETE anywhere else stays 405 (hardening contract intact).
        status, _h, _b = _request(f"{base_url}/api/plan", method="DELETE")
        assert status == 405
    finally:
        server.shutdown()


def test_feed_http_oversize_declared_length_413():
    import http.client
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        port = int(base_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # Declare an oversize body but send almost nothing: the
        # server answers from the declared length alone.
        conn.putrequest("POST", "/api/feeds")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(600_000))
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        assert resp.status == 413
        resp.read()
        conn.close()
    finally:
        server.shutdown()


def test_feed_http_owner_flow():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/feeds", _payload(),
                                 token=TOKEN)
        assert status == 201
        created = json.loads(body)
        assert created["owned"] is True
        feed_id = created["feed_id"]
        url = base_url + created["url"]
        # Update without / with the wrong token -> 403, feed unchanged.
        for bad_token in (None, WRONG):
            status, _h, body = _post(f"{base_url}/api/feeds/{feed_id}",
                                     _payload(), token=bad_token)
            assert status == 403
        status, _h, body = _post(f"{base_url}/api/feeds/{feed_id}",
                                 _payload(), token=TOKEN)
        assert status == 200
        # A malformed owner token on create -> 400.
        status, _h, _b = _post(f"{base_url}/api/feeds", _payload(),
                               token="short")
        assert status == 400
        # Revoke requires the owner too; the link stays live meanwhile.
        feed_url = f"{base_url}/api/feeds/{feed_id}"
        status, _h, _b = _request(feed_url, method="DELETE")
        assert status == 403
        status, _h, _b = _request(url)
        assert status == 200
        status, _h, _b = _request(
            feed_url, method="DELETE", headers={"X-Owner-Token": TOKEN})
        assert status == 200
        status, _h, _b = _request(url)
        assert status == 404
    finally:
        server.shutdown()


def test_feed_survives_server_restart(tmp_path):
    feed_file = tmp_path / "feeds.json"
    payload = _payload()
    server, base_url = _serve(make_handler(
        TastePilotAgent(), feed_file=feed_file))
    try:
        status, _h, body = _post(f"{base_url}/api/feeds", payload)
        assert status == 201
        url_path = json.loads(body)["url"]
    finally:
        server.shutdown()
    server, base_url = _serve(make_handler(
        TastePilotAgent(), feed_file=feed_file))
    try:
        status, headers, body = _request(base_url + url_path)
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 4
    finally:
        server.shutdown()


def test_feed_cli_serve_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    assert "--feed-file" in text and "--feed-ttl-days" in text


def test_feed_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Subscribe to timeline feed (iCalendar)" in html
    assert "/api/feeds" in html and "Update timeline feed" in html
    readme = README.read_text(encoding="utf-8")
    assert "calendar subscription" in readme
    assert "TASTEPILOT_FEED_FILE" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "live calendar subscription" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "TASTEPILOT_FEED_FILE" in deploy
    assert "Feed probe" in deploy
