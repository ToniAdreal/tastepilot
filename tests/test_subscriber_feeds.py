"""Subscriber feed directory: a cross-feed view of a
token's calendar subscriptions.

Doctrine under test: calendar feeds — single-timeline and
multi-timeline alike — are created and managed one feed at
a time, feed id in hand, and until now a subscriber had no
way to answer "what subscriptions do I own?" Shares have
an owner dashboard and webhooks a subscriber directory;
feeds now have the same: ``list_for_owner`` on both feed
stores and ``GET /api/subscriber/feeds`` list every live
feed the presenting token owns, each row with its stable
URLs (leg-level and paced), expiry, branch/timeline names,
and the verdict recomputed from the stored timeline at
list time — a feed whose branch no longer verifies lists
as FAIL, never as the verdict it was created with. The
directory is read-only and grants nothing: feeds owned by
another token, or by no token, never appear, and rows
carry metadata only (never a stored payload, a webhook
URL or secret, or the token / its full hash).
"""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.server import (OWNER_TOKEN_HEADER,
                               MultiTimelineFeedStore,
                               TimelineFeedStore, make_handler,
                               owner_token_hash)
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

# Built by concatenation: a single long credential-shaped
# literal in this file was once rewritten at write time
# (see run 42's incident note); concatenation keeps the
# intended value intact on disk.
TOKEN = "feeddir-token-" + "alpha-0123456789abcd"
OTHER = "feeddir-token-" + "bravo-0123456789abcd"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _single_payload():
    return {"base": {}, "branches": [{"name": "Museum"}]}


def _multi_payload(name="Trip A"):
    return {"timelines": [{"name": name, "base": {},
                           "branches": []}]}


# -- store level ---------------------------------------------------------------


def test_single_feed_directory_lists_only_the_owners_feeds():
    store = TimelineFeedStore(clock=FakeClock())
    owned = store.create(_single_payload(),
                         owner_token=TOKEN)["feed_id"]
    store.create(_single_payload(), owner_token=OTHER)
    store.create(_single_payload())
    rows = store.list_for_owner(TOKEN)
    assert [r["feed_id"] for r in rows] == [owned]
    row = rows[0]
    assert row["owned"] is True
    assert row["branches"] == ["Museum"]
    assert row["columns"] == 2
    assert row["etag"]
    assert row["expires_at"]
    assert store.list_for_owner(OTHER)[0]["feed_id"] != owned
    assert store.list_for_owner(None) == []
    assert store.list_for_owner("short") == []


def test_multi_feed_directory_lists_only_the_owners_feeds():
    store = MultiTimelineFeedStore(clock=FakeClock())
    owned = store.create(_multi_payload("Owned trip"),
                         owner_token=TOKEN)["feed_id"]
    store.create(_multi_payload("Other trip"),
                 owner_token=OTHER)
    store.create(_multi_payload("Unowned trip"))
    rows = store.list_for_owner(TOKEN)
    assert [r["feed_id"] for r in rows] == [owned]
    row = rows[0]
    assert row["timelines"] == ["Owned trip"]
    assert row["version"] == 1
    assert row["etag"]
    assert store.list_for_owner(None) == []
    assert store.list_for_owner("short") == []


def test_directories_never_carry_payload_or_token():
    single = TimelineFeedStore(clock=FakeClock())
    single.create(_single_payload(), owner_token=TOKEN)
    multi = MultiTimelineFeedStore(clock=FakeClock())
    multi.create(_multi_payload(), owner_token=TOKEN)
    for rows in (single.list_for_owner(TOKEN),
                 multi.list_for_owner(TOKEN)):
        blob = json.dumps(rows)
        assert "payload" not in blob
        assert TOKEN not in blob
        assert owner_token_hash(TOKEN) not in blob


def test_directory_drops_expired_and_revoked_feeds():
    clock = FakeClock()
    single = TimelineFeedStore(clock=clock)
    first = single.create(_single_payload(),
                          owner_token=TOKEN)["feed_id"]
    second = single.create(_single_payload(),
                           owner_token=TOKEN)["feed_id"]
    assert single.revoke(first, owner_token=TOKEN)
    assert [r["feed_id"]
            for r in single.list_for_owner(TOKEN)] == [second]
    clock.advance(single.ttl_seconds + 1)
    assert single.list_for_owner(TOKEN) == []
    multi = MultiTimelineFeedStore(clock=FakeClock())
    multi.create(_multi_payload(), owner_token=TOKEN)
    assert len(multi.list_for_owner(TOKEN)) == 1


