"""Webhook auto-pause: consecutive failures pause a webhook.

Doctrine under test: a webhook whose endpoint keeps failing is
paused, not hammered. The feed store counts consecutive failed
deliveries per webhook (the count folds in at record time, the
one path every push takes — registration pings, update pushes,
and redeliveries count the same way); a delivered push resets
the count, a skipped push is not an attempt and never counts.
At PAUSE_AFTER_CONSECUTIVE_FAILURES the webhook is paused:
later pushes are recorded as skipped deliveries (payload
still stored, so the missed change stays redeliverable),
redelivery to it is refused as webhook_paused, and bulk
redelivery skips it with that reason. Resuming — the
subscriber's deliberate act, under the feed's owner rule —
clears the count and starts the endpoint fresh.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (PAUSE_AFTER_CONSECUTIVE_FAILURES,
                               idempotency_key_for, skipped_record,
                               webhook_id_for)
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
# Built by concatenation: a single long literal in this file
# was redacted at write time in an earlier run (see BUILD_LOG
# run 42), which silently broke the owner gates it fed.
TOKEN = "pause-" + "owner-token-0123456789abcdef"
WRONG = "pause-" + "other-token-fedcba9876543210"
URL = "https://hooks.example.com/push"
WID = webhook_id_for(URL)


def _timeline(name, text=DATED):
    base = TastePilotAgent().run(text)
    return {"name": name, "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _payload():
    return {"timelines": [_timeline("Japan-France")]}


def _record(status, webhook_id=WID):
    return {"webhook_id": webhook_id, "host": "hooks.example.com",
            "event": "multi_feed.updated", "version": 2,
            "status": status,
            "attempts": 3 if status == "failed" else 1,
            "http_status": 500 if status == "failed" else 200,
            "error": "http_error (HTTP 500)" if status == "failed"
                     else None,
            "signed": False}


def _store_with_webhook(tmp_path=None, owner_token=None):
    path = tmp_path / "feeds.json" if tmp_path else None
    store = MultiTimelineFeedStore(path=path)
    meta = store.create(_payload(), owner_token=owner_token,
                        all_passed=True)
    status, wmeta = store.add_webhook(meta["feed_id"], URL,
                                      owner_token=owner_token,
                                      signing_secret=None)
    assert status == "ok"
    return store, meta["feed_id"], wmeta


def _webhook_meta(store, feed_id, owner_token=None):
    status, webhooks = store.webhooks(feed_id, owner_token=owner_token)
    assert status == "ok"
    return webhooks[0]


# -- notify.skipped_record -------------------------------------------


def test_skipped_record_shape():
    entry = {"webhook_id": WID, "url": URL, "host": "hooks.example.com",
             "signing_secret": "s" * 20}
    payload = {"event": "multi_feed.updated", "version": 4}
    record = skipped_record(entry, payload)
    # The record shape gained an idempotency_key in run 50:
    # the key its (never attempted) delivery would have
    # carried — same webhook id, same canonical payload
    # bytes — so a later redelivery matches it.
    body = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode()
    assert record == {"webhook_id": WID, "host": "hooks.example.com",
                      "event": "multi_feed.updated", "version": 4,
                      "status": "skipped", "attempts": 0,
                      "http_status": None, "error": "webhook_paused",
                      "signed": True,
                      "idempotency_key": idempotency_key_for(WID, body)}
    unsigned = skipped_record({"webhook_id": WID, "url": URL,
                               "host": "hooks.example.com"}, payload)
    assert unsigned["signed"] is False


# -- store-level health accounting ------------------------------------


def test_store_pauses_after_threshold_consecutive_failures():
    store, feed_id, wmeta = _store_with_webhook()
    assert wmeta["paused"] is False
    assert wmeta["consecutive_failures"] == 0
    payload = {"event": "multi_feed.updated", "version": 2}
    for _ in range(PAUSE_AFTER_CONSECUTIVE_FAILURES - 1):
        store.record_deliveries(feed_id, [_record("failed")],
                                payload=payload)
    meta = _webhook_meta(store, feed_id)
    assert meta["consecutive_failures"] == \
        PAUSE_AFTER_CONSECUTIVE_FAILURES - 1
    assert meta["paused"] is False
    store.record_deliveries(feed_id, [_record("failed")],
                            payload=payload)
    meta = _webhook_meta(store, feed_id)
    assert meta["consecutive_failures"] == \
        PAUSE_AFTER_CONSECUTIVE_FAILURES
    assert meta["paused"] is True


def test_store_delivered_resets_and_skipped_never_counts():
    store, feed_id, _ = _store_with_webhook()
    payload = {"event": "multi_feed.updated", "version": 2}
    store.record_deliveries(feed_id, [_record("failed")],
                            payload=payload)
    store.record_deliveries(feed_id, [_record("failed")],
                            payload=payload)
    store.record_deliveries(feed_id, [_record("skipped")],
                            payload=payload)
    assert _webhook_meta(store, feed_id)["consecutive_failures"] == 2
    store.record_deliveries(feed_id, [_record("delivered")],
                            payload=payload)
    meta = _webhook_meta(store, feed_id)
    assert meta["consecutive_failures"] == 0
    assert meta["paused"] is False


def test_store_pause_resume_statuses_and_owner_gate():
    store, feed_id, _ = _store_with_webhook(owner_token=TOKEN)
    status, meta = store.set_webhook_paused(
        feed_id, WID, True, owner_token=TOKEN)
    assert status == "ok" and meta["paused"] is True
    status, _ = store.set_webhook_paused(feed_id, WID, False)
    assert status == "forbidden"
    status, _ = store.set_webhook_paused(
        feed_id, WID, False, owner_token=WRONG)
    assert status == "forbidden"
    # A failed delivery while paused by hand, then resume clears.
    store.record_deliveries(
        feed_id, [_record("failed")],
        payload={"event": "multi_feed.updated", "version": 2})
    status, meta = store.set_webhook_paused(
        feed_id, WID, False, owner_token=TOKEN)
    assert status == "ok"
    assert meta["paused"] is False
    assert meta["consecutive_failures"] == 0
    status, _ = store.set_webhook_paused(
        feed_id, "no-such-webhook", True, owner_token=TOKEN)
    assert status == "webhook_not_found"
    status, _ = store.set_webhook_paused(
        "no-such-feed", WID, True, owner_token=TOKEN)
    assert status == "not_found"


def test_store_paused_state_persists_and_malformed_coerced(tmp_path):
    store, feed_id, _ = _store_with_webhook(tmp_path)
    payload = {"event": "multi_feed.updated", "version": 2}
    for _ in range(PAUSE_AFTER_CONSECUTIVE_FAILURES):
        store.record_deliveries(feed_id, [_record("failed")],
                                payload=payload)
    reloaded = MultiTimelineFeedStore(path=tmp_path / "feeds.json")
    meta = _webhook_meta(reloaded, feed_id)
    assert meta["paused"] is True
    assert meta["consecutive_failures"] == \
        PAUSE_AFTER_CONSECUTIVE_FAILURES
    # Malformed stored health fields coerce to the defaults.
    data = json.loads((tmp_path / "feeds.json").read_text())
    data["entries"][0]["webhooks"][0]["paused"] = "yes"
    data["entries"][0]["webhooks"][0]["consecutive_failures"] = -2
    (tmp_path / "feeds.json").write_text(json.dumps(data))
    coerced = MultiTimelineFeedStore(path=tmp_path / "feeds.json")
    meta = _webhook_meta(coerced, feed_id)
    assert meta["paused"] is False
    assert meta["consecutive_failures"] == 0


# -- HTTP flow ----------------------------------------------------------


class _Transport:
    """Injectable webhook transport with a switchable status."""

    def __init__(self, status=500):
        self.status = status
        self.calls = []

    def __call__(self, url, body, timeout):
        self.calls.append((url, body))
        return self.status, b""


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


def _post(url, obj=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, method="POST",
                    body=json.dumps(obj).encode() if obj is not None
                    else b"", headers=headers)


def _get(url, token=None):
    headers = {}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, headers=headers)


def _make_feed(base, transport, token=None):
    status, body = _post(f"{base}/api/multi-feeds", _payload(),
                         token=token)
    assert status == 201
    return json.loads(body)["feed_id"]


def test_http_auto_pause_skip_resume_flow():
    transport = _Transport(status=500)
    handler = make_handler(TastePilotAgent(),
                           webhook_transport=transport,
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _make_feed(base, transport)
        # Registration ping fails: failure #1.
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                             {"url": URL, "secret": None})
        assert status == 201
        created = json.loads(body)
        assert created["delivery"]["status"] == "failed"
        assert created["paused"] is False
        wid = created["webhook_id"]
        # Two update pushes fail: #2 and #3 — the third pauses it.
        for _ in range(2):
            status, _ = _post(f"{base}/api/multi-feeds/{feed_id}",
                              _payload())
            assert status == 200
        status, body = _get(f"{base}/api/multi-feeds/{feed_id}/webhooks")
        webhooks = json.loads(body)["webhooks"]
        assert webhooks[0]["paused"] is True
        assert webhooks[0]["consecutive_failures"] == \
            PAUSE_AFTER_CONSECUTIVE_FAILURES
        calls_before = len(transport.calls)
        # The next update is recorded as skipped, never attempted.
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        assert status == 200
        assert len(transport.calls) == calls_before
        update = json.loads(body)
        assert update["notifications"][0]["status"] == "skipped"
        assert update["notifications"][0]["error"] == "webhook_paused"
        # Resume clears the count; pushes flow again.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/resume")
        assert status == 200
        resumed = json.loads(body)
        assert resumed["paused"] is False
        assert resumed["consecutive_failures"] == 0
        transport.status = 200
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        assert status == 200
        assert json.loads(body)["notifications"][0]["status"] == \
            "delivered"
    finally:
        server.shutdown()


def test_http_pause_resume_gates():
    transport = _Transport(status=200)
    handler = make_handler(TastePilotAgent(),
                           webhook_transport=transport,
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _make_feed(base, transport, token=TOKEN)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                             {"url": URL, "secret": None}, token=TOKEN)
        assert status == 201
        wid = json.loads(body)["webhook_id"]
        # Owner rule: no token and wrong token both 403.
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/pause")
        assert status == 403
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/pause",
            token=WRONG)
        assert status == 403
        # Unknown webhook and unknown feed are 404s.
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/nope/pause",
            token=TOKEN)
        assert status == 404
        status, _ = _post(
            f"{base}/api/multi-feeds/nope/webhooks/{wid}/pause",
            token=TOKEN)
        assert status == 404
        # The owner's manual pause works and is reported.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/pause",
            token=TOKEN)
        assert status == 200
        assert json.loads(body)["paused"] is True
    finally:
        server.shutdown()


def test_http_redelivery_refused_while_paused_then_works():
    transport = _Transport(status=500)
    handler = make_handler(TastePilotAgent(),
                           webhook_transport=transport,
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _make_feed(base, transport)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                             {"url": URL, "secret": None})
        assert status == 201
        wid = json.loads(body)["webhook_id"]
        ping_id = json.loads(body)["delivery"]["delivery_id"]
        for _ in range(2):  # failures #2 and #3 pause the webhook
            status, _ = _post(f"{base}/api/multi-feeds/{feed_id}",
                              _payload())
            assert status == 200
        # Single redelivery to a paused webhook: categorised 400.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/notifications/"
            f"{ping_id}/redeliver")
        assert status == 400
        assert b"webhook_paused" in body
        # Bulk redelivery skips it with the same reason.
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/notifications/"
            "redeliver-failed")
        assert status == 200
        bulk = json.loads(body)
        assert bulk["attempted"] == 0
        assert {s["reason"] for s in bulk["skipped"]} == \
            {"webhook_paused"}
        # After a resume, the same record redelivers fine.
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/resume")
        assert status == 200
        transport.status = 200
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/notifications/"
            f"{ping_id}/redeliver")
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
    finally:
        server.shutdown()


def test_http_skipped_push_stays_redeliverable_after_resume():
    transport = _Transport(status=500)
    handler = make_handler(TastePilotAgent(),
                           webhook_transport=transport,
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _make_feed(base, transport)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                             {"url": URL, "secret": None})
        wid = json.loads(body)["webhook_id"]
        for _ in range(2):  # pause it
            _post(f"{base}/api/multi-feeds/{feed_id}", _payload())
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        skipped = json.loads(body)["notifications"][0]
        assert skipped["status"] == "skipped"
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/resume")
        assert status == 200
        transport.status = 200
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/notifications/"
            f"{skipped['delivery_id']}/redeliver")
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
    finally:
        server.shutdown()


# -- wiring -------------------------------------------------------------


def test_ui_and_docs_wiring():
    html = INDEX.read_text()
    assert "webhookresumebtn" in html
    assert "/resume" in html
    assert "webhook_paused" in html
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "paused" in text
    assert "Resume paused webhooks" in html
