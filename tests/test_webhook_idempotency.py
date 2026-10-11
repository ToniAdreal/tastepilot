"""Idempotency keys for push webhook deliveries.

Doctrine under test: TastePilot pushes are at-least-once —
a bounded retry after a lost response, an on-demand
redelivery, or a bulk redelivery can all deliver the same
change to the same webhook twice, and the receiver has no
way to tell a duplicate from a new change. Every delivery
therefore carries ``X-TastePilot-Idempotency-Key`` on the
wire and ``idempotency_key`` on its record — a
deterministic digest of the webhook id and the exact
payload bytes. Retries of one delivery share the key; a
redelivery of the same stored payload to the same webhook
reuses the original's key, so a receiver that already
applied that change can drop the resend; a different
payload or a different webhook is a different key. The
key is carried on unsigned deliveries too (dedupe is
orthogonal to signing) and proves nothing about
authenticity — that remains the signatures' job.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (IDEMPOTENCY_HEADER,
                               SIGNATURE_HEADER, TIMESTAMP_HEADER,
                               deliver_notification,
                               deliver_to_webhooks,
                               idempotency_key_for, skipped_record,
                               webhook_id_for)
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
SUPPLIED = "subscriber-supplied-secret-0123456789"
URL = "https://hooks.example.com/tastepilot"
PAYLOAD = {"event": "multi_feed.updated", "feed_id": "feed-1",
           "version": 2, "etag": "abc", "timelines": ["A"],
           "all_passed": True, "at": "2026-10-11T00:00:00Z"}


def _body(payload):
    return json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode()


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
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


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


def _post(url, obj=None):
    return _request(url, method="POST",
                    body=json.dumps(obj).encode() if obj is not None
                    else b"",
                    headers={"Content-Type": "application/json"})


def _payload():
    base = TastePilotAgent().run(DATED)
    timeline = {"name": "Japan-France", "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}
    return {"timelines": [timeline]}


# -- notify.py units ----------------------------------------------------------


def test_idempotency_key_derivation():
    body = _body(PAYLOAD)
    wid = webhook_id_for(URL)
    key = idempotency_key_for(wid, body)
    assert isinstance(key, str) and len(key) == 32
    assert all(ch in "0123456789abcdef" for ch in key)
    # Deterministic: same webhook + same payload bytes.
    assert idempotency_key_for(wid, body) == key
    assert idempotency_key_for(wid, bytearray(body)) == key
    # A different payload (a new version of the change) or a
    # different webhook is a different logical delivery.
    changed = _body({**PAYLOAD, "version": 3})
    assert idempotency_key_for(wid, changed) != key
    other = webhook_id_for("https://hooks.example.com/other")
    assert idempotency_key_for(other, body) != key
    # The key digests facts the record already shows; it is
    # not derived from any secret.
    assert SUPPLIED not in key


def test_idempotency_key_malformed_inputs_are_none():
    body = _body(PAYLOAD)
    for bad_id in (None, "", 42, b"bytes"):
        assert idempotency_key_for(bad_id, body) is None
    for bad_body in (None, "text", 42, {"event": "x"}):
        assert idempotency_key_for("webhook-id", bad_body) is None


def test_delivery_record_carries_key_matching_header_material():
    seen = {}

    def transport(url, body, timeout):
        seen["body"] = body
        return 200, b"{}"

    record = deliver_notification(URL, PAYLOAD, transport=transport)
    assert record["status"] == "delivered"
    assert record["idempotency_key"] == idempotency_key_for(
        record["webhook_id"], seen["body"])
    # The record's key is exactly what the wire header will
    # carry for this delivery (see the real-HTTP tests).
    assert record["idempotency_key"] == idempotency_key_for(
        webhook_id_for(URL), _body(PAYLOAD))


def test_failed_delivery_record_still_carries_key():
    def transport(url, body, timeout):
        return 500, b"no"

    record = deliver_notification(URL, PAYLOAD, transport=transport,
                                  max_attempts=1)
    assert record["status"] == "failed"
    assert record["idempotency_key"] == idempotency_key_for(
        webhook_id_for(URL), _body(PAYLOAD))


def test_malformed_stored_url_record_carries_no_key():
    records = deliver_to_webhooks([{"url": "not a url"}], PAYLOAD)
    assert records[0]["status"] == "failed"
    assert records[0]["idempotency_key"] is None


def test_skipped_record_carries_the_key_its_delivery_would():
    webhook = {"url": URL, "webhook_id": webhook_id_for(URL),
               "host": "hooks.example.com"}
    record = skipped_record(webhook, PAYLOAD)
    assert record["status"] == "skipped"
    assert record["idempotency_key"] == idempotency_key_for(
        webhook_id_for(URL), _body(PAYLOAD))
    # Malformed shapes degrade to no key, never a wrong one.
    assert skipped_record({}, PAYLOAD)["idempotency_key"] is None
    assert skipped_record(webhook, "not-a-dict")[
        "idempotency_key"] is None


# -- wire level (real HTTP through the default transport) ---------------------


def test_unsigned_delivery_carries_idempotency_header():
    receiver = _Receiver()
    try:
        record = deliver_notification(receiver.url, PAYLOAD)
        assert record["status"] == "delivered" \
            and record["signed"] is False
        headers, body = receiver.hits[0]
        key = _header(headers, IDEMPOTENCY_HEADER)
        assert key == record["idempotency_key"]
        assert key == idempotency_key_for(
            webhook_id_for(receiver.url), body)
        # Idempotency is not signing: no signature headers on
        # an unsigned delivery, exactly as before.
        assert _header(headers, SIGNATURE_HEADER) is None
        assert _header(headers, TIMESTAMP_HEADER) is None
    finally:
        receiver.close()


def test_signed_delivery_carries_key_beside_signatures():
    receiver = _Receiver()
    try:
        record = deliver_notification(receiver.url, PAYLOAD,
                                      secret=SUPPLIED)
        assert record["signed"] is True
        headers, body = receiver.hits[0]
        assert _header(headers, IDEMPOTENCY_HEADER) == \
            record["idempotency_key"]
        assert _header(headers, SIGNATURE_HEADER) is not None
    finally:
        receiver.close()


def test_retries_of_one_delivery_share_one_key():
    receiver = _Receiver(status=503)
    try:
        record = deliver_notification(receiver.url, PAYLOAD,
                                      sleeper=lambda s: None)
        assert record["status"] == "failed" and record["attempts"] == 3
        keys = {_header(headers, IDEMPOTENCY_HEADER)
                for headers, _ in receiver.hits}
        assert keys == {record["idempotency_key"]}
    finally:
        receiver.close()


def test_two_webhooks_same_payload_get_different_keys():
    first, second = _Receiver(), _Receiver()
    try:
        records = deliver_to_webhooks(
            [{"url": first.url}, {"url": second.url}], PAYLOAD)
        assert [r["status"] for r in records] == ["delivered"] * 2
        assert records[0]["idempotency_key"] != \
            records[1]["idempotency_key"]
        assert _header(first.hits[0][0], IDEMPOTENCY_HEADER) == \
            records[0]["idempotency_key"]
        assert _header(second.hits[0][0], IDEMPOTENCY_HEADER) == \
            records[1]["idempotency_key"]
    finally:
        first.close()
        second.close()


# -- server flow: a feed push and its redelivery -------------------------------


def test_feed_push_redelivery_reuses_original_key():
    receiver = _Receiver()
    store_server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, raw = _post(f"{base_url}/api/multi-feeds", _payload())
        assert status == 201
        feed_id = json.loads(raw)["feed_id"]
        status, raw = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url})
        assert status == 201
        registration = json.loads(raw)["delivery"]
        assert registration["idempotency_key"]
        assert _header(receiver.hits[0][0], IDEMPOTENCY_HEADER) == \
            registration["idempotency_key"]
        # A failed update push, redelivered: the resend is a
        # new delivery (new delivery_id) carrying the SAME
        # idempotency key — the receiver can drop it if the
        # original attempt actually landed.
        receiver.status = 500
        status, _ = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                          _payload())
        assert status == 200
        status, raw = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        log = json.loads(raw)["deliveries"]
        failed = [d for d in log if d["status"] == "failed"]
        assert failed and failed[-1]["idempotency_key"]
        receiver.status = 200
        status, raw = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications/"
            f"{failed[-1]['delivery_id']}/redeliver")
        assert status == 200
        resent = json.loads(raw)["delivery"]
        assert resent["delivery_id"] != failed[-1]["delivery_id"]
        assert resent["idempotency_key"] == \
            failed[-1]["idempotency_key"]
        assert _header(receiver.hits[-1][0],
                       IDEMPOTENCY_HEADER) == \
            failed[-1]["idempotency_key"]
        # A later, different change carries a different key.
        status, _ = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                          _payload())
        assert status == 200
        status, raw = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        log = json.loads(raw)["deliveries"]
        assert log[-1]["idempotency_key"] != \
            failed[-1]["idempotency_key"]
    finally:
        receiver.close()
        store_server.shutdown()


# -- UI + docs wiring -----------------------------------------------------------


def test_ui_and_docs_describe_idempotency_keys():
    page = INDEX.read_text()
    assert "X-TastePilot-Idempotency-Key" in page
    assert "idempoten" in page.lower()
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "X-TastePilot-Idempotency-Key" in text
        assert "idempoten" in text.lower()
