"""Multi-timeline calendar subscriptions with change notification
(SUBMISSION "What's next", built in local/generic form).

Doctrine under test: one stable subscription URL carries MANY
timelines at once — its calendar is re-rendered from the stored
bundle on every fetch, with every timeline's verdicts recomputed,
so a tampered branch's series reads NOT VERIFIED. Change
notification is poll-based and explicit: the feed is versioned,
keeps a capped change log (creation + every in-place update, each
with its timeline names and write-time verdict), and serves an
ETag — a poller presenting the current etag in If-None-Match gets
a cheap 304, and an update changes the etag, so the next poll
learns "changed" and downloads the new series. No subscriber
registry is kept and nothing is pushed to anyone. Bundles expire,
can be revoked, can be owner-managed, and persist across
restarts in file mode with only the token's hash on disk.
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
from tastepilot.export import (export_multi_timeline_ics,
                               validate_multi_timelines)
from tastepilot.server import (DEFAULT_FEED_TTL_SECONDS,
                               MultiTimelineFeedStore, make_handler,
                               multi_feed_etag)
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
DATED_ONE = ("a slow weekend in Kyoto Nov 3-5, "
             "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"
TOKEN = "multi-owner-token-0123456789"
WRONG = "multi-other-token-9876543210"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _timeline(name, text=DATED):
    base = TastePilotAgent().run(text)
    return {"name": name, "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _payload():
    return {"timelines": [_timeline("Japan-France"),
                          _timeline("Kyoto only", DATED_ONE)]}


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


# -- the bundle renderer ---------------------------------------------------


def test_validate_multi_timelines_shape_gate():
    validate_multi_timelines(_payload()["timelines"])  # no raise
    for bad, category in (
            (None, "bad_timelines"), ([], "bad_timelines"),
            ("x", "bad_timelines"),
            ([{"name": "A"}] * 7, "too_many_timelines"),
            ([{"name": "A", "base": {}, "branches": []},
              {"name": "A", "base": {}, "branches": []}],
             "duplicate_timeline_name"),
            ([{"name": "", "base": {}, "branches": []}],
             "bad_timelines"),
            ([{"name": "A", "base": [], "branches": []}],
             "bad_timelines")):
        with pytest.raises(ValueError, match=category):
            validate_multi_timelines(bad)


def test_multi_ics_one_calendar_many_timelines():
    content, all_passed, summaries = export_multi_timeline_ics(
        _payload()["timelines"])
    assert all_passed is True
    # 2 timelines: (2 columns x 2 legs) + (2 columns x 1 leg) = 6.
    assert content.count("BEGIN:VEVENT") == 6
    assert content.count("BEGIN:VCALENDAR") == 1
    assert "TastePilot [Japan-France / Museum]: Kyoto" in content
    assert "TastePilot [Kyoto only / As planned]: Kyoto" in content
    assert [s["name"] for s in summaries] == ["Japan-France",
                                              "Kyoto only"]
    assert all(s["all_passed"] for s in summaries)
    # Byte-deterministic, and UIDs unique across timelines.
    again, _, _ = export_multi_timeline_ics(_payload()["timelines"])
    uids = [l for l in content.split("\r\n") if l.startswith("UID:")]
    assert len(uids) == len(set(uids)) == 6
    assert "NOT VERIFIED" not in content


def test_multi_ics_tampered_and_dateless():
    timelines = _payload()["timelines"]
    timelines[0]["branches"][0]["plan"]["items"][0]["affinity"] = 0.01
    content, all_passed, summaries = export_multi_timeline_ics(timelines)
    assert all_passed is False and summaries[0]["all_passed"] is False
    assert summaries[1]["all_passed"] is True
    assert "NOT VERIFIED" in content and "Branch: Museum (FAIL" in content
    plain_base = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away")
    dateless = [{"name": "No dates", "base": plain_base, "branches": [
        {"name": "Museum",
         "plan": swap_plan(plain_base, PRIMARY, ALT)}]}]
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_multi_timeline_ics(dateless)


# -- the store ---------------------------------------------------------------


def test_multi_store_create_update_changes_revoke():
    clock = FakeClock()
    store = MultiTimelineFeedStore(ttl_seconds=100, clock=clock)
    payload = _payload()
    meta = store.create(payload, all_passed=True)
    feed_id = meta["feed_id"]
    assert meta["version"] == 1 and meta["timelines"] == [
        "Japan-France", "Kyoto only"]
    assert meta["etag"] == multi_feed_etag(payload, 1)
    assert store.get(feed_id) == payload
    # Ids are unique per creation, never content-addressed.
    assert store.create(payload)["feed_id"] != feed_id
    log = store.changes(feed_id)
    assert log["version"] == 1 and len(log["changes"]) == 1
    assert log["changes"][0]["kind"] == "created"
    assert log["changes"][0]["all_passed"] is True
    # Update: same id, version + 1, etag changes, change appended,
    # expiry renewed.
    clock.advance(50)
    newer = {"timelines": [_timeline("Japan-France")]}
    status, meta2 = store.update_status(feed_id, newer, all_passed=True)
    assert status == "updated" and meta2["version"] == 2
    assert meta2["etag"] != meta["etag"]
    assert meta2["expires_at"] > meta["expires_at"]
    log = store.changes(feed_id)
    assert [c["kind"] for c in log["changes"]] == ["created", "updated"]
    assert log["changes"][1]["timelines"] == ["Japan-France"]
    assert store.update_status("missing", newer)[0] == "not_found"
    assert store.revoke(feed_id) is True
    assert store.changes(feed_id) is None
    assert store.revoke(feed_id) is False


def test_multi_store_expiry_bounds_and_ownership():
    clock = FakeClock()
    store = MultiTimelineFeedStore(max_feeds=2, ttl_seconds=100,
                                   clock=clock)
    first = store.create(_payload())["feed_id"]
    clock.advance(50)
    second = store.create(_payload(), owner_token=TOKEN)
    assert second["owned"] is True
    assert TOKEN not in json.dumps(second)
    second_id = second["feed_id"]
    clock.advance(25)
    third = store.create(_payload())["feed_id"]  # evicts the oldest
    assert store.get(first) is None and store.get(second_id) is not None
    assert store.update_status(second_id, _payload())[0] == "forbidden"
    assert store.update_status(
        second_id, _payload(), owner_token=WRONG)[0] == "forbidden"
    assert store.update_status(
        second_id, _payload(), owner_token=TOKEN)[0] == "updated"
    assert store.revoke_status(second_id) == "forbidden"
    assert store.revoke_status(
        second_id, owner_token=TOKEN) == "revoked"
    clock.advance(200)  # everything left expires
    assert store.get(third) is None
    assert store.info()["count"] == 0
    with pytest.raises(ValueError):
        MultiTimelineFeedStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        MultiTimelineFeedStore(max_feeds=0)


def test_multi_store_change_log_capped():
    store = MultiTimelineFeedStore()
    feed_id = store.create(_payload())["feed_id"]
    for _ in range(25):
        store.update_status(feed_id, _payload())
    log = store.changes(feed_id)
    assert log["version"] == 26
    assert len(log["changes"]) == MultiTimelineFeedStore.MAX_CHANGES
    assert log["changes"][-1]["version"] == 26


def test_multi_store_persistence_round_trip(tmp_path):
    path = tmp_path / "multifeeds.json"
    clock = FakeClock()
    store = MultiTimelineFeedStore(path=path, clock=clock)
    meta = store.create(_payload(), owner_token=TOKEN, all_passed=True)
    feed_id = meta["feed_id"]
    raw = path.read_text(encoding="utf-8")
    assert TOKEN not in raw  # only the token's hash is ever persisted
    revived = MultiTimelineFeedStore(path=path, clock=clock)
    assert revived.get(feed_id) is not None
    assert revived.meta(feed_id)["version"] == 1
    assert revived.changes(feed_id)["changes"][0]["kind"] == "created"
    assert revived.revoke(feed_id, owner_token=TOKEN) is True
    assert MultiTimelineFeedStore(
        path=path, clock=clock).get(feed_id) is None
    path.write_text("{not json", encoding="utf-8")
    assert MultiTimelineFeedStore(
        path=path, clock=clock).info()["count"] == 0


# -- the HTTP surface --------------------------------------------------------


def test_multi_http_create_fetch_etag_update_revoke():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        assert status == 201
        created = json.loads(body)
        assert created["version"] == 1 and created["all_passed"] is True
        assert created["timelines"] == ["Japan-France", "Kyoto only"]
        url = base_url + created["url"]
        assert url.endswith(".ics")
        status, headers, body = _request(url)
        assert status == 200
        assert "text/calendar" in headers.get("Content-Type", "")
        assert headers.get("Cache-Control") == "public, max-age=300"
        etag = headers.get("ETag", "").strip('"')
        assert etag == created["etag"]
        text = body.decode().replace("\r\n ", "")
        assert text.count("BEGIN:VEVENT") == 6
        assert "TastePilot [Japan-France / Museum]: Paris" in text
        # Unchanged: a conditional poll is a cheap 304, no body.
        status, headers, body = _request(
            url, headers={"If-None-Match": f'"{etag}"'})
        assert status == 304 and body == b""
        assert headers.get("ETag", "").strip('"') == etag
        # The change log names the creation.
        status, _h, body = _request(base_url + created["changes_url"])
        log = json.loads(body)
        assert log["version"] == 1
        assert log["changes"][0]["kind"] == "created"
        # Health names the multi-feed store.
        status, _h, body = _request(f"{base_url}/api/health")
        health = json.loads(body)
        assert health["multi_feeds"]["count"] == 1
        assert health["multi_feeds"]["ttl_seconds"] == \
            DEFAULT_FEED_TTL_SECONDS
        # Update in place: one timeline now — version 2, NEW etag,
        # the old etag no longer matches, the log grows.
        feed_id = created["feed_id"]
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}",
            {"timelines": [_timeline("Japan-France")]})
        updated = json.loads(body)
        assert status == 200 and updated["updated"] is True
        assert updated["version"] == 2 and updated["etag"] != etag
        status, _h, body = _request(
            url, headers={"If-None-Match": f'"{etag}"'})
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 4
        status, _h, body = _request(base_url + created["changes_url"])
        log = json.loads(body)
        assert [c["kind"] for c in log["changes"]] == ["created",
                                                      "updated"]
        # Meta recomputes per-timeline verdicts.
        status, _h, body = _request(f"{base_url}/api/multi-feeds/{feed_id}")
        meta = json.loads(body)
        assert status == 200 and meta["all_passed"] is True
        assert meta["timelines_summary"][0]["events"] == 4
        # Revoke: every route 404s from then on.
        status, _h, _b = _request(
            f"{base_url}/api/multi-feeds/{feed_id}", method="DELETE")
        assert status == 200
        for route in (url, base_url + created["changes_url"]):
            status, _h, _b = _request(route)
            assert status == 404
    finally:
        server.shutdown()


def test_multi_http_tampered_timeline_not_verified():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        payload = _payload()
        payload["timelines"][1]["branches"][0]["plan"]["items"][0][
            "affinity"] = 0.01
        status, _h, body = _post(f"{base_url}/api/multi-feeds", payload)
        assert status == 201  # tampering is a verdict, not a refusal
        created = json.loads(body)
        assert created["all_passed"] is False
        status, _h, body = _request(base_url + created["url"])
        text = body.decode().replace("\r\n ", "")
        assert "NOT VERIFIED" in text
        assert "Timeline: Kyoto only" in text
    finally:
        server.shutdown()


def test_multi_http_gates():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        # A bundle with no dated leg anywhere -> no_dated_legs.
        plain = TastePilotAgent().run(
            "a slow weekend in Kyoto, I love Spirited Away")
        dateless = {"timelines": [{"name": "No dates", "base": plain,
                    "branches": [{"name": "Museum", "plan": swap_plan(
                        plain, PRIMARY, ALT)}]}]}
        status, _h, body = _post(f"{base_url}/api/multi-feeds", dateless)
        assert status == 400 and "no_dated_legs" in body.decode()
        # Bundle-shape problems -> categorised 400s.
        for bad, needle in (
                ({"timelines": []}, "bad_timelines"),
                ({"timelines": [{"name": "A", "base": {}, "branches": []},
                                 {"name": "A", "base": {},
                                  "branches": []}]},
                 "duplicate_timeline_name"),
                ({"timelines": "x"}, "timelines are required"),
                ({"base": {}}, "timelines are required")):
            status, _h, body = _post(f"{base_url}/api/multi-feeds", bad)
            assert status == 400 and needle in body.decode()
        status, _h, body = _request(f"{base_url}/api/multi-feeds",
                                    method="POST", body=b"{oops",
                                    headers={"Content-Type":
                                             "application/json"})
        assert status == 400 and "invalid JSON" in body.decode()
        # Unknown ids -> 404 on every route.
        for route in ("/api/multi-feeds/nope.ics", "/api/multi-feeds/nope",
                      "/api/multi-feeds/nope/changes"):
            status, _h, _b = _request(base_url + route)
            assert status == 404
        status, _h, _b = _post(f"{base_url}/api/multi-feeds/nope",
                               _payload())
        assert status == 404
        # DELETE anywhere else stays 405.
        status, _h, _b = _request(f"{base_url}/api/plan", method="DELETE")
        assert status == 405
    finally:
        server.shutdown()


def test_multi_http_oversize_declared_length_413():
    import http.client
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        port = int(base_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/multi-feeds")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(3_000_000))
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        assert resp.status == 413
        resp.read()
        conn.close()
    finally:
        server.shutdown()


def test_multi_http_owner_flow():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload(), token=TOKEN)
        assert status == 201
        created = json.loads(body)
        assert created["owned"] is True
        feed_id = created["feed_id"]
        url = base_url + created["url"]
        for bad_token in (None, WRONG):
            status, _h, _b = _post(
                f"{base_url}/api/multi-feeds/{feed_id}", _payload(),
                token=bad_token)
            assert status == 403
        status, _h, _b = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                               _payload(), token=TOKEN)
        assert status == 200
        status, _h, _b = _post(f"{base_url}/api/multi-feeds", _payload(),
                               token="short")
        assert status == 400
        feed_url = f"{base_url}/api/multi-feeds/{feed_id}"
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


def test_multi_feed_survives_server_restart(tmp_path):
    feed_file = tmp_path / "multifeeds.json"
    server, base_url = _serve(make_handler(
        TastePilotAgent(), multi_feed_file=feed_file))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        assert status == 201
        created = json.loads(body)
        url_path, changes_path = created["url"], created["changes_url"]
        etag = created["etag"]
    finally:
        server.shutdown()
    server, base_url = _serve(make_handler(
        TastePilotAgent(), multi_feed_file=feed_file))
    try:
        status, headers, body = _request(base_url + url_path)
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 6
        assert headers.get("ETag", "").strip('"') == etag
        status, _h, body = _request(base_url + changes_path)
        assert json.loads(body)["version"] == 1
    finally:
        server.shutdown()


def test_multi_cli_serve_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    assert "--multi-feed-file" in text
    assert "--multi-feed-ttl-days" in text


def test_multi_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Add this timeline to a multi-timeline bundle" in html
    assert "Create multi-timeline subscription" in html
    assert "/api/multi-feeds" in html and "changes_url" in html
    readme = README.read_text(encoding="utf-8")
    assert "multi-timeline" in readme
    assert "TASTEPILOT_MULTI_FEED_FILE" in readme
    assert "ETag" in readme and "change log" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "multi-timeline" in submission
    assert "change log" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "TASTEPILOT_MULTI_FEED_FILE" in deploy
    assert "Multi-feed probe" in deploy
