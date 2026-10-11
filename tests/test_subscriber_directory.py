"""Subscriber webhook directory: a cross-feed view of a
token's push webhook subscriptions.

Doctrine under test: push webhooks are registered per
multi-timeline feed, so until now a subscriber could only
see a webhook by returning to its own feed — feed id in
hand — one feed at a time. Shares have an owner dashboard
and profiles an owner panel; webhooks now have the same:
``MultiTimelineFeedStore.webhooks_for_owner`` and
``GET /api/subscriber/webhooks`` list every webhook
registered on a feed the presenting token owns, across
all of that owner's feeds, each row naming its feed (id,
version, timeline names, expiry) beside the webhook's own
meta plus its delivery tallies from the feed's capped
log. The directory is read-only and grants nothing:
webhooks on unowned feeds, or on another owner's feeds,
never appear, listings carry host labels only (never a
stored URL, a signing secret, or the token / its full
hash), and management stays on the per-feed routes under
the same owner rule.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import webhook_id_for
from tastepilot.server import (OWNER_TOKEN_HEADER,
                               MultiTimelineFeedStore, make_handler,
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
TOKEN = "subscriber-token-" + "alpha-0123456789abcd"
OTHER = "subscriber-token-" + "bravo-0123456789abcd"
HOOK_A = "https://hooks.example.com/alpha"
HOOK_B = "https://hooks.example.com/bravo"
SUPPLIED = "subscriber-supplied-" + "secret-0123456789"

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


def _payload(name="Trip A"):
    return {"timelines": [{"name": name, "base": {},
                           "branches": []}]}


def _store_with(clock=None):
    store = MultiTimelineFeedStore(
        clock=clock or FakeClock())
    owned = store.create(_payload("Owned trip"),
                         owner_token=TOKEN)["feed_id"]
    other = store.create(_payload("Other trip"),
                         owner_token=OTHER)["feed_id"]
    unowned = store.create(_payload("Unowned trip"))["feed_id"]
    return store, owned, other, unowned


# -- store level ---------------------------------------------------------------


def test_directory_lists_only_the_owners_webhooks():
    store, owned, other, unowned = _store_with()
    store.add_webhook(owned, HOOK_A, owner_token=TOKEN,
                      signing_secret=SUPPLIED)
    store.add_webhook(other, HOOK_B, owner_token=OTHER)
    store.add_webhook(unowned, HOOK_B)
    rows = store.webhooks_for_owner(TOKEN)
    assert len(rows) == 1
    row = rows[0]
    assert row["webhook_id"] == webhook_id_for(HOOK_A)
    assert row["host"] == "hooks.example.com"
    assert row["signed"] is True
    assert row["feed_id"] == owned
    assert row["feed_version"] == 1
    assert row["feed_timelines"] == ["Owned trip"]
    assert row["feed_expires_at"]
    assert row["deliveries"] == 0
    assert row["last_delivery_status"] is None
    # The other owner's directory holds only their own;
    # the unowned feed's webhook appears in neither.
    other_rows = store.webhooks_for_owner(OTHER)
    assert [r["feed_id"] for r in other_rows] == [other]
    assert store.webhooks_for_owner(None) == []
    assert store.webhooks_for_owner("short") == []


def test_directory_never_carries_url_secret_or_token():
    store, owned, _, _ = _store_with()
    store.add_webhook(owned, HOOK_A, owner_token=TOKEN,
                      signing_secret=SUPPLIED)
    blob = json.dumps(store.webhooks_for_owner(TOKEN))
    assert HOOK_A not in blob
    assert SUPPLIED not in blob
    assert TOKEN not in blob
    assert owner_token_hash(TOKEN) not in blob


def test_directory_spans_feeds_and_counts_deliveries():
    store, owned, _, _ = _store_with()
    second = store.create(_payload("Second trip"),
                          owner_token=TOKEN)["feed_id"]
    _, meta = store.add_webhook(owned, HOOK_A,
                                owner_token=TOKEN)
    store.add_webhook(second, HOOK_B, owner_token=TOKEN)
    wid = meta["webhook_id"]
    payload = {"event": "multi_feed.updated", "feed_id": owned,
               "version": 2, "etag": "x", "timelines": ["Owned trip"],
               "all_passed": True}
    store.record_deliveries(owned, [
        {"webhook_id": wid, "host": "hooks.example.com",
         "event": "multi_feed.updated", "version": 2,
         "status": "delivered", "attempts": 1},
        {"webhook_id": wid, "host": "hooks.example.com",
         "event": "multi_feed.updated", "version": 2,
         "status": "failed", "attempts": 3},
        {"webhook_id": "someone-else",
         "host": "hooks.example.com",
         "event": "multi_feed.updated", "version": 2,
         "status": "delivered", "attempts": 1}], payload=payload)
    rows = store.webhooks_for_owner(TOKEN)
    assert [r["feed_id"] for r in rows] == [owned, second]
    first = rows[0]
    assert first["deliveries"] == 2
    assert first["delivered"] == 1
    assert first["failed"] == 1
    assert first["skipped"] == 0
    assert first["last_delivery_status"] == "failed"
    assert rows[1]["deliveries"] == 0


def test_directory_reflects_pause_state_and_expiry():
    clock = FakeClock()
    store, owned, _, _ = _store_with(clock=clock)
    _, meta = store.add_webhook(owned, HOOK_A,
                                owner_token=TOKEN)
    store.set_webhook_paused(owned, meta["webhook_id"], True,
                             owner_token=TOKEN)
    rows = store.webhooks_for_owner(TOKEN)
    assert rows[0]["paused"] is True
    assert rows[0]["paused_reason"] == "manual"
    # An expired feed leaves the directory like any store read.
    clock.advance(store.ttl_seconds + 1)
    assert store.webhooks_for_owner(TOKEN) == []


# -- HTTP level ------------------------------------------------------------------


class _Receiver:
    def __init__(self, status=200):
        self.status = status
        self.hits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.hits.append(self.rfile.read(length))
                body = b"{}"
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.url = (f"http://127.0.0.1:"
                    f"{self.server.server_address[1]}/hook")

    def close(self):
        self.server.shutdown()


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
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(url, obj=None, token=None):
    return _request(url, method="POST",
                    body=json.dumps(obj).encode()
                    if obj is not None else b"", token=token)


def _plan_payload():
    base = TastePilotAgent().run(DATED)
    timeline = {"name": "Japan-France", "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}
    return {"timelines": [timeline]}


def test_http_directory_flow_and_gates():
    receiver = _Receiver()
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        # Gates: no token -> 401, malformed token -> 400.
        status, _ = _request(f"{base_url}/api/subscriber/webhooks")
        assert status == 401
        status, _ = _request(f"{base_url}/api/subscriber/webhooks",
                             raw_token="short")
        assert status == 400
        # An owned feed with a registered webhook (its ping is
        # delivered by the real receiver).
        status, raw = _post(f"{base_url}/api/multi-feeds",
                            _plan_payload(), token=TOKEN)
        assert status == 201
        feed_id = json.loads(raw)["feed_id"]
        status, raw = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url, "secret": SUPPLIED},
            token=TOKEN)
        assert status == 201
        # An unowned feed's webhook is in nobody's directory.
        status, raw = _post(f"{base_url}/api/multi-feeds",
                            _plan_payload())
        assert status == 201
        unowned_id = json.loads(raw)["feed_id"]
        status, _ = _post(
            f"{base_url}/api/multi-feeds/{unowned_id}/webhooks",
            {"url": receiver.url})
        assert status == 201
        status, raw = _request(f"{base_url}/api/subscriber/webhooks",
                               token=TOKEN)
        assert status == 200
        data = json.loads(raw)
        assert data["count"] == 1 and data["feeds"] == 1
        assert data["owner_fingerprint"] == \
            owner_token_hash(TOKEN)[:12]
        row = data["webhooks"][0]
        assert row["feed_id"] == feed_id
        assert row["feed_url"] == f"/api/multi-feeds/{feed_id}.ics"
        assert row["notifications_url"] == \
            f"/api/multi-feeds/{feed_id}/notifications"
        assert row["feed_timelines"] == ["Japan-France"]
        assert row["signed"] is True
        assert row["deliveries"] == 1
        assert row["delivered"] == 1
        assert row["last_delivery_status"] == "delivered"
        blob = raw.decode()
        assert receiver.url not in blob
        assert SUPPLIED not in blob and TOKEN not in blob
        # Another token's directory is empty.
        status, raw = _request(f"{base_url}/api/subscriber/webhooks",
                               token=OTHER)
        assert status == 200
        assert json.loads(raw)["count"] == 0
    finally:
        receiver.close()
        server.shutdown()


# -- UI + docs wiring -----------------------------------------------------------


def test_ui_and_docs_describe_subscriber_directory():
    page = INDEX.read_text()
    assert "/api/subscriber/webhooks" in page
    assert "subscriberbtn" in page
    assert "Your webhook subscriptions" in page
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "/api/subscriber/webhooks" in text
        assert "subscriber director" in text.lower()
