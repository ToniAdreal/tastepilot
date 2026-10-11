"""Replay protection for signed push webhooks.

Doctrine under test: a body signature alone proves a push
was genuine when it was sent — forever. A captured push
(a logged request body, a compromised intermediary) could
be replayed verbatim at any later time and still verify.
Signed deliveries therefore also carry
``X-TastePilot-Timestamp`` and
``X-TastePilot-Timestamp-Signature`` (HMAC-SHA256 over
``"<timestamp>." + body`` under the same secret): the
subscriber's ``verify_timestamped_signature`` rejects a
timestamp more than the tolerance (300 s) from now in
either direction, so the replay fails even though the
legacy body signature still verifies. The legacy header
and ``verify_signature`` are byte-unchanged for
subscribers who have not adopted the new check. The
timestamp is stamped once per delivery, so bounded
retries of one delivery share it; a redelivery is a new
delivery and is stamped fresh.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.notify import (SIGNATURE_HEADER,
                               SIGNATURE_TOLERANCE_SECONDS,
                               TIMESTAMP_HEADER,
                               TIMESTAMP_SIGNATURE_HEADER,
                               deliver_notification,
                               sign_payload,
                               sign_timestamped_payload,
                               verify_signature,
                               verify_timestamped_signature)
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
NOW = 1_700_000_000


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


def test_timestamped_sign_and_verify_round_trip():
    body = b'{"event":"multi_feed.updated","version":2}'
    header = sign_timestamped_payload(SUPPLIED, NOW, body)
    assert header.startswith("sha256=") and len(header) == 7 + 64
    # The timestamp is part of the signed material: the same
    # body at another moment is a different signature, and
    # neither equals the legacy body-only signature.
    assert header != sign_timestamped_payload(SUPPLIED, NOW + 1, body)
    assert header != sign_payload(SUPPLIED, body)
    assert verify_timestamped_signature(SUPPLIED, body, header, NOW,
                                        now=NOW)
    # Header timestamps arrive as strings — accepted as-is.
    assert verify_timestamped_signature(SUPPLIED, body, header,
                                        str(NOW), now=NOW)
    assert not verify_timestamped_signature(
        SUPPLIED, body + b" ", header, NOW, now=NOW)
    assert not verify_timestamped_signature(
        SUPPLIED, body, header, NOW + 1, now=NOW)
    assert not verify_timestamped_signature(
        "another-secret-0123456789", body, header, NOW, now=NOW)
    with pytest.raises(ValueError, match="bad_timestamp"):
        sign_timestamped_payload(SUPPLIED, "not-a-time", body)


def test_timestamped_verify_freshness_window():
    body = b'{"event":"multi_feed.updated"}'
    header = sign_timestamped_payload(SUPPLIED, NOW, body)
    tol = SIGNATURE_TOLERANCE_SECONDS
    assert tol == 300
    # Inside the window, both directions, verifies.
    assert verify_timestamped_signature(SUPPLIED, body, header, NOW,
                                        now=NOW + tol)
    assert verify_timestamped_signature(SUPPLIED, body, header, NOW,
                                        now=NOW - tol)
    # A replay after the window fails — the exact attack this
    # feature exists for — even though the signature itself
    # is genuine and the legacy check still passes.
    assert not verify_timestamped_signature(SUPPLIED, body, header,
                                            NOW, now=NOW + tol + 1)
    assert not verify_timestamped_signature(SUPPLIED, body, header,
                                            NOW, now=NOW - tol - 1)
    assert verify_signature(SUPPLIED, body, sign_payload(SUPPLIED,
                                                         body))
    # A caller-chosen tighter window is honoured.
    assert not verify_timestamped_signature(
        SUPPLIED, body, header, NOW, tolerance_seconds=10,
        now=NOW + 11)
    assert verify_timestamped_signature(
        SUPPLIED, body, header, NOW, tolerance_seconds=10, now=NOW + 9)


def test_timestamped_verify_malformed_inputs_are_false():
    body = b"{}"
    header = sign_timestamped_payload(SUPPLIED, NOW, body)
    for bad_ts in (None, True, 0, -5, "12x", "", 1.5):
        assert not verify_timestamped_signature(
            SUPPLIED, body, header, bad_ts, now=NOW)
    assert not verify_timestamped_signature(None, body, header, NOW,
                                            now=NOW)
    assert not verify_timestamped_signature(SUPPLIED, "nope", header,
                                            NOW, now=NOW)
    assert not verify_timestamped_signature(SUPPLIED, body, None, NOW,
                                            now=NOW)
    assert not verify_timestamped_signature(SUPPLIED, body, header,
                                            NOW, now="soon")
    assert not verify_timestamped_signature(
        SUPPLIED, body, header, NOW, tolerance_seconds=-1, now=NOW)


def test_legacy_signature_scheme_unchanged():
    body = b'{"event":"multi_feed.subscribed"}'
    header = sign_payload(SUPPLIED, body)
    assert verify_signature(SUPPLIED, body, header)
    # A timestamped signature is not a legacy signature and
    # vice versa — the two checks never cross-accept.
    ts_header = sign_timestamped_payload(SUPPLIED, NOW, body)
    assert not verify_signature(SUPPLIED, body, ts_header)
    assert not verify_timestamped_signature(SUPPLIED, body, header,
                                            NOW, now=NOW)


# -- wire level (real HTTP through the default transport) ---------------------


def test_signed_delivery_carries_replay_headers():
    receiver = _Receiver()
    try:
        payload = {"event": "multi_feed.updated", "version": 3}
        record = deliver_notification(receiver.url, payload,
                                      secret=SUPPLIED, timestamp=NOW)
        assert record["status"] == "delivered" and record["signed"]
        assert len(receiver.hits) == 1
        headers, body = receiver.hits[0]
        # Legacy headers are byte-identical to before.
        assert _header(headers, SIGNATURE_HEADER) == \
            sign_payload(SUPPLIED, body)
        assert verify_signature(SUPPLIED, body,
                                _header(headers, SIGNATURE_HEADER))
        # The replay-protection pair: the stamped timestamp and
        # a signature over "<timestamp>." + body that verifies
        # inside the window and fails outside it.
        assert _header(headers, TIMESTAMP_HEADER) == str(NOW)
        ts_header = _header(headers, TIMESTAMP_SIGNATURE_HEADER)
        assert ts_header == sign_timestamped_payload(SUPPLIED, NOW,
                                                     body)
        assert verify_timestamped_signature(SUPPLIED, body, ts_header,
                                            NOW, now=NOW + 60)
        assert not verify_timestamped_signature(
            SUPPLIED, body, ts_header, NOW,
            now=NOW + SIGNATURE_TOLERANCE_SECONDS + 1)
    finally:
        receiver.close()


def test_signed_delivery_default_timestamp_is_current():
    receiver = _Receiver()
    try:
        before = int(time.time())
        record = deliver_notification(
            receiver.url, {"event": "multi_feed.test", "version": 1},
            secret=SUPPLIED)
        after = int(time.time())
        assert record["status"] == "delivered"
        headers, _body = receiver.hits[0]
        stamped = int(_header(headers, TIMESTAMP_HEADER))
        assert before <= stamped <= after
    finally:
        receiver.close()


def test_unsigned_delivery_carries_no_replay_headers():
    receiver = _Receiver()
    try:
        record = deliver_notification(
            receiver.url, {"event": "multi_feed.updated", "version": 1})
        assert record["status"] == "delivered" \
            and record["signed"] is False
        headers, _body = receiver.hits[0]
        assert _header(headers, SIGNATURE_HEADER) is None
        assert _header(headers, TIMESTAMP_HEADER) is None
        assert _header(headers, TIMESTAMP_SIGNATURE_HEADER) is None
    finally:
        receiver.close()


def test_retries_of_one_delivery_share_one_timestamp():
    receiver = _Receiver(status=503)
    try:
        sleeps = []
        record = deliver_notification(
            receiver.url, {"event": "multi_feed.updated",
                           "version": 1},
            secret=SUPPLIED, timestamp=NOW, sleeper=sleeps.append)
        assert record["status"] == "failed"
        assert record["attempts"] == 3 and sleeps == [0.5, 1.0]
        stamps = {_header(headers, TIMESTAMP_HEADER)
                  for headers, _ in receiver.hits}
        sigs = {_header(headers, TIMESTAMP_SIGNATURE_HEADER)
                for headers, _ in receiver.hits}
        assert stamps == {str(NOW)} and len(sigs) == 1
    finally:
        receiver.close()


# -- server flow: a feed push and its redelivery --------------------------------


def test_feed_push_and_redelivery_carry_fresh_replay_headers():
    receiver = _Receiver()
    store_server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, raw = _post(f"{base_url}/api/multi-feeds", _payload())
        assert status == 201
        feed_id = json.loads(raw)["feed_id"]
        status, raw = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/webhooks",
            {"url": receiver.url, "secret": SUPPLIED})
        assert status == 201
        delivery = json.loads(raw)["delivery"]
        assert delivery["status"] == "delivered"
        assert len(receiver.hits) == 1
        headers, body = receiver.hits[0]
        first_ts = int(_header(headers, TIMESTAMP_HEADER))
        assert verify_timestamped_signature(
            SUPPLIED, body,
            _header(headers, TIMESTAMP_SIGNATURE_HEADER), first_ts)
        # Redeliver a failed push: the resend is a NEW delivery,
        # stamped fresh (never the original's replayed stamp).
        receiver.status = 500
        status, _ = _post(f"{base_url}/api/multi-feeds/{feed_id}",
                          _payload())
        assert status == 200
        status, raw = _request(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications")
        log = json.loads(raw)["deliveries"]
        failed = [d for d in log if d["status"] == "failed"]
        assert failed
        receiver.status = 200
        before = int(time.time())
        status, raw = _post(
            f"{base_url}/api/multi-feeds/{feed_id}/notifications/"
            f"{failed[-1]['delivery_id']}/redeliver")
        assert status == 200
        headers, body = receiver.hits[-1]
        resent_ts = int(_header(headers, TIMESTAMP_HEADER))
        assert resent_ts >= before
        assert verify_timestamped_signature(
            SUPPLIED, body,
            _header(headers, TIMESTAMP_SIGNATURE_HEADER), resent_ts)
    finally:
        receiver.close()
        store_server.shutdown()


# -- UI + docs wiring -----------------------------------------------------------


def test_ui_and_docs_describe_replay_protection():
    page = INDEX.read_text()
    assert "X-TastePilot-Timestamp" in page
    assert "replay" in page.lower()
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "X-TastePilot-Timestamp" in text
        assert "replay" in text.lower()
