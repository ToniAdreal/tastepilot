"""Webhook event filters: a webhook subscribes to events.

Doctrine under test: until now a registered webhook received
every push its feed produced — the registration test delivery
and every update. Now the subscriber names the events they
want, at registration (``"events": [...]``) or later in place
(``POST .../webhooks/<id>/set-events``): any subset of
``WEBHOOK_EVENTS``, canonicalised, or no filter at all (None —
every event, and what a legacy registration carries). A push
whose event the webhook filtered out is NOT sent to it: no
attempt, no delivery record, no failure-count effect —
opting out is not failing, and it is not being paused either
(a paused webhook's wanted pushes are recorded as skipped;
a filtered webhook simply was not a recipient). The filter
governs automatic pushes only — an explicit on-demand
redelivery still resends. Setting the filter stamps
``events_updated_at``, is idempotent for the filter already
in effect, an explicit null resets to every event, and
everything else (id, URL, secret, pause state, failure
count, created_at) survives; delivery records are never
edited. Validation is categorised (``bad_webhook_events``)
and never echoes the supplied values.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (WEBHOOK_EVENTS,
                               validate_webhook_events,
                               webhook_id_for,
                               webhook_subscribes_to)
from tastepilot.server import MultiTimelineFeedStore, make_handler
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
# Built by concatenation on purpose (run 42's lesson): a single
# credential-shaped literal can be redacted at write time.
TOKEN = "owner-" + "token-0123456789abcdef0123456789"
WRONG = "owner-" + "token-ffffffffffffffffffffffff"
SUPPLIED = "subscriber-" + "supplied-secret-0123456789"
URL = "https://hooks.example.com/push"
URL2 = "https://hooks2.example.com/push"
WID = webhook_id_for(URL)
UPDATED = ["multi_feed.updated"]


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _timeline(name, text=DATED):
    base = TastePilotAgent().run(text)
    return {"name": name, "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _payload():
    return {"timelines": [_timeline("Japan-France")]}


def _store(tmp_path=None, owner_token=None, clock=None, events=None):
    path = tmp_path / "feeds.json" if tmp_path else None
    store = MultiTimelineFeedStore(path=path,
                                   clock=clock or FakeClock())
    meta = store.create(_payload(), owner_token=owner_token,
                        all_passed=True)
    status, wmeta = store.add_webhook(
        meta["feed_id"], URL, owner_token=owner_token,
        signing_secret=SUPPLIED, events=events)
    assert status == "ok"
    return store, meta["feed_id"], wmeta


def _entry(store, feed_id):
    return store.webhook_entries(feed_id)[0]


def _meta(store, feed_id, owner_token=None):
    status, webhooks = store.webhooks(feed_id, owner_token=owner_token)
    assert status == "ok"
    return webhooks[0]


# -- notify-level validation ---------------------------------------------


def test_validate_events_canonicalises_and_dedupes():
    assert validate_webhook_events(None) is None
    assert validate_webhook_events(
        ["multi_feed.updated", "multi_feed.subscribed",
         "multi_feed.updated"]) == ["multi_feed.subscribed",
                                    "multi_feed.updated"]
    assert validate_webhook_events(
        list(WEBHOOK_EVENTS)) == list(WEBHOOK_EVENTS)
    assert validate_webhook_events(
        ("multi_feed.updated",)) == ["multi_feed.updated"]


def test_validate_events_rejects_bad_shapes_without_echo():
    secretish = "multi_feed." + "hunter2-secret-value"
    for bad in ([], "multi_feed.updated", 42, [42],
                ["multi_feed.deleted"], [secretish], [None]):
        with pytest.raises(ValueError) as excinfo:
            validate_webhook_events(bad)
        assert "bad_webhook_events" in str(excinfo.value)
        assert "hunter2" not in str(excinfo.value)  # never echoed


def test_subscribes_to_helper_defaults_to_every_event():
    assert webhook_subscribes_to({}, "multi_feed.updated")
    assert webhook_subscribes_to({"events": None},
                                 "multi_feed.updated")
    assert webhook_subscribes_to({"events": []},
                                 "multi_feed.updated")
    assert webhook_subscribes_to("junk", "multi_feed.updated")
    entry = {"events": UPDATED}
    assert webhook_subscribes_to(entry, "multi_feed.updated")
    assert not webhook_subscribes_to(entry, "multi_feed.subscribed")


# -- store-level registration + setting -----------------------------------


def test_registration_stores_filter_and_meta_reports_it():
    store, feed_id, wmeta = _store(events=UPDATED)
    assert wmeta["events"] == UPDATED
    assert wmeta["events_updated_at"] is None
    assert _entry(store, feed_id)["events"] == UPDATED
    # No filter at registration: meta reports every event.
    store2, feed2, meta2 = _store()
    assert meta2["events"] == list(WEBHOOK_EVENTS)
    # A re-registration never replaces the first filter.
    status, again = store.add_webhook(
        feed_id, URL, signing_secret=None, events=None)
    assert status == "ok" and again["events"] == UPDATED
    # A bad filter at registration leaves the feed unchanged.
    with pytest.raises(ValueError) as excinfo:
        store.add_webhook(feed_id, URL2, events=["bogus.event"])
    assert "bad_webhook_events" in str(excinfo.value)
    status, webhooks = store.webhooks(feed_id)
    assert status == "ok" and len(webhooks) == 1


def test_set_events_stamps_and_preserves_everything_else():
    clock = FakeClock()
    store, feed_id, wmeta = _store(clock=clock)
    clock.now += 60
    status, meta = store.set_webhook_events(feed_id, WID, UPDATED)
    assert status == "ok"
    assert meta["events"] == UPDATED
    assert meta["events_updated_at"] is not None
    assert meta["webhook_id"] == WID  # a filter is not an address
    assert meta["created_at"] == wmeta["created_at"]
    assert meta["signed"] is True
    assert _entry(store, feed_id)["signing_secret"] == SUPPLIED
    # Setting the same filter again stamps nothing new.
    stamp = meta["events_updated_at"]
    clock.now += 60
    status, meta = store.set_webhook_events(
        feed_id, WID, ["multi_feed.updated", "multi_feed.updated"])
    assert status == "ok"
    assert meta["events_updated_at"] == stamp
    # An explicit null resets to every event (and stamps).
    status, meta = store.set_webhook_events(feed_id, WID, None)
    assert status == "ok"
    assert meta["events"] == list(WEBHOOK_EVENTS)
    assert meta["events_updated_at"] != stamp
    # Resetting an already-unfiltered webhook stamps nothing.
    stamp = meta["events_updated_at"]
    clock.now += 60
    status, meta = store.set_webhook_events(feed_id, WID, None)
    assert status == "ok" and meta["events_updated_at"] == stamp


def test_set_events_bad_filter_leaves_webhook_unchanged():
    store, feed_id, _ = _store(events=UPDATED)
    before = _entry(store, feed_id)
    with pytest.raises(ValueError) as excinfo:
        store.set_webhook_events(feed_id, WID, ["nope.event"])
    assert "bad_webhook_events" in str(excinfo.value)
    with pytest.raises(ValueError):
        store.set_webhook_events(feed_id, WID, [])
    assert _entry(store, feed_id) == before


def test_set_events_preserves_pause_state_and_count():
    store, feed_id, _ = _store()
    payload = {"event": "multi_feed.updated", "version": 2}
    record = {"webhook_id": WID, "host": "hooks.example.com",
              "event": "multi_feed.updated", "version": 2,
              "status": "failed", "attempts": 3,
              "http_status": 500, "error": "http_error (HTTP 500)",
              "signed": True}
    for _ in range(3):
        store.record_deliveries(feed_id, [dict(record)],
                                payload=payload)
    assert _meta(store, feed_id)["paused"] is True
    status, meta = store.set_webhook_events(feed_id, WID, UPDATED)
    assert status == "ok"
    assert meta["paused"] is True
    assert meta["paused_reason"] == "auto"
    assert meta["consecutive_failures"] == 3


def test_set_events_gates():
    store, feed_id, _ = _store(owner_token=TOKEN)
    status, _ = store.set_webhook_events("nope", WID, UPDATED,
                                         owner_token=TOKEN)
    assert status == "not_found"
    status, _ = store.set_webhook_events(feed_id, WID, UPDATED)
    assert status == "forbidden"
    status, _ = store.set_webhook_events(feed_id, WID, UPDATED,
                                         owner_token=WRONG)
    assert status == "forbidden"
    status, _ = store.set_webhook_events(feed_id, "missing", UPDATED,
                                         owner_token=TOKEN)
    assert status == "webhook_not_found"
    status, meta = store.set_webhook_events(feed_id, WID, UPDATED,
                                            owner_token=TOKEN)
    assert status == "ok" and meta["events"] == UPDATED


def test_events_filter_persists_and_malformed_rows_degrade(tmp_path):
    store, feed_id, _ = _store(tmp_path=tmp_path, events=UPDATED)
    path = tmp_path / "feeds.json"
    store2 = MultiTimelineFeedStore(path=path, clock=FakeClock())
    assert _entry(store2, feed_id)["events"] == UPDATED
    status, meta = store2.set_webhook_events(feed_id, WID, None)
    assert status == "ok" and meta["events_updated_at"] is not None
    # A malformed stored filter degrades to no-filter on load —
    # it never silences the webhook or takes the feed down.
    data = json.loads(path.read_text())
    data["entries"][0]["webhooks"][0]["events"] = ["bogus.event"]
    data["entries"][0]["webhooks"][0]["events_updated_at"] = "junk"
    path.write_text(json.dumps(data))
    store3 = MultiTimelineFeedStore(path=path, clock=FakeClock())
    entry = _entry(store3, feed_id)
    assert entry["events"] is None
    assert entry["events_updated_at"] is None
    assert _meta(store3, feed_id)["events"] == list(WEBHOOK_EVENTS)


# -- HTTP surface ---------------------------------------------------------


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(url, obj=None, token=None, raw=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    body = raw if raw is not None else (
        json.dumps(obj).encode() if obj is not None else b"")
    return _request(url, method="POST", body=body, headers=headers)


def _get(url, token=None):
    headers = {}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, headers=headers)


class _Receiver:
    """A loopback webhook receiver capturing headers + bodies."""

    def __init__(self, status=200):
        self.status = status
        self.hits = []  # (headers dict, body bytes)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.hits.append(
                    (dict(self.headers), self.rfile.read(length)))
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

    def events_seen(self):
        return [json.loads(body)["event"]
                for _headers, body in self.hits]

    def close(self):
        self.server.shutdown()


def _feed(base, token=None):
    status, body = _post(f"{base}/api/multi-feeds", _payload(),
                         token=token)
    assert status in (200, 201)
    return json.loads(body)["feed_id"]


def _register(base, feed_id, receiver, token=None, **extra):
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                         {"url": receiver.url, **extra}, token=token)
    assert status == 201, body
    return json.loads(body)


def _update(base, feed_id, token=None):
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                         _payload(), token=token)
    assert status == 200
    return json.loads(body)


def _deliveries(base, feed_id, token=None):
    status, body = _get(f"{base}/api/multi-feeds/{feed_id}"
                        "/notifications", token=token)
    assert status == 200
    return json.loads(body)["deliveries"]


def test_http_filtered_registration_gets_no_ping_but_gets_updates():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        reg = _register(base, feed_id, receiver, events=UPDATED)
        assert reg["events"] == UPDATED
        assert reg["delivery"] is None  # its own filter opted out
        assert receiver.hits == []       # no ping was attempted
        assert _deliveries(base, feed_id) == []  # and none recorded
        result = _update(base, feed_id)
        assert len(result["notifications"]) == 1
        assert result["notifications"][0]["status"] == "delivered"
        assert receiver.events_seen() == ["multi_feed.updated"]
        assert len(_deliveries(base, feed_id)) == 1
    finally:
        server.shutdown()
        receiver.close()


def test_http_unfiltered_webhook_still_gets_ping_and_updates():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        reg = _register(base, feed_id, receiver)
        assert reg["events"] == list(WEBHOOK_EVENTS)
        assert reg["delivery"]["status"] == "delivered"
        _update(base, feed_id)
        assert receiver.events_seen() == ["multi_feed.subscribed",
                                          "multi_feed.updated"]
    finally:
        server.shutdown()
        receiver.close()


def test_http_set_events_moves_pushes_and_redelivery_ignores_filter():
    receiver = _Receiver(status=500)
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        reg = _register(base, feed_id, receiver)  # ping fails (500)
        wid = reg["webhook_id"]
        assert len(_deliveries(base, feed_id)) == 1
        # Filter updates out: the next update is not attempted,
        # not recorded, and does not touch the failure count.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
            "/set-events", {"events": ["multi_feed.subscribed"]})
        assert status == 200
        meta = json.loads(body)
        assert meta["events"] == ["multi_feed.subscribed"]
        assert meta["events_updated_at"]
        assert meta["consecutive_failures"] == 1
        hits_after_ping = len(receiver.hits)  # ping + its retries
        result = _update(base, feed_id)
        assert result["notifications"] == []
        assert len(receiver.hits) == hits_after_ping  # not attempted
        assert len(_deliveries(base, feed_id)) == 1
        # But the subscriber's explicit redelivery of that
        # failed ping still resends — the filter governs
        # automatic pushes only.
        receiver.status = 200
        failed = _deliveries(base, feed_id)[0]
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/notifications/"
            f"{failed['delivery_id']}/redeliver")
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
        # Reset the filter to every event: updates flow again.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
            "/set-events", {"events": None})
        assert status == 200
        assert json.loads(body)["events"] == list(WEBHOOK_EVENTS)
        result = _update(base, feed_id)
        assert len(result["notifications"]) == 1
        assert "multi_feed.updated" in receiver.events_seen()
    finally:
        server.shutdown()
        receiver.close()


def test_http_set_events_gates_and_bad_bodies():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base, token=TOKEN)
        reg = _register(base, feed_id, receiver, token=TOKEN)
        wid = reg["webhook_id"]
        url = (f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
               "/set-events")
        status, _ = _post(url, {"events": UPDATED})  # no token
        assert status == 403
        status, _ = _post(url, {"events": UPDATED}, token=WRONG)
        assert status == 403
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/nope"
            "/set-events", {"events": UPDATED}, token=TOKEN)
        assert status == 404
        status, _ = _post(
            f"{base}/api/multi-feeds/nope/webhooks/{wid}"
            "/set-events", {"events": UPDATED}, token=TOKEN)
        assert status == 404
        status, body = _post(url, raw=b"{nope", token=TOKEN)
        assert status == 400
        status, body = _post(url, raw=b"[1, 2]", token=TOKEN)
        assert status == 400
        status, body = _post(url, {}, token=TOKEN)  # events required
        assert status == 400 and b"bad_webhook_events" in body
        status, body = _post(url, {"events": []}, token=TOKEN)
        assert status == 400 and b"bad_webhook_events" in body
        bogus = "multi_feed." + "hunter2-secret-value"
        status, body = _post(url, {"events": [bogus]}, token=TOKEN)
        assert status == 400 and b"bad_webhook_events" in body
        assert b"hunter2" not in body  # supplied values never echoed
        status, body = _post(url, {"events": UPDATED},
                             token="bad token with spaces")
        assert status == 400
        # Registration with a bad filter is the same categorised 400.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks",
            {"url": "https://other.example.com/hook",
             "events": ["bogus.event"]}, token=TOKEN)
        assert status == 400 and b"bad_webhook_events" in body
        # The owner can still set the filter after all of that.
        status, body = _post(url, {"events": UPDATED}, token=TOKEN)
        assert status == 200
        assert json.loads(body)["events"] == UPDATED
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_carry_event_filters():
    html = INDEX.read_text()
    assert "set-events" in html
    assert "Set webhook event filters" in html
    assert "webhookevents" in html
    for doc in (README, SUBMISSION, DEPLOY):
        assert "set-events" in doc.read_text()
