"""Signed push webhooks: HMAC signatures subscribers can verify.

Doctrine under test: a push is only trustworthy if the
subscriber can prove TastePilot sent it and it was not altered.
Every delivery to a webhook with a signing secret carries
``X-TastePilot-Signature: sha256=<HMAC-SHA256 hex>`` over the
exact body bytes (plus the event header). The subscriber either
supplies their own secret at registration (validated, never
echoed) or the server generates one and returns it exactly
once, in the registration response — listings, delivery
records, and logs name only a ``signed`` flag, never the
secret. An explicit null secret registers an unsigned webhook
by the subscriber's choice. Signed redeliveries sign too.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (EVENT_HEADER, SIGNATURE_HEADER,
                               deliver_notification, deliver_to_webhooks,
                               generate_signing_secret, sign_payload,
                               validate_signing_secret, verify_signature,
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
TOKEN = "signed-owner-token-0123456789abcd"
SUPPLIED = "subscriber-supplied-secret-0123456789"


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



def _header(headers, name):
    """Case-insensitive header lookup (HTTP header names are
    case-insensitive; the wire form may differ in case)."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _create_feed(base_url, token=None):
    status, raw = _post(f"{base_url}/api/multi-feeds",
                        _payload(), token=token)
    assert status == 201
    return json.loads(raw)["feed_id"]


# -- notify.py units ----------------------------------------------------------


def test_signing_secret_validation_and_generation():
    assert validate_signing_secret(f"  {SUPPLIED}  ") == SUPPLIED
    for bad in (None, 123, "", "short", "x" * 201,
                "has whitespace in it ok"):
        with pytest.raises(ValueError, match="bad_signing_secret"):
            validate_signing_secret(bad)
    first, second = generate_signing_secret(), generate_signing_secret()
    assert len(first) == 48 and first != second
    assert validate_signing_secret(first) == first


def test_sign_and_verify_signature():
    body = b'{"event":"multi_feed.updated","version":2}'
    header = sign_payload(SUPPLIED, body)
    assert header.startswith("sha256=") and len(header) == 7 + 64
    assert verify_signature(SUPPLIED, body, header)
    assert not verify_signature(SUPPLIED, body + b" ", header)
    assert not verify_signature("another-secret-0123456789", body, header)
    assert not verify_signature(SUPPLIED, body, "sha256=deadbeef")
    assert not verify_signature(SUPPLIED, body, None)
    assert not verify_signature(None, body, header)
    assert not verify_signature(SUPPLIED, "not-bytes", header)


def test_signed_delivery_headers_over_http():
    receiver = _Receiver()
    try:
        payload = {"event": "multi_feed.updated", "version": 2}
        record = deliver_notification(receiver.url, payload,
                                      sleeper=lambda s: None,
                                      secret=SUPPLIED)
        assert record["status"] == "delivered" and record["signed"] is True
        assert SUPPLIED not in json.dumps(record)
        headers, body = receiver.hits[0]
        assert _header(headers, SIGNATURE_HEADER) == sign_payload(SUPPLIED, body)
        assert verify_signature(SUPPLIED, body, _header(headers, SIGNATURE_HEADER))
        assert _header(headers, EVENT_HEADER) == "multi_feed.updated"
        assert json.loads(body) == payload
    finally:
        receiver.close()


def test_unsigned_delivery_has_no_signature_header():
    receiver = _Receiver()
    try:
        record = deliver_notification(
            receiver.url, {"event": "multi_feed.updated", "version": 1},
            sleeper=lambda s: None)
        assert record["status"] == "delivered" and record["signed"] is False
        headers, _body = receiver.hits[0]
        assert _header(headers, SIGNATURE_HEADER) is None
        assert _header(headers, EVENT_HEADER) == "multi_feed.updated"
    finally:
        receiver.close()


