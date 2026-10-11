"""Push change notifications via webhooks (SUBMISSION "What's
next", built in generic form).

Doctrine under test: change on a multi-timeline feed PUSHES to
subscriber-registered webhooks instead of only being polled.
A webhook URL is validated (http(s) only, no credentials in the
URL), stored only inside the feed entry (it is the delivery
address), and named everywhere else — responses, delivery
records, logs — by host label only, so a query string that
could carry a token never reaches a surface. Registration fires
one multi_feed.subscribed test delivery; every in-place update
fires multi_feed.updated to every registered webhook with
bounded retries on transient failures. Every outcome is a
delivery record in the feed's capped log, and a failed delivery
never fails the feed operation that triggered it. Webhook
management follows the feed's owner rule and persists with the
feed in file mode.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli  # noqa: F401  (serve-help pattern parity)
from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (MAX_WEBHOOKS_PER_FEED,
                               build_notification, deliver_notification,
                               deliver_to_webhooks, validate_webhook_url,
                               webhook_host_label, webhook_id_for)
from tastepilot.server import (MultiTimelineFeedStore, make_handler)
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
TOKEN = "webhook-owner-token-0123456789"
WRONG = "webhook-other-token-9876543210"
SECRET_URL = "http://127.0.0.1:9/hook/path?token=SENTINELTOKEN123"


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


def _post(url, obj, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, method="POST",
                    body=json.dumps(obj).encode(), headers=headers)


class _Receiver:
    """A real loopback webhook receiver recording what arrives."""

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
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/hook"

    def close(self):
        self.server.shutdown()


def _ok_transport(url, body, timeout):
    return 200, b"{}"


# -- notify.py units ---------------------------------------------------------


def test_validate_webhook_url_rules():
    assert validate_webhook_url(" https://example.org/hook ") == \
        "https://example.org/hook"
    for bad, category in (
            (None, "bad_webhook_url"), (123, "bad_webhook_url"),
            ("", "bad_webhook_url"),
            ("https://example.org/" + "x" * 600, "bad_webhook_url"),
            ("https://exa mple.org/h", "bad_webhook_url"),
            ("https://user:pass@example.org/h", "bad_webhook_url"),
            ("ftp://example.org/h", "unsupported"),
            ("example.org/h", "unsupported"),
            ("https:///no-host", "unsupported")):
        with pytest.raises(ValueError, match=category):
            validate_webhook_url(bad)
    assert webhook_host_label("https://example.org:8443/h?x=1") == \
        "example.org:8443"
    assert webhook_id_for("https://example.org/h") == \
        webhook_id_for("https://example.org/h")
    assert webhook_id_for("https://example.org/h") != \
        webhook_id_for("https://example.org/other")


def test_build_notification_shape():
    payload = build_notification("feed1", "updated", 3, "etagx",
                                 ["A", "B"], True, "2026-01-01T00:00:00+00:00")
    assert payload == {"event": "multi_feed.updated", "kind": "updated",
                       "feed_id": "feed1", "version": 3, "etag": "etagx",
                       "timelines": ["A", "B"], "all_passed": True,
                       "at": "2026-01-01T00:00:00+00:00"}
    assert build_notification("f", "subscribed", 1, "e", [], None,
                              "t")["event"] == "multi_feed.subscribed"


def test_deliver_success_and_host_only_record():
    seen = []

    def transport(url, body, timeout):
        seen.append((url, body))
        return 201, b""

    record = deliver_notification(SECRET_URL, {"event": "x", "version": 2},
                                  transport=transport)
    assert record["status"] == "delivered" and record["attempts"] == 1
    assert record["http_status"] == 201 and record["error"] is None
    assert record["host"] == "127.0.0.1:9"
    assert "SENTINELTOKEN123" not in json.dumps(record)
    assert seen[0][0] == SECRET_URL  # delivery itself uses the full URL
    assert json.loads(seen[0][1])["event"] == "x"


def test_deliver_retries_transient_then_succeeds():
    calls, sleeps = [], []

    def flaky(url, body, timeout):
        calls.append(1)
        return (503, b"") if len(calls) == 1 else (200, b"")

    record = deliver_notification(
        "https://example.org/h", {"event": "x"}, transport=flaky,
        sleeper=sleeps.append)
    assert record["status"] == "delivered" and record["attempts"] == 2
    assert sleeps == [0.5]


def test_deliver_exhaustion_and_non_transient():
    sleeps = []
    record = deliver_notification(
        "https://example.org/h", {"event": "x"},
        transport=lambda u, b, t: (503, b""), sleeper=sleeps.append)
    assert record["status"] == "failed" and record["attempts"] == 3
    assert record["http_status"] == 503 and sleeps == [0.5, 1.0]
    assert "http_error" in record["error"]
    calls = []
    record = deliver_notification(
        "https://example.org/h", {"event": "x"},
        transport=lambda u, b, t: (calls.append(1), (404, b""))[1],
        sleeper=sleeps.append)
    assert record["attempts"] == 1 and len(calls) == 1
    assert record["http_status"] == 404


def test_deliver_network_error_never_echoes_url():
    def boom(url, body, timeout):
        raise urllib.error.URLError(f"cannot reach {url}")

    record = deliver_notification(SECRET_URL, {"event": "x"},
                                  transport=boom, sleeper=lambda s: None)
    assert record["status"] == "failed" and record["attempts"] == 3
    assert "network_error" in record["error"]
    assert "SENTINELTOKEN123" not in json.dumps(record)
    assert "hook/path" not in json.dumps(record)


def test_deliver_to_webhooks_malformed_entry_is_data():
    records = deliver_to_webhooks(
        [{"url": "https://example.org/h"}, {"url": "ftp://bad"},
         {"nope": 1}],
        {"event": "multi_feed.updated", "version": 2},
        transport=_ok_transport)
    assert [r["status"] for r in records] == ["delivered", "failed",
                                              "failed"]
    assert records[1]["error"].startswith("unsupported")


# -- the store's webhook surface ---------------------------------------------


def test_store_webhook_lifecycle():
    store = MultiTimelineFeedStore(clock=FakeClock())
    feed_id = store.create(_payload())["feed_id"]
    url = "https://hooks.example.org/feed?tok=abc"
    status, meta = store.add_webhook(feed_id, url)
    assert status == "ok" and meta["host"] == "hooks.example.org"
    assert meta["webhook_id"] == webhook_id_for(url)
    assert "tok=abc" not in json.dumps(meta)
    # Re-registering is idempotent.
    status, again = store.add_webhook(feed_id, url)
    assert status == "ok" and again == meta
    status, listed = store.webhooks(feed_id)
    assert status == "ok" and listed == [meta]
    # The cap holds.
    for i in range(MAX_WEBHOOKS_PER_FEED - 1):
        assert store.add_webhook(
            feed_id, f"https://h{i}.example.org/x")[0] == "ok"
    assert store.add_webhook(
        feed_id, "https://extra.example.org/x")[0] == "too_many_webhooks"
    # Removal + unknown ids.
    assert store.remove_webhook(feed_id, "nope") == "webhook_not_found"
    assert store.remove_webhook(feed_id, meta["webhook_id"]) == "removed"
    remaining = store.webhooks(feed_id)[1]
    assert len(remaining) == MAX_WEBHOOKS_PER_FEED - 1
    assert all(w["webhook_id"] != meta["webhook_id"] for w in remaining)
    assert store.add_webhook("missing", url)[0] == "not_found"
    assert store.webhooks("missing")[0] == "not_found"


def test_store_webhook_ownership_and_delivery_log():
    store = MultiTimelineFeedStore(clock=FakeClock())
    feed_id = store.create(_payload(), owner_token=TOKEN)["feed_id"]
    assert store.add_webhook(
        feed_id, "https://example.org/h")[0] == "forbidden"
    assert store.add_webhook(
        feed_id, "https://example.org/h", owner_token=WRONG)[0] == \
        "forbidden"
    status, meta = store.add_webhook(
        feed_id, "https://example.org/h", owner_token=TOKEN)
    assert status == "ok"
    assert store.webhooks(feed_id)[0] == "forbidden"
    assert store.deliveries(feed_id)[0] == "forbidden"
    stamped = store.record_deliveries(feed_id, [
        {"webhook_id": meta["webhook_id"], "host": "example.org",
         "event": "multi_feed.subscribed", "version": 1,
         "status": "delivered", "attempts": 1, "http_status": 200,
         "error": None}])
    assert stamped[0]["feed_id"] == feed_id and "at" in stamped[0]
    status, log = store.deliveries(feed_id, owner_token=TOKEN)
    assert status == "ok" and log == stamped
    assert store.meta(feed_id)["webhooks"] == 1
    assert store.meta(feed_id)["deliveries"] == 1
    assert store.remove_webhook(
        feed_id, meta["webhook_id"]) == "forbidden"
    assert store.remove_webhook(
        feed_id, meta["webhook_id"], owner_token=TOKEN) == "removed"


def test_store_webhooks_persist_without_query_leak(tmp_path):
    path = tmp_path / "multifeeds.json"
    clock = FakeClock()
    store = MultiTimelineFeedStore(path=path, clock=clock)
    feed_id = store.create(_payload(), owner_token=TOKEN)["feed_id"]
    url = "https://hooks.example.org/feed"
    store.add_webhook(feed_id, url, owner_token=TOKEN)
    store.record_deliveries(feed_id, [
        {"webhook_id": webhook_id_for(url), "host": "hooks.example.org",
         "event": "multi_feed.subscribed", "version": 1,
         "status": "delivered", "attempts": 1, "http_status": 200,
         "error": None}])
    raw = path.read_text(encoding="utf-8")
    assert TOKEN not in raw  # only the owner token's hash persists
    revived = MultiTimelineFeedStore(path=path, clock=clock)
    status, listed = revived.webhooks(feed_id, owner_token=TOKEN)
    assert status == "ok" and listed[0]["host"] == "hooks.example.org"
    assert revived.deliveries(feed_id, owner_token=TOKEN)[1][0][
        "status"] == "delivered"
    # A malformed stored webhook row is skipped on load.
    data = json.loads(raw)
    data["entries"][0]["webhooks"] = [{"url": 5}, {"nope": 1}]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert MultiTimelineFeedStore(
        path=path, clock=clock).webhooks(
            feed_id, owner_token=TOKEN)[1] == []


# -- the HTTP surface ----------------------------------------------------------


def test_http_register_ping_update_push_and_log():
    receiver = _Receiver()
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds", _payload())
        assert status == 201
        feed_id = json.loads(body)["feed_id"]
        hooks_url = f"{base_url}/api/multi-feeds/{feed_id}/webhooks"
        status, _h, body = _post(hooks_url, {"url": receiver.url})
        assert status == 201
        created = json.loads(body)
        assert created["host"].startswith("127.0.0.1:")
        assert created["delivery"]["status"] == "delivered"
        assert created["delivery"]["event"] == "multi_feed.subscribed"
        assert len(receiver.hits) == 1
        ping = json.loads(receiver.hits[0][1])
        assert ping["event"] == "multi_feed.subscribed"
        assert ping["version"] == 1 and ping["feed_id"] == feed_id
        # The stored URL is never echoed by any listing.
        status, _h, body = _request(hooks_url)
        listed = json.loads(body)
        assert listed["count"] == 1
        assert receiver.url not in body.decode()
        # An update pushes multi_feed.updated with the new version.
        status, _h, body = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                                 _payload())
        updated = json.loads(body)
        assert status == 200 and updated["version"] == 2
        assert len(updated["notifications"]) == 1
        note = updated["notifications"][0]
        assert note["status"] == "delivered"
        assert note["event"] == "multi_feed.updated"
        assert note["version"] == 2
        assert len(receiver.hits) == 2
        pushed = json.loads(receiver.hits[1][1])
        assert pushed["version"] == 2 and pushed["etag"] == updated["etag"]
        assert pushed["timelines"] == ["Japan-France"]
        assert pushed["all_passed"] is True
        # The delivery log shows both, in order.
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        log = json.loads(body)
        assert [d["event"] for d in log["deliveries"]] == [
            "multi_feed.subscribed", "multi_feed.updated"]
        # Feed meta counts the webhook.
        status, _h, body = _request(f"{base_url}/api/multi-feeds/{feed_id}")
        assert json.loads(body)["webhooks"] == 1
    finally:
        server.shutdown()
        receiver.close()


def test_http_failed_delivery_never_fails_update():
    receiver = _Receiver(status=404)
    sleeps = []
    handler = make_handler(TastePilotAgent(), webhook_sleeper=sleeps.append)
    server, base_url = _serve(handler)
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds", _payload())
        feed_id = json.loads(body)["feed_id"]
        hooks_url = f"{base_url}/api/multi-feeds/{feed_id}/webhooks"
        status, _h, body = _post(hooks_url, {"url": receiver.url})
        created = json.loads(body)
        assert status == 201  # registration stands despite the 404 ping
        assert created["delivery"]["status"] == "failed"
        assert created["delivery"]["http_status"] == 404
        assert created["delivery"]["attempts"] == 1  # 404: no retry
        status, _h, body = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                                 _payload())
        updated = json.loads(body)
        assert status == 200 and updated["version"] == 2
        assert updated["notifications"][0]["status"] == "failed"
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        assert json.loads(body)["count"] == 2
    finally:
        server.shutdown()
        receiver.close()


def test_http_webhook_gates_and_removal():
    receiver = _Receiver()
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds", _payload())
        feed_id = json.loads(body)["feed_id"]
        hooks_url = f"{base_url}/api/multi-feeds/{feed_id}/webhooks"
        for bad, needle in (
                ({"url": "ftp://example.org/h"}, "unsupported"),
                ({"url": "https://user:pass@example.org/h"},
                 "bad_webhook_url"),
                ({"url": 5}, "bad_webhook_url"),
                ({"nope": "x"}, "bad_webhook_url")):
            status, _h, body = _post(hooks_url, bad)
            assert status == 400 and needle in body.decode()
            assert "user:pass" not in body.decode()
        status, _h, body = _post(
            f"{base_url}/api/multi-feeds/nope/webhooks",
            {"url": receiver.url})
        assert status == 404
        status, _h, _b = _request(hooks_url + "x")
        assert status == 404
        # Register, remove, and confirm updates push nothing more.
        status, _h, body = _post(hooks_url, {"url": receiver.url})
        webhook_id = json.loads(body)["webhook_id"]
        assert len(receiver.hits) == 1
        status, _h, _b = _request(
            f"{hooks_url}/{webhook_id}", method="DELETE")
        assert status == 200
        status, _h, _b = _request(
            f"{hooks_url}/{webhook_id}", method="DELETE")
        assert status == 404
        status, _h, body = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                                 _payload())
        assert json.loads(body)["notifications"] == []
        assert len(receiver.hits) == 1
        # Declared-length oversize -> 413 without reading.
        import http.client
        port = int(base_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", hooks_url)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(100_000))
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        assert resp.status == 413
        resp.read()
        conn.close()
    finally:
        server.shutdown()
        receiver.close()


def test_http_webhook_owner_flow():
    receiver = _Receiver()
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 _payload(), token=TOKEN)
        feed_id = json.loads(body)["feed_id"]
        hooks_url = f"{base_url}/api/multi-feeds/{feed_id}/webhooks"
        for bad_token in (None, WRONG):
            status, _h, _b = _post(hooks_url, {"url": receiver.url},
                                   token=bad_token)
            assert status == 403
        status, _h, body = _post(hooks_url, {"url": receiver.url},
                                 token=TOKEN)
        assert status == 201
        webhook_id = json.loads(body)["webhook_id"]
        status, _h, _b = _request(hooks_url)
        assert status == 403
        status, _h, body = _request(hooks_url, headers={
            "X-Owner-Token": TOKEN})
        assert status == 200 and json.loads(body)["count"] == 1
        notes_url = f"{base_url}/api/multi-feeds/{feed_id}/notifications"
        status, _h, _b = _request(notes_url)
        assert status == 403
        status, _h, _b = _request(
            f"{hooks_url}/{webhook_id}", method="DELETE")
        assert status == 403
        status, _h, _b = _request(
            f"{hooks_url}/{webhook_id}", method="DELETE",
            headers={"X-Owner-Token": TOKEN})
        assert status == 200
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Register push webhook" in html
    assert "/webhooks" in html and "/notifications" in html
    assert "multi_feed.subscribed" in html
    readme = README.read_text(encoding="utf-8")
    assert "notify.py" in readme and "multi_feed.updated" in readme
    assert "delivery log" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "register a webhook" in submission
    assert "Hosted push-notification accounts" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "Push-webhook probe" in deploy