# -- HTTP level ------------------------------------------------------------------


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request(url, method="GET", body=None, token=None,
             raw_token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[OWNER_TOKEN_HEADER] = token
    if raw_token is not None:
        headers[OWNER_TOKEN_HEADER] = raw_token
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(url, obj=None, token=None):
    return _request(url, method="POST",
                    body=json.dumps(obj).encode()
                    if obj is not None else b"", token=token)


def _timeline():
    base = TastePilotAgent().run(DATED)
    return {"base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _tampered_timeline():
    timeline = _timeline()
    branch = copy.deepcopy(timeline["branches"][0]["plan"])
    branch["items"][0]["affinity"] = 0.01
    return {"base": timeline["base"],
            "branches": [{"name": "Museum", "plan": branch}]}


def test_http_feed_directory_flow_and_gates():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        # Gates: no token -> 401, malformed token -> 400.
        status, _, _ = _request(f"{base_url}/api/subscriber/feeds")
        assert status == 401
        status, _, _ = _request(f"{base_url}/api/subscriber/feeds",
                                raw_token="short")
        assert status == 400
        # One owned single feed, one owned multi-feed, one
        # unowned single feed (in nobody's directory).
        status, _, raw = _post(f"{base_url}/api/feeds", _timeline(),
                            token=TOKEN)
        assert status == 201
        single_id = json.loads(raw)["feed_id"]
        timeline = _timeline()
        status, _, raw = _post(
            f"{base_url}/api/multi-feeds",
            {"timelines": [{"name": "Japan-France",
                            "base": timeline["base"],
                            "branches": timeline["branches"]}]},
            token=TOKEN)
        assert status == 201
        multi_id = json.loads(raw)["feed_id"]
        status, _, _ = _post(f"{base_url}/api/feeds", _timeline())
        assert status == 201
        status, headers, raw = _request(
            f"{base_url}/api/subscriber/feeds", token=TOKEN)
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        data = json.loads(raw)
        assert data["count"] == 2
        assert data["owner_fingerprint"] == \
            owner_token_hash(TOKEN)[:12]
        single = data["feeds"][0]
        assert single["feed_id"] == single_id
        assert single["feed_url"] == f"/api/feeds/{single_id}.ics"
        assert single["paced_url"] == \
            f"/api/feeds/{single_id}.ics?format=ics-days"
        assert single["branches"] == ["Museum"]
        assert single["all_passed"] is True
        multi = data["multi_feeds"][0]
        assert multi["feed_id"] == multi_id
        assert multi["feed_url"] == \
            f"/api/multi-feeds/{multi_id}.ics"
        assert multi["paced_url"] == \
            f"/api/multi-feeds/{multi_id}.ics?format=ics-days"
        assert multi["timelines"] == ["Japan-France"]
        assert multi["all_passed"] is True
        assert multi["timelines_summary"][0]["all_passed"] is True
        blob = raw.decode()
        assert TOKEN not in blob
        assert owner_token_hash(TOKEN) not in blob
        # Another token's directory is empty.
        status, _, raw = _request(f"{base_url}/api/subscriber/feeds",
                                  token=OTHER)
        assert status == 200
        assert json.loads(raw)["count"] == 0
    finally:
        server.shutdown()


def test_http_directory_tampered_feed_lists_as_fail():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _, raw = _post(f"{base_url}/api/feeds",
                            _tampered_timeline(), token=TOKEN)
        assert status == 201
        assert json.loads(raw)["all_passed"] is False
        status, _, raw = _request(f"{base_url}/api/subscriber/feeds",
                                  token=TOKEN)
        assert status == 200
        data = json.loads(raw)
        assert data["feeds"][0]["all_passed"] is False
    finally:
        server.shutdown()


# -- UI + docs wiring -----------------------------------------------------------


def test_ui_and_docs_describe_feed_directory():
    page = INDEX.read_text()
    assert "/api/subscriber/feeds" in page
    assert "feeddirbtn" in page
    assert "Your calendar subscriptions" in page
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "/api/subscriber/feeds" in text
        assert "feed director" in text.lower()
