"""Webhook test pushes: a webhook is test-pushed on demand.

Doctrine under test: until now the only test delivery a
webhook ever received was the registration ping — a
subscriber who rotated an endpoint, rotated a signing
secret, or replaced an event filter had no way to verify
the endpoint worked (and that the signature verifies)
without forcing a feed update. Now ``POST
/api/multi-feeds/<id>/webhooks/<webhook_id>/test`` fires
one ``multi_feed.test`` delivery to that webhook alone,
carrying the feed's current version, etag, timeline
names, and recomputed verdict. The test is the
subscriber's explicit act, so — like an on-demand
redelivery — it is sent regardless of the webhook's event
filter (the filter governs automatic pushes only); a
paused webhook is refused (``webhook_paused``), exactly
as for redelivery. The delivery is recorded like any
push and counts toward the webhook's delivery health
like any push, so testing a still-failing endpoint can
itself reach the auto-pause threshold.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (WEBHOOK_EVENTS, build_notification,
                               validate_webhook_events,
                               verify_signature, webhook_id_for)
from tastepilot.server import make_handler
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


def _timeline(name, text=DATED):
    base = TastePilotAgent().run(text)
    return {"name": name, "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _payload():
    return {"timelines": [_timeline("Japan-France")]}


# -- notify-level ---------------------------------------------------------


def test_test_event_is_a_first_class_event():
    assert "multi_feed.test" in WEBHOOK_EVENTS
    assert validate_webhook_events(["multi_feed.test"]) == \
        ["multi_feed.test"]
    payload = build_notification("feed1", "test", 3, "etag9",
                                 ["Japan-France"], True, "t")
    assert payload["event"] == "multi_feed.test"
    assert payload["version"] == 3 and payload["etag"] == "etag9"
    assert payload["timelines"] == ["Japan-France"]
    assert payload["all_passed"] is True


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


def _deliveries(base, feed_id, token=None):
    status, body = _get(f"{base}/api/multi-feeds/{feed_id}"
                        "/notifications", token=token)
    assert status == 200
    return json.loads(body)["deliveries"]


def _test_url(base, feed_id, wid):
    return f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/test"


def test_http_test_push_delivers_signed_test_event_and_records_it():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        reg = _register(base, feed_id, receiver, secret=SUPPLIED)
        wid = reg["webhook_id"]
        assert receiver.events_seen() == ["multi_feed.subscribed"]
        status, body = _post(_test_url(base, feed_id, wid))
        assert status == 200
        result = json.loads(body)
        delivery = result["delivery"]
        assert delivery["event"] == "multi_feed.test"
        assert delivery["status"] == "delivered"
        assert delivery["signed"] is True
        assert result["webhook_id"] == wid
        # The receiver saw exactly the test event, signed under
        # the webhook's secret over the exact body bytes.
        assert receiver.events_seen() == ["multi_feed.subscribed",
                                          "multi_feed.test"]
        headers, raw = receiver.hits[-1]
        lowered = {k.lower(): v for k, v in headers.items()}
        assert verify_signature(SUPPLIED, raw,
                                lowered["x-tastepilot-signature"])
        payload = json.loads(raw)
        assert payload["version"] == 1
        assert payload["timelines"] == ["Japan-France"]
        assert payload["all_passed"] is True
        assert payload["etag"]
        # Recorded like any push.
        deliveries = _deliveries(base, feed_id)
        assert [d["event"] for d in deliveries] == [
            "multi_feed.subscribed", "multi_feed.test"]
    finally:
        server.shutdown()
        receiver.close()


def test_http_test_push_ignores_the_event_filter():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        # Filtered to updates only: no registration ping at all.
        reg = _register(base, feed_id, receiver,
                        events=["multi_feed.updated"])
        assert reg["delivery"] is None and receiver.hits == []
        status, body = _post(_test_url(base, feed_id,
                                       reg["webhook_id"]))
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
        assert receiver.events_seen() == ["multi_feed.test"]
    finally:
        server.shutdown()
        receiver.close()


def test_http_test_push_paused_refused_then_resume_delivers():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        reg = _register(base, feed_id, receiver)
        wid = reg["webhook_id"]
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/pause")
        assert status == 200
        hits = len(receiver.hits)
        status, body = _post(_test_url(base, feed_id, wid))
        assert status == 400 and b"webhook_paused" in body
        assert len(receiver.hits) == hits  # nothing attempted
        assert len(_deliveries(base, feed_id)) == 1  # nothing recorded
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}/resume")
        assert status == 200
        status, body = _post(_test_url(base, feed_id, wid))
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
    finally:
        server.shutdown()
        receiver.close()


def test_http_failed_tests_count_toward_auto_pause():
    receiver = _Receiver(status=500)
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base)
        reg = _register(base, feed_id, receiver)  # ping fails: count 1
        wid = reg["webhook_id"]
        assert reg["consecutive_failures"] == 0  # create meta predates it
        status, body = _post(_test_url(base, feed_id, wid))
        assert status == 200
        result = json.loads(body)
        assert result["delivery"]["status"] == "failed"
        assert result["consecutive_failures"] == 2  # refreshed meta
        status, body = _post(_test_url(base, feed_id, wid))
        assert status == 200
        result = json.loads(body)
        assert result["consecutive_failures"] == 3
        assert result["paused"] is True
        assert result["paused_reason"] == "auto"
        # Now paused: the next test is refused, not attempted.
        status, body = _post(_test_url(base, feed_id, wid))
        assert status == 400 and b"webhook_paused" in body
    finally:
        server.shutdown()
        receiver.close()


def test_http_test_gates():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id = _feed(base, token=TOKEN)
        reg = _register(base, feed_id, receiver, token=TOKEN)
        wid = reg["webhook_id"]
        status, _ = _post(_test_url(base, feed_id, wid))  # no token
        assert status == 403
        status, _ = _post(_test_url(base, feed_id, wid), token=WRONG)
        assert status == 403
        status, _ = _post(_test_url(base, feed_id, "nope"), token=TOKEN)
        assert status == 404
        status, _ = _post(_test_url(base, "nope", wid), token=TOKEN)
        assert status == 404
        status, _ = _post(_test_url(base, feed_id, wid),
                          token="bad token with spaces")
        assert status == 400
        # The owner can still test after all of that.
        status, body = _post(_test_url(base, feed_id, wid), token=TOKEN)
        assert status == 200
        assert json.loads(body)["delivery"]["status"] == "delivered"
        assert webhook_id_for(receiver.url) == wid
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_carry_test_pushes():
    html = INDEX.read_text()
    assert "/test" in html
    assert "Test webhooks" in html
    assert "webhooktestbtn" in html
    for doc in (README, SUBMISSION, DEPLOY):
        assert "/test" in doc.read_text()
    assert "multi_feed.test" in README.read_text()
    assert "multi_feed.test" in SUBMISSION.read_text()