def test_deliver_to_webhooks_signs_from_entry_secret():
    receiver = _Receiver()
    try:
        records = deliver_to_webhooks(
            [{"url": receiver.url, "signing_secret": SUPPLIED},
             {"url": receiver.url}],
            {"event": "multi_feed.updated", "version": 3},
            sleeper=lambda s: None)
        assert [r["signed"] for r in records] == [True, False]
        assert records[0]["webhook_id"] == webhook_id_for(receiver.url)
        headers, body = receiver.hits[0]
        assert verify_signature(SUPPLIED, body, _header(headers, SIGNATURE_HEADER))
        headers, _body = receiver.hits[1]
        assert _header(headers, SIGNATURE_HEADER) is None
    finally:
        receiver.close()


# -- the store's signing surface ----------------------------------------------


def test_store_signing_lifecycle():
    store = MultiTimelineFeedStore(clock=FakeClock())
    feed_id = store.create(_payload())["feed_id"]
    url = "https://hooks.example.org/feed"
    # Default: generated, handed out once via secret_out only.
    secret_out = {}
    status, meta = store.add_webhook(feed_id, url,
                                     secret_out=secret_out)
    assert status == "ok" and meta["signed"] is True
    generated = secret_out["signing_secret"]
    assert len(generated) == 48
    assert generated not in json.dumps(meta)
    assert generated not in json.dumps(store.webhooks(feed_id)[1])
    # Re-registering keeps the first secret and hands out nothing.
    again_out = {}
    status, again = store.add_webhook(feed_id, url,
                                      secret_out=again_out)
    assert status == "ok" and again == meta and again_out == {}
    assert store.webhook_entries(feed_id)[0]["signing_secret"] == generated
    # Subscriber-supplied: stored, never handed back.
    out2 = {}
    status, meta2 = store.add_webhook(
        feed_id, "https://hooks2.example.org/f",
        signing_secret=SUPPLIED, secret_out=out2)
    assert status == "ok" and meta2["signed"] is True and out2 == {}
    # Explicit null: unsigned by choice.
    status, meta3 = store.add_webhook(
        feed_id, "https://hooks3.example.org/f", signing_secret=None)
    assert status == "ok" and meta3["signed"] is False
    assert "signing_secret" not in store.webhook_entries(feed_id)[2]
    # A bad supplied secret is refused categorically.
    with pytest.raises(ValueError, match="bad_signing_secret"):
        store.add_webhook(feed_id, "https://hooks4.example.org/f",
                          signing_secret="short")


def test_store_signing_secret_persists_and_signs_after_reload(tmp_path):
    path = tmp_path / "multifeeds.json"
    clock = FakeClock()
    store = MultiTimelineFeedStore(path=path, clock=clock)
    feed_id = store.create(_payload(), owner_token=TOKEN)["feed_id"]
    receiver = _Receiver()
    try:
        secret_out = {}
        store.add_webhook(feed_id, receiver.url, owner_token=TOKEN,
                          secret_out=secret_out)
        generated = secret_out["signing_secret"]
        raw = path.read_text(encoding="utf-8")
        assert TOKEN not in raw  # the owner token stays hash-only
        revived = MultiTimelineFeedStore(path=path, clock=clock)
        entries = revived.webhook_entries(feed_id)
        assert entries[0]["signing_secret"] == generated
        records = deliver_to_webhooks(
            entries, {"event": "multi_feed.updated", "version": 2})
        assert records[0]["signed"] is True
        headers, body = receiver.hits[0]
        assert verify_signature(generated, body, _header(headers, SIGNATURE_HEADER))
    finally:
        receiver.close()


def test_store_load_drops_malformed_signing_secret(tmp_path):
    path = tmp_path / "multifeeds.json"
    clock = FakeClock()
    store = MultiTimelineFeedStore(path=path, clock=clock)
    feed_id = store.create(_payload())["feed_id"]
    store.add_webhook(feed_id, "https://hooks.example.org/f",
                      signing_secret=SUPPLIED)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"][0]["webhooks"][0]["signing_secret"] = "bad"
    path.write_text(json.dumps(data), encoding="utf-8")
    revived = MultiTimelineFeedStore(path=path, clock=clock)
    entry = revived.webhook_entries(feed_id)[0]
    assert "signing_secret" not in entry  # unsigned, never a crash
    assert revived.webhooks(feed_id)[1][0]["signed"] is False


