"""Webhook endpoint rotation: point a webhook at a new URL.

Doctrine under test: until now a subscriber whose endpoint
moved (a new domain, a rotated tunnel) had exactly one
remedy — remove the webhook and register it again, resetting
its pause state, failure count, and registration moment, and
starting over with a fresh signing secret. Rotation is the
managed-endpoint remedy, the sibling of run 45's secret
rotation: the URL is replaced in place under the feed's
owner rule, validated exactly as at registration, and the
webhook id is recomputed from the new URL (the id is the
URL's hash everywhere in the store). The signing secret,
pause state and its provenance, failure count, and
created_at all survive; the rotation moment is stamped
(``url_rotated_at``). Rotating onto a URL another webhook
on the feed already holds is refused (webhook_exists) —
one endpoint, one registration — and rotating to the
current URL is idempotent and stamps nothing. Rotation
delivers nothing. Delivery records are never edited: a
pre-rotation record keeps the old id and host label as the
audit trail, so it can no longer be redelivered (its id
names no current registration).
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (SIGNATURE_HEADER, verify_signature,
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
# Built by concatenation on purpose (run 42's lesson): a single
# credential-shaped literal can be redacted at write time.
TOKEN = "owner-" + "token-0123456789abcdef0123456789"
WRONG = "owner-" + "token-ffffffffffffffffffffffff"
SUPPLIED = "subscriber-" + "supplied-secret-0123456789"
URL = "https://hooks.example.com/push"
URL2 = "https://hooks2.example.com/push"
BAD_URL = "https://user:pw@hooks.example.com/push"
WID = webhook_id_for(URL)
WID2 = webhook_id_for(URL2)


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


def _store(tmp_path=None, owner_token=None, clock=None):
    path = tmp_path / "feeds.json" if tmp_path else None
    store = MultiTimelineFeedStore(path=path,
                                   clock=clock or FakeClock())
    meta = store.create(_payload(), owner_token=owner_token,
                        all_passed=True)
    status, wmeta = store.add_webhook(
        meta["feed_id"], URL, owner_token=owner_token,
        signing_secret=SUPPLIED)
    assert status == "ok"
    return store, meta["feed_id"], wmeta


def _entry(store, feed_id):
    return store.webhook_entries(feed_id)[0]


def _meta(store, feed_id, owner_token=None):
    status, webhooks = store.webhooks(feed_id, owner_token=owner_token)
    assert status == "ok"
    return webhooks[0]


# -- store-level rotation -----------------------------------------------


def test_rotate_url_recomputes_id_and_stamps_moment():
    clock = FakeClock()
    store, feed_id, wmeta = _store(clock=clock)
    assert wmeta["url_rotated_at"] is None
    clock.now += 60
    status, meta = store.rotate_webhook_url(feed_id, WID, URL2)
    assert status == "ok"
    assert meta["webhook_id"] == WID2 != WID
    assert meta["host"] == "hooks2.example.com"
    assert meta["url_rotated_at"] is not None
    assert meta["created_at"] == wmeta["created_at"]
    entry = _entry(store, feed_id)
    assert entry["url"] == URL2
    assert entry["signing_secret"] == SUPPLIED  # secret survives


def test_rotate_url_same_url_is_idempotent_and_unstamped():
    store, feed_id, _ = _store()
    status, meta = store.rotate_webhook_url(feed_id, WID, URL)
    assert status == "ok"
    assert meta["webhook_id"] == WID
    assert meta["url_rotated_at"] is None


def test_rotate_url_onto_registered_url_refused():
    store, feed_id, _ = _store()
    status, other = store.add_webhook(feed_id, URL2,
                                      signing_secret=None)
    assert status == "ok"
    status, meta = store.rotate_webhook_url(feed_id, WID, URL2)
    assert status == "webhook_exists" and meta is None
    assert _entry(store, feed_id)["url"] == URL  # unchanged


def test_rotate_url_bad_url_rejected_and_webhook_unchanged():
    store, feed_id, _ = _store()
    before = _entry(store, feed_id)
    with pytest.raises(ValueError) as excinfo:
        store.rotate_webhook_url(feed_id, WID, BAD_URL)
    assert "bad_webhook_url" in str(excinfo.value)
    assert "user:pw" not in str(excinfo.value)  # never echoed
    with pytest.raises(ValueError):
        store.rotate_webhook_url(feed_id, WID, "ftp://x.example/h")
    assert _entry(store, feed_id) == before


def test_rotate_url_preserves_pause_state_count_and_secret():
    store, feed_id, wmeta = _store()
    payload = {"event": "multi_feed.updated", "version": 2}
    record = {"webhook_id": WID, "host": "hooks.example.com",
              "event": "multi_feed.updated", "version": 2,
              "status": "failed", "attempts": 3,
              "http_status": 500, "error": "http_error (HTTP 500)",
              "signed": True}
    for _ in range(3):
        store.record_deliveries(feed_id, [dict(record)],
                                payload=payload)
    before = _meta(store, feed_id)
    assert before["paused"] is True
    assert before["paused_reason"] == "auto"
    status, meta = store.rotate_webhook_url(feed_id, WID, URL2)
    assert status == "ok"
    assert meta["paused"] is True
    assert meta["paused_reason"] == "auto"
    assert meta["consecutive_failures"] == 3
    assert meta["created_at"] == wmeta["created_at"]
    assert _entry(store, feed_id)["signing_secret"] == SUPPLIED


def test_rotate_url_gates():
    store, feed_id, _ = _store(owner_token=TOKEN)
    status, _ = store.rotate_webhook_url("nope", WID, URL2,
                                         owner_token=TOKEN)
    assert status == "not_found"
    status, _ = store.rotate_webhook_url(feed_id, WID, URL2)
    assert status == "forbidden"
    status, _ = store.rotate_webhook_url(feed_id, WID, URL2,
                                         owner_token=WRONG)
    assert status == "forbidden"
    status, _ = store.rotate_webhook_url(feed_id, "missing", URL2,
                                         owner_token=TOKEN)
    assert status == "webhook_not_found"
    status, meta = store.rotate_webhook_url(feed_id, WID, URL2,
                                            owner_token=TOKEN)
    assert status == "ok" and meta["webhook_id"] == WID2


def test_url_rotation_persists_and_old_records_kept(tmp_path):
    store, feed_id, _ = _store(tmp_path=tmp_path)
    payload = {"event": "multi_feed.updated", "version": 2}
    record = {"webhook_id": WID, "host": "hooks.example.com",
              "event": "multi_feed.updated", "version": 2,
              "status": "failed", "attempts": 1,
              "http_status": 500, "error": "http_error (HTTP 500)",
              "signed": True}
    store.record_deliveries(feed_id, [record], payload=payload)
    status, _ = store.rotate_webhook_url(feed_id, WID, URL2)
    assert status == "ok"
    store2 = MultiTimelineFeedStore(path=tmp_path / "feeds.json",
                                    clock=FakeClock())
    entry = _entry(store2, feed_id)
    assert entry["url"] == URL2 and entry["webhook_id"] == WID2
    assert entry["url_rotated_at"] is not None
    # The audit trail is never edited: the old record still
    # names the old id/host, and can no longer be redelivered.
    status, deliveries = store2.deliveries(feed_id)
    assert status == "ok"
    old = deliveries[0]
    assert old["webhook_id"] == WID
    assert old["host"] == "hooks.example.com"
    status, _ = store2.redelivery_target(feed_id, old["delivery_id"])
    assert status == "webhook_not_found"


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


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


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

    def close(self):
        self.server.shutdown()


def _feed_with_webhook(base, receiver, token=None):
    status, body = _post(f"{base}/api/multi-feeds", _payload(),
                         token=token)
    assert status in (200, 201)
    feed_id = json.loads(body)["feed_id"]
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                         {"url": receiver.url}, token=token)
    assert status == 201
    return feed_id, json.loads(body)


def _update(base, feed_id, token=None):
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                         _payload(), token=token)
    assert status == 200


def test_http_rotate_url_moves_pushes_and_keeps_secret():
    old, new = _Receiver(), _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id, reg = _feed_with_webhook(base, old)
        secret = reg["signing_secret"]
        wid = reg["webhook_id"]
        _update(base, feed_id)
        old_hits, new_hits = len(old.hits), len(new.hits)
        assert old_hits >= 1 and new_hits == 0
        status, body_bytes = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
            "/rotate-url", {"url": new.url})
        assert status == 200
        rotated = json.loads(body_bytes)
        assert rotated["webhook_id"] == webhook_id_for(new.url)
        assert rotated["host"] in new.url
        assert rotated["url_rotated_at"]
        assert rotated["signed"] is True
        assert rotated["created_at"] == reg["created_at"]
        assert new.url.encode() not in body_bytes  # never echoed
        _update(base, feed_id)
        assert len(old.hits) == old_hits      # old endpoint silent
        assert len(new.hits) == new_hits + 1  # pushes moved
        headers, body = new.hits[-1]
        # The surviving secret still signs pushes at the new URL.
        assert verify_signature(secret, body,
                                _header(headers, SIGNATURE_HEADER))
    finally:
        server.shutdown()
        old.close()
        new.close()


def test_http_rotate_url_onto_registered_url_is_400():
    first, second = _Receiver(), _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id, reg = _feed_with_webhook(base, first)
        status, _ = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                          {"url": second.url})
        assert status == 201
        status, body = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/"
            f"{reg['webhook_id']}/rotate-url", {"url": second.url})
        assert status == 400
        assert b"webhook_exists" in body
    finally:
        server.shutdown()
        first.close()
        second.close()


def test_http_rotate_url_gates_and_bad_bodies():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id, reg = _feed_with_webhook(base, receiver, token=TOKEN)
        wid = reg["webhook_id"]
        url = (f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
               "/rotate-url")
        status, _ = _post(url, {"url": URL2})  # owned, no token
        assert status == 403
        status, _ = _post(url, {"url": URL2}, token=WRONG)
        assert status == 403
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/nope"
            "/rotate-url", {"url": URL2}, token=TOKEN)
        assert status == 404
        status, _ = _post(
            f"{base}/api/multi-feeds/nope/webhooks/{wid}"
            "/rotate-url", {"url": URL2}, token=TOKEN)
        assert status == 404
        status, body = _post(url, raw=b"{nope", token=TOKEN)
        assert status == 400
        status, body = _post(url, raw=b"[1, 2]", token=TOKEN)
        assert status == 400
        status, body = _post(url, {}, token=TOKEN)  # url required
        assert status == 400 and b"bad_webhook_url" in body
        status, body = _post(url, {"url": 42}, token=TOKEN)
        assert status == 400
        status, body = _post(url, {"url": BAD_URL}, token=TOKEN)
        assert status == 400 and b"bad_webhook_url" in body
        assert b"user:pw" not in body  # the URL is never echoed
        status, body = _post(url, {"url": URL2},
                             token="bad token with spaces")
        assert status == 400
        # The owner can still rotate after all of that.
        status, body = _post(url, {"url": URL2}, token=TOKEN)
        assert status == 200
        assert json.loads(body)["webhook_id"] == WID2
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_carry_url_rotation():
    html = INDEX.read_text()
    assert "rotate-url" in html
    assert "Rotate webhook endpoints" in html
    for doc in (README, SUBMISSION, DEPLOY):
        assert "rotate-url" in doc.read_text()
