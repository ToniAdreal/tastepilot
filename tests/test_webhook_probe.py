"""Webhook half-open probe: an auto-pause is a circuit breaker.

Doctrine under test: run 43 paused a failing webhook until the
subscriber resumed it — correct for a deliberate pause, wrong
as a life sentence for a transient outage. An auto-pause is
now stamped with its reason and moment, and after
PAUSE_COOLDOWN_SECONDS the next update push probes the
endpoint with that push itself: a delivered probe revives the
webhook (count cleared, pause provenance gone), a failed
probe restarts the cooldown from that moment. A webhook
paused by hand (reason "manual") is never probed — a
deliberate pause is an instruction, not a symptom — and a
legacy paused row with no stamped reason is never probed
either. Listings report paused_reason / paused_at /
probe_eligible_at so a subscriber can see when the probe
will happen.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (PAUSE_AFTER_CONSECUTIVE_FAILURES,
                               PAUSE_COOLDOWN_SECONDS,
                               webhook_id_for)
from tastepilot.server import MultiTimelineFeedStore, _iso, make_handler
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
TOKEN = "probe-" + "owner-token-0123456789abcdef"
URL = "https://hooks.example.com/push"
WID = webhook_id_for(URL)
BASE_EPOCH = 1_800_000_000.0


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


class _Clock:
    def __init__(self, start=BASE_EPOCH):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _store_with_webhook(clock, tmp_path=None, owner_token=None):
    path = tmp_path / "feeds.json" if tmp_path else None
    store = MultiTimelineFeedStore(path=path, clock=clock)
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


def _fail(store, feed_id, times=PAUSE_AFTER_CONSECUTIVE_FAILURES):
    payload = {"event": "multi_feed.updated", "version": 2}
    for _ in range(times):
        store.record_deliveries(feed_id, [_record("failed")],
                                payload=payload)


# -- store-level breaker state ------------------------------------------


def test_auto_pause_is_stamped_and_probe_due_after_cooldown():
    clock = _Clock()
    store, feed_id, wmeta = _store_with_webhook(clock)
    assert wmeta["paused_reason"] is None
    assert wmeta["paused_at"] is None
    assert wmeta["probe_eligible_at"] is None
    _fail(store, feed_id)
    meta = _webhook_meta(store, feed_id)
    assert meta["paused"] is True
    assert meta["paused_reason"] == "auto"
    assert meta["paused_at"] == _iso(BASE_EPOCH)
    assert meta["probe_eligible_at"] == \
        _iso(BASE_EPOCH + PAUSE_COOLDOWN_SECONDS)
    assert store.probe_due_ids(feed_id) == []
    clock.advance(PAUSE_COOLDOWN_SECONDS - 1)
    assert store.probe_due_ids(feed_id) == []
    clock.advance(1)
    assert store.probe_due_ids(feed_id) == [WID]
    assert store.probe_due_ids("no-such-feed") == []


def test_failed_probe_restarts_cooldown():
    clock = _Clock()
    store, feed_id, _ = _store_with_webhook(clock)
    _fail(store, feed_id)
    clock.advance(PAUSE_COOLDOWN_SECONDS)
    assert store.probe_due_ids(feed_id) == [WID]
    # The probe attempt fails: recorded while auto-paused.
    store.record_deliveries(
        feed_id, [_record("failed")],
        payload={"event": "multi_feed.updated", "version": 2})
    meta = _webhook_meta(store, feed_id)
    assert meta["paused"] is True
    assert meta["paused_reason"] == "auto"
    assert meta["paused_at"] == \
        _iso(BASE_EPOCH + PAUSE_COOLDOWN_SECONDS)
    assert meta["probe_eligible_at"] == \
        _iso(BASE_EPOCH + 2 * PAUSE_COOLDOWN_SECONDS)
    assert store.probe_due_ids(feed_id) == []


def test_delivered_probe_revives_webhook():
    clock = _Clock()
    store, feed_id, _ = _store_with_webhook(clock)
    _fail(store, feed_id)
    clock.advance(PAUSE_COOLDOWN_SECONDS)
    store.record_deliveries(
        feed_id, [_record("delivered")],
        payload={"event": "multi_feed.updated", "version": 2})
    meta = _webhook_meta(store, feed_id)
    assert meta["paused"] is False
    assert meta["consecutive_failures"] == 0
    assert meta["paused_reason"] is None
    assert meta["paused_at"] is None
    assert meta["probe_eligible_at"] is None


def test_manual_pause_is_never_probe_due():
    clock = _Clock()
    store, feed_id, _ = _store_with_webhook(clock, owner_token=TOKEN)
    status, meta = store.set_webhook_paused(
        feed_id, WID, True, owner_token=TOKEN)
    assert status == "ok"
    assert meta["paused_reason"] == "manual"
    assert meta["paused_at"] == _iso(BASE_EPOCH)
    assert meta["probe_eligible_at"] is None
    clock.advance(10 * PAUSE_COOLDOWN_SECONDS)
    assert store.probe_due_ids(feed_id) == []
    # Converting an auto pause by hand makes it manual too.
    store2_clock = _Clock()
    store2, feed2, _ = _store_with_webhook(store2_clock)
    _fail(store2, feed2)
    status, meta = store2.set_webhook_paused(feed2, WID, True)
    assert status == "ok" and meta["paused_reason"] == "manual"
    store2_clock.advance(10 * PAUSE_COOLDOWN_SECONDS)
    assert store2.probe_due_ids(feed2) == []


def test_probe_state_persists_and_malformed_coerced(tmp_path):
    clock = _Clock()
    store, feed_id, _ = _store_with_webhook(clock, tmp_path)
    _fail(store, feed_id)
    reloaded = MultiTimelineFeedStore(path=tmp_path / "feeds.json",
                                      clock=clock)
    meta = _webhook_meta(reloaded, feed_id)
    assert meta["paused_reason"] == "auto"
    assert meta["paused_at"] == _iso(BASE_EPOCH)
    clock.advance(PAUSE_COOLDOWN_SECONDS)
    assert reloaded.probe_due_ids(feed_id) == [WID]
    # A bogus stored reason/moment coerces away — and a row
    # with no valid "auto" stamp is never probe-eligible.
    data = json.loads((tmp_path / "feeds.json").read_text())
    data["entries"][0]["webhooks"][0]["paused_reason"] = "bogus"
    data["entries"][0]["webhooks"][0]["paused_at"] = "not-a-date"
    (tmp_path / "feeds.json").write_text(json.dumps(data))
    coerced = MultiTimelineFeedStore(path=tmp_path / "feeds.json",
                                     clock=clock)
    meta = _webhook_meta(coerced, feed_id)
    assert meta["paused"] is True
    assert meta["paused_reason"] is None
    assert meta["paused_at"] is None
    assert meta["probe_eligible_at"] is None
    assert coerced.probe_due_ids(feed_id) == []


# -- HTTP flow ------------------------------------------------------------


class _Transport:
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


def _paused_feed(base, transport, clock):
    status, body = _post(f"{base}/api/multi-feeds", _payload())
    assert status == 201
    feed_id = json.loads(body)["feed_id"]
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                         {"url": URL, "secret": None})
    assert status == 201
    wid = json.loads(body)["webhook_id"]
    for _ in range(PAUSE_AFTER_CONSECUTIVE_FAILURES - 1):
        status, _ = _post(f"{base}/api/multi-feeds/{feed_id}",
                          _payload())
        assert status == 200
    status, body = _get(f"{base}/api/multi-feeds/{feed_id}/webhooks")
    meta = json.loads(body)["webhooks"][0]
    assert meta["paused"] is True and meta["paused_reason"] == "auto"
    return feed_id, wid, meta


def test_http_probe_flow_failed_then_delivered():
    import time as _time
    clock = _Clock(_time.time())
    transport = _Transport(status=500)
    handler = make_handler(TastePilotAgent(),
                           webhook_transport=transport,
                           webhook_sleeper=lambda s: None,
                           multi_feed_clock=clock)
    server, base = _serve(handler)
    try:
        feed_id, wid, meta = _paused_feed(base, transport, clock)
        assert meta["probe_eligible_at"] is not None
        # Before the cooldown: skipped, receiver untouched.
        calls = len(transport.calls)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        assert status == 200
        note = json.loads(body)["notifications"][0]
        assert note["status"] == "skipped" and "probe" not in note
        assert len(transport.calls) == calls
        # At the cooldown the next update probes — and fails.
        # (One second past it, not exactly on it: the clock
        # starts at a real time.time(), whose microsecond
        # round-trip through the ISO paused_at stamp can land
        # a fraction of a microsecond past the start value,
        # which made an exact-cooldown advance a coin flip —
        # the exact boundary itself is pinned deterministically
        # by the store-level test on integer epochs above.)
        clock.advance(PAUSE_COOLDOWN_SECONDS + 1)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        assert status == 200
        note = json.loads(body)["notifications"][0]
        assert note["status"] == "failed" and note["probe"] is True
        assert len(transport.calls) > calls
        status, body = _get(f"{base}/api/multi-feeds/{feed_id}/webhooks")
        meta = json.loads(body)["webhooks"][0]
        assert meta["paused"] is True
        # The failed probe restarted the cooldown: skipped again.
        calls = len(transport.calls)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        assert json.loads(body)["notifications"][0]["status"] == \
            "skipped"
        assert len(transport.calls) == calls
        # After the second cooldown a delivered probe revives it.
        # (Same one-second slack as above, for the same reason:
        # the failed probe re-stamped paused_at from this clock.)
        clock.advance(PAUSE_COOLDOWN_SECONDS + 1)
        transport.status = 200
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        note = json.loads(body)["notifications"][0]
        assert note["status"] == "delivered" and note["probe"] is True
        status, body = _get(f"{base}/api/multi-feeds/{feed_id}/webhooks")
        meta = json.loads(body)["webhooks"][0]
        assert meta["paused"] is False
        assert meta["consecutive_failures"] == 0
        assert meta["paused_reason"] is None
    finally:
        server.shutdown()


def test_http_manual_pause_never_probed():
    import time as _time
    clock = _Clock(_time.time())
    transport = _Transport(status=200)
    handler = make_handler(TastePilotAgent(),
                           webhook_transport=transport,
                           webhook_sleeper=lambda s: None,
                           multi_feed_clock=clock)
    server, base = _serve(handler)
    try:
        status, body = _post(f"{base}/api/multi-feeds", _payload())
        feed_id = json.loads(body)["feed_id"]
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks",
            {"url": URL, "secret": None})
        wid = json.loads(body)["webhook_id"]
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/pause")
        assert status == 200
        meta = json.loads(body)
        assert meta["paused_reason"] == "manual"
        assert meta["probe_eligible_at"] is None
        clock.advance(10 * PAUSE_COOLDOWN_SECONDS)
        calls = len(transport.calls)
        status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                             _payload())
        assert status == 200
        note = json.loads(body)["notifications"][0]
        assert note["status"] == "skipped" and "probe" not in note
        assert len(transport.calls) == calls
    finally:
        server.shutdown()


# -- wiring -----------------------------------------------------------------


def test_ui_and_docs_wiring():
    html = INDEX.read_text()
    assert "probe_eligible_at" in html
    assert "auto-paused" in html
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "probe" in text.lower()
    assert "half-open" in README.read_text()
    assert "half-open" in SUBMISSION.read_text()