# -- the HTTP surface -----------------------------------------------------------


def test_http_generated_secret_shown_once():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    receiver = _Receiver()
    try:
        feed_id = _create_feed(base_url, token=TOKEN)
        status, raw = _post(f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
                            {"url": receiver.url}, token=TOKEN)
        assert status == 201
        created = json.loads(raw)
        generated = created["signing_secret"]
        assert len(generated) == 48 and created["signed"] is True
        assert created["delivery"]["signed"] is True
        headers, body = receiver.hits[0]
        assert verify_signature(generated, body, _header(headers, SIGNATURE_HEADER))
        # …and never again: listings and the delivery log omit it.
        status, raw = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            headers={"X-Owner-Token": TOKEN})
        assert status == 200 and generated not in raw.decode()
        status, raw = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications",
            headers={"X-Owner-Token": TOKEN})
        assert status == 200 and generated not in raw.decode()
    finally:
        receiver.close()
        server.shutdown()


def test_http_supplied_secret_signs_ping_and_update():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    receiver = _Receiver()
    try:
        feed_id = _create_feed(base_url)
        status, raw = _post(f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
                            {"url": receiver.url, "secret": SUPPLIED})
        assert status == 201
        created = json.loads(raw)
        assert "signing_secret" not in created  # theirs, not echoed
        assert created["signed"] is True
        headers, body = receiver.hits[0]
        assert verify_signature(SUPPLIED, body, _header(headers, SIGNATURE_HEADER))
        # The update push is signed with the same secret.
        status, raw = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                            _payload())
        assert status == 200
        headers, body = receiver.hits[-1]
        assert json.loads(body)["event"] == "multi_feed.updated"
        assert verify_signature(SUPPLIED, body, _header(headers, SIGNATURE_HEADER))
    finally:
        receiver.close()
        server.shutdown()


def test_http_unsigned_and_bad_secret():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    receiver = _Receiver()
    try:
        feed_id = _create_feed(base_url)
        status, raw = _post(f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
                            {"url": receiver.url, "secret": None})
        assert status == 201
        created = json.loads(raw)
        assert created["signed"] is False
        assert "signing_secret" not in created
        headers, _body = receiver.hits[0]
        assert _header(headers, SIGNATURE_HEADER) is None
        for bad in ("short-secret", 12345):
            status, raw = _post(
                f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
                {"url": "https://hooks.example.org/x", "secret": bad})
            assert status == 400
            assert b"bad_signing_secret" in raw
            assert b"short-secret" not in raw
    finally:
        receiver.close()
        server.shutdown()


def test_http_signed_redelivery_verifies():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    receiver = _Receiver(status=500)
    try:
        feed_id = _create_feed(base_url)
        status, raw = _post(f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
                            {"url": receiver.url, "secret": SUPPLIED})
        assert status == 201
        failed = json.loads(raw)["delivery"]
        assert failed["status"] == "failed" and failed["signed"] is True
        receiver.status = 200
        status, raw = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications/"
            f"{failed['delivery_id']}/redeliver")
        assert status == 200
        resent = json.loads(raw)["delivery"]
        assert resent["status"] == "delivered" and resent["signed"] is True
        headers, body = receiver.hits[-1]
        assert verify_signature(SUPPLIED, body, _header(headers, SIGNATURE_HEADER))
    finally:
        receiver.close()
        server.shutdown()


def test_ui_and_docs_describe_signing():
    page = INDEX.read_text(encoding="utf-8")
    assert "webhooksecret" in page and "signing_secret" in page
    assert "X-TastePilot-Signature" in page
    assert "HMAC" in README.read_text(encoding="utf-8")
    assert "signed" in SUBMISSION.read_text(encoding="utf-8")
    assert "signature" in DEPLOY.read_text(encoding="utf-8").lower()
