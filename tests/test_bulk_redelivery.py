"""Bulk redelivery of a multi-feed's failed push deliveries.

Doctrine under test: one call resends every still-actionable
failed delivery on a feed — the LATEST attempt of each failed
chain — instead of the subscriber resending records one by
one. A failed record a later resend already superseded is
reported under ``skipped`` and never resent (resending it
would duplicate a push its chain has moved past); a
payload-less legacy record and a record whose webhook was
removed are skipped with their categorised reasons; delivered
records are simply done. Each resend is recorded exactly as a
single redelivery is — a new delivery whose ``redelivery_of``
names the record it resends — and the feed's owner rule
applies exactly as for every other webhook surface.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import build_notification, webhook_id_for
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
TOKEN = "bulk-redelivery-owner-" + "token-1234567890"
WRONG = "bulk-other-token-9876543210fedcba"


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


URL = "http://127.0.0.1:9/hook"


def _record(url, status="failed", version=2):
    return {"webhook_id": webhook_id_for(url),
            "host": "127.0.0.1:9", "event": "multi_feed.updated",
            "version": version, "status": status,
            "attempts": 3 if status == "failed" else 1,
            "http_status": 500 if status == "failed" else 200,
            "error": "http_error (HTTP 500)"
            if status == "failed" else None}


def _payload_for(feed_id, version=2):
    return build_notification(feed_id, "updated", version, "etagx",
                              ["Japan-France"], True,
                              "2026-01-01T00:00:00+00:00")


def _store_with_feed():
    store = MultiTimelineFeedStore(clock=FakeClock())
    feed_id = store.create(_payload())["feed_id"]
    store.add_webhook(feed_id, URL)
    return store, feed_id


# -- the store ---------------------------------------------------


def test_store_selects_latest_attempt_of_each_chain():
    store, feed_id = _store_with_feed()
    payload = _payload_for(feed_id)
    first = store.record_deliveries(
        feed_id, [_record(URL)], payload=payload)[0]
    # A failed resend of the first record supersedes it.
    second = store.record_deliveries(
        feed_id, [_record(URL)], payload=payload,
        redelivery_of=first["delivery_id"])[0]
    # An unrelated failed record (another event) stands alone.
    other_payload = _payload_for(feed_id, version=3)
    other = store.record_deliveries(
        feed_id, [_record(URL, version=3)], payload=other_payload)[0]
    status, result = store.failed_redelivery_targets(feed_id)
    assert status == "ok"
    assert [t["record"]["delivery_id"] for t in result["targets"]] == \
        [second["delivery_id"], other["delivery_id"]]
    assert result["targets"][0]["payload"] == payload
    assert result["targets"][0]["webhook"]["url"] == URL
    assert result["skipped"] == [{
        "delivery_id": first["delivery_id"], "reason": "superseded",
        "superseded_by": second["delivery_id"]}]


def test_store_skips_legacy_and_removed_webhook_records():
    store, feed_id = _store_with_feed()
    payload = _payload_for(feed_id)
    legacy = store.record_deliveries(feed_id, [_record(URL)])[0]
    gone = store.record_deliveries(
        feed_id, [_record(URL)], payload=payload)[0]
    done = store.record_deliveries(
        feed_id, [_record(URL, status="delivered")], payload=payload)[0]
    store.remove_webhook(feed_id, webhook_id_for(URL))
    status, result = store.failed_redelivery_targets(feed_id)
    assert status == "ok"
    assert result["targets"] == []
    reasons = {s["delivery_id"]: s["reason"] for s in result["skipped"]}
    assert reasons == {legacy["delivery_id"]: "not_redeliverable",
                       gone["delivery_id"]: "webhook_not_found"}
    # The delivered record is neither a target nor a skip.
    assert done["delivery_id"] not in reasons


def test_store_bulk_owner_gate():
    store = MultiTimelineFeedStore(clock=FakeClock())
    feed_id = store.create(_payload(), owner_token=TOKEN)["feed_id"]
    assert store.failed_redelivery_targets(feed_id)[0] == "forbidden"
    assert store.failed_redelivery_targets(
        feed_id, owner_token=WRONG)[0] == "forbidden"
    assert store.failed_redelivery_targets(
        feed_id, owner_token=TOKEN)[0] == "ok"
    assert store.failed_redelivery_targets("missing")[0] == "not_found"


# -- the HTTP surface -----------------------------------------------


def _handler():
    return make_handler(TastePilotAgent(),
                        webhook_sleeper=lambda *_: None)


def _bulk_url(base_url, feed_id):
    return (f"{base_url}/api/multi-feeds/{feed_id}"
            f"/notifications/redeliver-failed")


def test_http_bulk_redelivers_all_failed_chains():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        feed_id = json.loads(body)["feed_id"]
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url})
        assert json.loads(body)["delivery"]["status"] == "failed"
        # A second failed chain: the update push also fails.
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}", _payload())
        assert status == 200
        receiver.status = 200
        status, _h, body = _post(_bulk_url(base_url, feed_id))
        assert status == 200
        result = json.loads(body)
        assert result["attempted"] == 2
        assert result["delivered"] == 2 and result["failed"] == 0
        assert result["skipped"] == []
        assert [r["event"] for r in result["results"]] == [
            "multi_feed.subscribed", "multi_feed.updated"]
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        log = json.loads(body)["deliveries"]
        assert [d["status"] for d in log] == [
            "failed", "failed", "delivered", "delivered"]
        assert log[2]["redelivery_of"] == log[0]["delivery_id"]
        assert log[3]["redelivery_of"] == log[1]["delivery_id"]
        # A second bulk call resends nothing: both failed records
        # are superseded by their delivered resends.
        status, _h, body = _post(_bulk_url(base_url, feed_id))
        result = json.loads(body)
        assert result["attempted"] == 0
        assert {s["reason"] for s in result["skipped"]} == {"superseded"}
        assert len(result["skipped"]) == 2
    finally:
        server.shutdown()
        receiver.close()


def test_http_bulk_gates_and_skips():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, _b = _post(_bulk_url(base_url, "nope"))
        assert status == 404
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        feed_id = json.loads(body)["feed_id"]
        # Nothing registered, nothing failed: a clean no-op.
        status, _h, body = _post(_bulk_url(base_url, feed_id))
        result = json.loads(body)
        assert status == 200 and result["attempted"] == 0
        assert result["skipped"] == []
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url})
        assert json.loads(body)["delivery"]["status"] == "failed"
        # Remove the webhook: the failed record is skipped, named.
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks")
        webhook_id = json.loads(body)["webhooks"][0]["webhook_id"]
        status, _h, _b = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks/"
            f"{webhook_id}", method="DELETE")
        assert status == 200
        status, _h, body = _post(_bulk_url(base_url, feed_id))
        result = json.loads(body)
        assert result["attempted"] == 0
        assert [s["reason"] for s in result["skipped"]] == [
            "webhook_not_found"]
    finally:
        server.shutdown()
        receiver.close()


def test_http_bulk_owner_flow():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload(), token=TOKEN)
        feed_id = json.loads(body)["feed_id"]
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url}, token=TOKEN)
        assert json.loads(body)["delivery"]["status"] == "failed"
        for bad_token in (None, WRONG):
            status, _h, _b = _post(_bulk_url(base_url, feed_id),
                                   token=bad_token)
            assert status == 403
        receiver.status = 200
        status, _h, body = _post(_bulk_url(base_url, feed_id),
                                 token=TOKEN)
        result = json.loads(body)
        assert status == 200 and result["delivered"] == 1
    finally:
        server.shutdown()
        receiver.close()


def test_http_bulk_still_failing_receiver_records_failed_resends():
    receiver = _Receiver(status=500)
    server, base_url = _serve(_handler())
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload())
        feed_id = json.loads(body)["feed_id"]
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url})
        status, _h, body = _post(_bulk_url(base_url, feed_id))
        result = json.loads(body)
        assert result["attempted"] == 1 and result["failed"] == 1
        assert result["delivered"] == 0
        # The failed resend is the chain's new latest attempt: a
        # later bulk call targets IT, not the original record.
        status, result2 = None, None
        status, _h, body = _post(_bulk_url(base_url, feed_id))
        result2 = json.loads(body)
        assert result2["attempted"] == 1
        assert result2["results"][0]["delivery_id"] == \
            result["results"][0]["new_delivery_id"]
        # …and the original record is reported superseded by it.
        assert [s["reason"] for s in result2["skipped"]] == \
            ["superseded"]
        assert result2["skipped"][0]["superseded_by"] == \
            result["results"][0]["new_delivery_id"]
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Redeliver failed pushes" in html
    assert "/notifications/redeliver-failed" in html
    readme = README.read_text(encoding="utf-8")
    assert "redeliver-failed" in readme and "superseded" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "redelivered in bulk" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "Bulk redelivery probe" in deploy
