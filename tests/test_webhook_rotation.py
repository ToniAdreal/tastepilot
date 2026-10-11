"""Webhook signing-secret rotation: replace a secret in place.

Doctrine under test: until now a subscriber whose signing
secret leaked (or aged out) had exactly one remedy — remove
the webhook and register it again, losing nothing but also
gaining nothing: the pause state, failure count, and
registration moment all reset, and there was a window with no
endpoint at all. Rotation is the managed-endpoint remedy:
the secret is replaced in place under the feed's owner rule,
the old secret stops verifying immediately (no grace window
— a rotation usually means a leak), and everything else
about the webhook is untouched. The registration contract is
mirrored exactly: no secret named generates a fresh one,
returned exactly once in the rotation response; a supplied
secret is validated and never echoed; an explicit null
un-signs the webhook. Listings, metas, and delivery records
still report only the ``signed`` flag — never a secret, old
or new.
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
ROTATED = "subscriber-" + "rotated-secret-0123456789abc"
BAD = "tiny-" + "secret"
URL = "https://hooks.example.com/push"
WID = webhook_id_for(URL)


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


def _store(tmp_path=None, owner_token=None, secret=SUPPLIED):
    path = tmp_path / "feeds.json" if tmp_path else None
    store = MultiTimelineFeedStore(path=path, clock=FakeClock())
    meta = store.create(_payload(), owner_token=owner_token,
                        all_passed=True)
    status, wmeta = store.add_webhook(
        meta["feed_id"], URL, owner_token=owner_token,
        signing_secret=secret)
    assert status == "ok"
    return store, meta["feed_id"], wmeta


def _entry(store, feed_id):
    return store.webhook_entries(feed_id)[0]


def _meta(store, feed_id, owner_token=None):
    status, webhooks = store.webhooks(feed_id, owner_token=owner_token)
    assert status == "ok"
    return webhooks[0]


# -- store-level rotation -----------------------------------------------


def test_rotate_generated_replaces_secret_and_reports_once():
    store, feed_id, wmeta = _store()
    out = {}
    status, meta = store.rotate_webhook_secret(
        feed_id, WID, secret_out=out)
    assert status == "ok"
    assert meta["signed"] is True
    new_secret = out["signing_secret"]
    assert new_secret != SUPPLIED
    assert _entry(store, feed_id)["signing_secret"] == new_secret
    body = b'{"event": "multi_feed.updated"}'
    assert verify_signature(new_secret, body,
                            _sign(new_secret, body))
    assert not verify_signature(SUPPLIED, body,
                                _sign(new_secret, body))


def _sign(secret, body):
    from tastepilot.notify import sign_payload
    return sign_payload(secret, body)


def test_rotate_to_supplied_secret_never_reports_it():
    store, feed_id, _ = _store()
    out = {}
    status, meta = store.rotate_webhook_secret(
        feed_id, WID, signing_secret=ROTATED, secret_out=out)
    assert status == "ok"
    assert meta["signed"] is True
    assert out == {}  # a supplied secret has no exit at all
    assert _entry(store, feed_id)["signing_secret"] == ROTATED


def test_rotate_to_null_unsigned():
    store, feed_id, _ = _store()
    status, meta = store.rotate_webhook_secret(
        feed_id, WID, signing_secret=None)
    assert status == "ok"
    assert meta["signed"] is False
    assert "signing_secret" not in _entry(store, feed_id)


def test_rotate_bad_secret_rejected_and_webhook_unchanged():
    store, feed_id, _ = _store()
    with pytest.raises(ValueError) as excinfo:
        store.rotate_webhook_secret(feed_id, WID,
                                    signing_secret=BAD)
    assert "bad_signing_secret" in str(excinfo.value)
    assert BAD not in str(excinfo.value)
    assert _entry(store, feed_id)["signing_secret"] == SUPPLIED


def test_rotate_preserves_pause_state_count_and_created_at():
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
    status, meta = store.rotate_webhook_secret(feed_id, WID)
    assert status == "ok"
    assert meta["paused"] is True
    assert meta["paused_reason"] == "auto"
    assert meta["consecutive_failures"] == \
        before["consecutive_failures"] == 3
    assert meta["created_at"] == wmeta["created_at"]


def test_rotate_gates():
    store, feed_id, _ = _store(owner_token=TOKEN)
    status, _ = store.rotate_webhook_secret("nope", WID,
                                            owner_token=TOKEN)
    assert status == "not_found"
    status, _ = store.rotate_webhook_secret(feed_id, WID)
    assert status == "forbidden"
    status, _ = store.rotate_webhook_secret(feed_id, WID,
                                            owner_token=WRONG)
    assert status == "forbidden"
    status, _ = store.rotate_webhook_secret(feed_id, "missing",
                                            owner_token=TOKEN)
    assert status == "webhook_not_found"
    status, meta = store.rotate_webhook_secret(feed_id, WID,
                                               owner_token=TOKEN)
    assert status == "ok" and meta["signed"] is True


def test_rotation_persists_across_store_instances(tmp_path):
    store, feed_id, _ = _store(tmp_path=tmp_path)
    out = {}
    status, _ = store.rotate_webhook_secret(feed_id, WID,
                                            secret_out=out)
    assert status == "ok"
    text = (tmp_path / "feeds.json").read_text()
    assert out["signing_secret"] in text      # stored server-side
    assert SUPPLIED not in text               # the old one is gone
    store2 = MultiTimelineFeedStore(path=tmp_path / "feeds.json",
                                    clock=FakeClock())
    assert _entry(store2, feed_id)["signing_secret"] == \
        out["signing_secret"]


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


def _feed_with_webhook(base, receiver, token=None, secret="omit"):
    status, body = _post(f"{base}/api/multi-feeds", _payload(),
                         token=token)
    assert status in (200, 201)
    feed_id = json.loads(body)["feed_id"]
    reg = {"url": receiver.url}
    if secret != "omit":
        reg["secret"] = secret
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}/webhooks",
                         reg, token=token)
    assert status == 201
    return feed_id, json.loads(body)


def _update(base, feed_id, token=None):
    status, body = _post(f"{base}/api/multi-feeds/{feed_id}",
                         _payload(), token=token)
    assert status == 200


def test_http_rotation_flow_old_secret_stops_verifying():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id, reg = _feed_with_webhook(base, receiver)
        first = reg["signing_secret"]
        wid = reg["webhook_id"]
        _update(base, feed_id)
        headers, body = receiver.hits[-1]
        assert verify_signature(first, body,
                                _header(headers, SIGNATURE_HEADER))
        # Rotate with no body at all: a fresh secret, shown once.
        status, body_bytes = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
            "/rotate-secret")
        assert status == 200
        rotated = json.loads(body_bytes)
        second = rotated["signing_secret"]
        assert second != first and rotated["signed"] is True
        _update(base, feed_id)
        headers, body = receiver.hits[-1]
        assert verify_signature(second, body,
                                _header(headers, SIGNATURE_HEADER))
        assert not verify_signature(first, body,
                                    _header(headers, SIGNATURE_HEADER))
        # Listings show neither secret, ever.
        status, body_bytes = _request(
            f"{base}/api/multi-feeds/{feed_id}/webhooks")
        assert status == 200
        assert first.encode() not in body_bytes
        assert second.encode() not in body_bytes
    finally:
        server.shutdown()
        receiver.close()


def test_http_rotate_supplied_then_unsigned():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id, reg = _feed_with_webhook(base, receiver)
        wid = reg["webhook_id"]
        url = (f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
               "/rotate-secret")
        status, body_bytes = _post(url, {"secret": ROTATED})
        assert status == 200
        rotated = json.loads(body_bytes)
        assert "signing_secret" not in rotated  # never echoed
        assert rotated["signed"] is True
        _update(base, feed_id)
        headers, body = receiver.hits[-1]
        assert verify_signature(ROTATED, body,
                                _header(headers, SIGNATURE_HEADER))
        status, body_bytes = _post(url, {"secret": None})
        assert status == 200
        assert json.loads(body_bytes)["signed"] is False
        _update(base, feed_id)
        headers, body = receiver.hits[-1]
        assert _header(headers, SIGNATURE_HEADER) is None
    finally:
        server.shutdown()
        receiver.close()


def test_http_rotate_gates_and_bad_bodies():
    receiver = _Receiver()
    handler = make_handler(TastePilotAgent(),
                           webhook_sleeper=lambda s: None)
    server, base = _serve(handler)
    try:
        feed_id, reg = _feed_with_webhook(base, receiver, token=TOKEN)
        wid = reg["webhook_id"]
        url = (f"{base}/api/multi-feeds/{feed_id}/webhooks/{wid}"
               "/rotate-secret")
        status, _ = _post(url)  # owned feed, no token
        assert status == 403
        status, _ = _post(url, token=WRONG)
        assert status == 403
        status, _ = _post(
            f"{base}/api/multi-feeds/{feed_id}/webhooks/nope"
            "/rotate-secret", token=TOKEN)
        assert status == 404
        status, _ = _post(
            f"{base}/api/multi-feeds/nope/webhooks/{wid}"
            "/rotate-secret", token=TOKEN)
        assert status == 404
        status, body = _post(url, raw=b"{nope", token=TOKEN)
        assert status == 400
        status, body = _post(url, raw=b"[1, 2]", token=TOKEN)
        assert status == 400
        status, body = _post(url, {"secret": BAD}, token=TOKEN)
        assert status == 400
        assert b"bad_signing_secret" in body
        assert BAD.encode() not in body
        status, body = _post(url, token="bad token with spaces")
        assert status == 400
        # The owner can still rotate after all of that.
        status, body = _post(url, token=TOKEN)
        assert status == 200
        assert json.loads(body)["signing_secret"] != \
            reg["signing_secret"]
    finally:
        server.shutdown()
        receiver.close()


def test_ui_and_docs_carry_rotation():
    html = INDEX.read_text()
    assert "rotate-secret" in html
    assert "Rotate webhook signing secrets" in html
    assert "shown only here" in html
    for doc in (README, SUBMISSION, DEPLOY):
        assert "rotate" in doc.read_text().lower()
