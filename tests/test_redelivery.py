"""On-demand redelivery of failed push deliveries.

Doctrine under test: a failed webhook delivery is recorded with
a ``delivery_id`` and the exact payload it attempted, so the
subscriber can have it resent on demand instead of the push
being lost. The resend goes to the webhook's CURRENTLY
registered URL, is recorded as a new delivery whose
``redelivery_of`` names the original, and never edits the
original record — the log is an audit trail. A delivered
record, a payload-less legacy record, and a removed webhook
are categorised refusals; management follows the feed's owner
rule exactly like every other webhook surface.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (build_notification, delivery_id_for,
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
TOKEN = "redelivery-owner-token-1234567890"
WRONG = "redelivery-other-token-9876543210"


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


def _post(url, obj=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, method="POST",
                    body=json.dumps(obj).encode() if obj is not None
                    else b"", headers=headers)


class _Receiver:
    """A loopback webhook receiver whose status can flip."""

    def __init__(self, status=200):
        self.status = status
        self.hits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.hits.append(
                    (self.path, self.rfile.read(length)))
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


def _record(url, status="failed", payload_marker=1):
    return {"webhook_id": webhook_id_for(url),
            "host": "127.0.0.1:9", "event": "multi_feed.updated",
            "version": 2, "status": status,
            "attempts": 3 if status == "failed" else 1,
            "http_status": 500 if status == "failed" else 200,
            "error": "http_error (HTTP 500)"
            if status == "failed" else None}


# -- notify.py units ---------------------------------------------------------


def test_delivery_id_deterministic_and_unique():
    args = ("feed1", "wh1", "multi_feed.updated", 2, "2026-01-01T00:00:00+00:00")
    assert delivery_id_for(*args, 0) == delivery_id_for(*args, 0)
    assert delivery_id_for(*args, 0) != delivery_id_for(*args, 1)
    assert delivery_id_for("feed2", *args[1:], 0) != \
        delivery_id_for(*args, 0)
    assert len(delivery_id_for(*args, 0)) == 16


# -- the store ---------------------------------------------------------------


def _store_with_failed_delivery(path=None):
    store = MultiTimelineFeedStore(path=path, clock=FakeClock())
    feed_id = store.create(_payload())["feed_id"]
    url = "http://127.0.0.1:9/hook"
    store.add_webhook(feed_id, url)
    payload = build_notification(feed_id, "updated", 2, "etagx",
                                 ["Japan-France"], True,
                                 "2026-01-01T00:00:00+00:00")
    stamped = store.record_deliveries(
        feed_id, [_record(url)], payload=payload)
    return store, feed_id, url, stamped[0]


def test_store_stamps_delivery_id_and_payload():
    store, feed_id, _url, record = _store_with_failed_delivery()
    assert record["delivery_id"]
    assert record["payload"]["event"] == "multi_feed.updated"
    assert record["payload"]["version"] == 2
    assert record["redelivery_of"] is None
    status, log = store.deliveries(feed_id)
    assert status == "ok" and log == [record]
    status, target = store.redelivery_target(
        feed_id, record["delivery_id"])
    assert status == "ok"
    assert target["payload"] == record["payload"]
    assert target["webhook"]["url"] == "http://127.0.0.1:9/hook"
    assert target["record"]["delivery_id"] == record["delivery_id"]


def test_store_redelivery_refusals():
    store, feed_id, url, record = _store_with_failed_delivery()
    did = record["delivery_id"]
    assert store.redelivery_target(feed_id, "nope")[0] == \
        "delivery_not_found"
    assert store.redelivery_target("missing", did)[0] == "not_found"
    # A delivered record cannot be redelivered.
    stamped = store.record_deliveries(
        feed_id, [_record(url, status="delivered")],
        payload=record["payload"])
    assert store.redelivery_target(
        feed_id, stamped[0]["delivery_id"])[0] == "already_delivered"
    # A legacy record stamped without a payload cannot be either.
    legacy = store.record_deliveries(feed_id, [_record(url)])
    assert legacy[0]["payload"] is None
    assert store.redelivery_target(
        feed_id, legacy[0]["delivery_id"])[0] == "not_redeliverable"
    # Once the webhook is removed there is no address to resend to.
    store.remove_webhook(feed_id, webhook_id_for(url))
    assert store.redelivery_target(feed_id, did)[0] == \
        "webhook_not_found"


def test_store_redelivery_owner_gate_and_persistence(tmp_path):
    path = tmp_path / "multifeeds.json"
    store = MultiTimelineFeedStore(path=path, clock=FakeClock())
    feed_id = store.create(_payload(), owner_token=TOKEN)["feed_id"]
    url = "http://127.0.0.1:9/hook"
    store.add_webhook(feed_id, url, owner_token=TOKEN)
    payload = build_notification(feed_id, "updated", 2, "e",
                                 ["Japan-France"], True, "t")
    stamped = store.record_deliveries(
        feed_id, [_record(url)], payload=payload)
    did = stamped[0]["delivery_id"]
    assert store.redelivery_target(feed_id, did)[0] == "forbidden"
    assert store.redelivery_target(
        feed_id, did, owner_token=WRONG)[0] == "forbidden"
    assert store.redelivery_target(
        feed_id, did, owner_token=TOKEN)[0] == "ok"
    # The stamped id + payload survive a restart in file mode.
    revived = MultiTimelineFeedStore(path=path, clock=FakeClock())
    status, target = revived.redelivery_target(
        feed_id, did, owner_token=TOKEN)
    assert status == "ok" and target["payload"]["version"] == 2
    assert TOKEN not in path.read_text(encoding="utf-8")


# -- the HTTP surface ----------------------------------------------------------


def _handler():
    return make_handler(TastePilotAgent(),
                        webhook_sleeper=lambda *_: None)


def test_http_redeliver_failed_ping_end_to_end():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        feed_id = json.loads(body)["feed_id"]
        hooks_url = f"{base_url}/api/multi-feeds/{feed_id}/webhooks"
        status, _h, body = _post(hooks_url, {"url": receiver.url})
        created = json.loads(body)
        assert status == 201
        assert created["delivery"]["status"] == "failed"
        original_id = created["delivery"]["delivery_id"]
        assert original_id
        assert created["delivery"]["payload"]["event"] == \
            "multi_feed.subscribed"
        hits_before = len(receiver.hits)
        assert hits_before == 3  # bounded retries on the 500
        # The receiver recovers; the subscriber redelivers.
        receiver.status = 200
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications/"
            f"{original_id}/redeliver")
        assert status == 200
        resent = json.loads(body)
        assert resent["redelivery_of"] == original_id
        assert resent["delivery"]["status"] == "delivered"
        assert resent["delivery"]["redelivery_of"] == original_id
        assert resent["delivery"]["delivery_id"] != original_id
        # The resent body is byte-identical to the failed attempt's.
        assert receiver.hits[-1][1] == receiver.hits[0][1]
        # The log is an audit trail: original still failed, first.
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        log = json.loads(body)["deliveries"]
        assert [d["status"] for d in log] == ["failed", "delivered"]
        assert log[0]["delivery_id"] == original_id
        assert log[0]["redelivery_of"] is None
        # A delivered record cannot be redelivered again.
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications/"
            f"{resent['delivery']['delivery_id']}/redeliver")
        assert status == 400 and b"already_delivered" in body
    finally:
        server.shutdown()
        receiver.close()


def test_http_redeliver_gates():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        feed_id = json.loads(body)["feed_id"]
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url})
        did = json.loads(body)["delivery"]["delivery_id"]
        base = f"{base_url}/api/multi-feeds/{feed_id}/notifications"
        status, _h, _b = _post(f"{base}/nope/redeliver")
        assert status == 404
        status, _h, _b = _post(
            f"{base_url}/api/multi-feeds/nope/notifications/"
            f"{did}/redeliver")
        assert status == 404
        # Removing the webhook leaves nothing to resend to.
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks")
        webhook_id = json.loads(body)["webhooks"][0]["webhook_id"]
        status, _h, _b = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks/"
            f"{webhook_id}", method="DELETE")
        assert status == 200
        status, _h, body = _post(f"{base}/{did}/redeliver")
        assert status == 400 and b"webhook_not_found" in body
    finally:
        server.shutdown()
        receiver.close()


def test_http_redeliver_owner_flow():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload(), token=TOKEN)
        feed_id = json.loads(body)["feed_id"]
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url}, token=TOKEN)
        did = json.loads(body)["delivery"]["delivery_id"]
        url = (f"{base_url}/api/multi-feeds/{feed_id}/notifications/"
               f"{did}/redeliver")
        for bad_token in (None, WRONG):
            status, _h, _b = _post(url, token=bad_token)
            assert status == 403
        receiver.status = 200
        status, _h, body = _post(url, token=TOKEN)
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Redeliver failed pushes" in html
    assert "/redeliver" in html
    readme = README.read_text(encoding="utf-8")
    assert "redeliver" in readme and "redelivery_of" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "redelivered on demand" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "Redelivery probe" in deploy
