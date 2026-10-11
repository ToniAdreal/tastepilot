"""Push change notifications for calendar subscriptions (webhooks).

Run 38's multi-timeline feeds made change *checkable*: a version, a
capped change log, and an ETag a subscriber polls. This module adds
the push half in generic form — webhooks the subscriber registers
themselves — exactly as earlier runs built generic HTTP providers
instead of a named service's API:

- a subscriber registers an http(s) webhook URL on a multi-feed
  (at most ``MAX_WEBHOOKS_PER_FEED`` per feed); TastePilot stores
  the URL only inside the feed entry (it is the delivery address —
  there is no way to deliver without it) and names it everywhere
  else — responses, delivery records, logs — by its host label
  only, so a query string (which could carry a token) never
  appears in any surface;
- registration itself fires one ``multi_feed.subscribed`` test
  delivery to that webhook, so a subscriber learns immediately
  whether their endpoint works; every later in-place update of
  the feed fires one ``multi_feed.updated`` delivery to every
  registered webhook, carrying the new version, etag, timeline
  names, and recomputed verdict — the same facts the change log
  records, pushed instead of polled;
- delivery is a plain POST of the JSON notification with bounded
  retries on transient failures (HTTP 429/5xx and transport-level
  network errors, up to 3 attempts with exponential backoff —
  the real Qloo client's contract), through an injectable
  transport for tests. No credential or API key is ever sent:
  the headers are the content type, accept, the event name,
  and — for a signed webhook — its HMAC signature (below);
- every attempt's outcome is a delivery record (webhook id,
  host, event, feed version, delivered/failed, attempt count,
  last HTTP status, error category). The feed store keeps the
  last ``MAX_DELIVERIES`` records per feed. A failed delivery
  never fails the feed operation that triggered it — the feed
  update stands, the failure is recorded, and the subscriber can
  read it back from the notifications surface;
- a failed delivery is not the end of the story: each record
  is stamped by the feed store with a ``delivery_id`` and
  carries the exact notification payload it attempted, so the
  subscriber can ask for it to be resent on demand
  (``POST /api/multi-feeds/<id>/notifications/<delivery_id>/
  redeliver``). The resend goes to the webhook's currently
  registered URL, is itself recorded as a new delivery naming
  the one it redelivers (``redelivery_of``), and the original
  record is never edited — the log stays an audit trail, not a
  status board. A delivered record cannot be redelivered; a
  record from before payloads were kept cannot be either;
- webhook URLs carrying userinfo (``user:pass@host``) are
  refused: a credential does not belong in a stored URL.
- every delivery to a webhook that has a signing secret is
  signed: the POST carries ``X-TastePilot-Signature:
  sha256=<hex>`` (HMAC-SHA256 over the exact body bytes) and
  ``X-TastePilot-Event``, so the subscriber can verify with
  ``verify_signature`` that a push genuinely came from
  TastePilot and was not altered in transit. A subscriber
  either supplies their own secret at registration or has
  one generated for them and shown exactly once in the
  registration response; the secret itself never appears in
  any listing, delivery record, or log — records carry only
  a ``signed`` flag. A webhook registered with an explicit
  null secret stays unsigned by the subscriber's choice.
- a webhook whose endpoint keeps failing is paused, not
  hammered: the feed store counts consecutive failed
  deliveries per webhook (a delivered push resets the count;
  a skipped push is not an attempt and never counts), and
  at ``PAUSE_AFTER_CONSECUTIVE_FAILURES`` the webhook is
  paused — later pushes to it are recorded as ``skipped``
  deliveries (status ``skipped``, error ``webhook_paused``,
  zero attempts, the payload still stored, so the missed
  change can be redelivered after a resume) instead of
  being attempted. Redelivery to a paused webhook is
  refused (``webhook_paused``) and bulk redelivery skips
  it with that reason. The subscriber resumes the webhook
  (``POST /api/multi-feeds/<id>/webhooks/<webhook_id>/
  resume``, owner rule as for the feed; pausing by hand is
  the same route with ``/pause``), which clears the count;
  resuming is the subscriber's deliberate act, exactly like
  a feed update renewing a feed's expiry.
- a webhook's endpoint is maintainable, not just its
  secret: the feed store can point a webhook at a new URL
  in place (``rotate_webhook_url``) — the id is recomputed
  from the new URL, the moment is stamped, and the signing
  secret, pause state, failure count, and registration
  moment all survive, so an endpoint migration costs the
  subscriber nothing but the edit. Rotating onto a URL
  another webhook on the feed already holds is refused,
  and delivery records are never edited: pre-rotation
  records keep the old id and host as the audit trail.
- a webhook subscribes to events, not to everything: at
  registration (or later, in place, via ``set_webhook_events``)
  the subscriber may name the events they want — any subset
  of ``WEBHOOK_EVENTS``; no filter (the default, and what a
  legacy registration carries) means every event. An automatic
  push whose event the webhook did not subscribe to is
  simply not sent to it: it is not an attempt, produces no
  delivery record, and never touches the webhook's failure
  count — the subscriber opted out, which is different from
  the endpoint failing or being paused (a paused webhook's
  pushes ARE recorded, as ``skipped``, because those are
  changes the subscriber wanted and missed). The filter
  governs automatic pushes only: an on-demand redelivery is
  the subscriber's explicit act and resends regardless.
  Replacing the filter stamps ``events_updated_at`` and
  leaves everything else — id, secret, pause state, failure
  count, registration moment — untouched; setting the same
  effective filter is idempotent and stamps nothing, and an
  explicit null filter resets the webhook to every event.
- a subscriber can ask for a test push at any time, not
  only at registration: ``POST /api/multi-feeds/<id>/
  webhooks/<webhook_id>/test`` fires one ``multi_feed.test``
  delivery to that webhook alone (owner rule as for the
  feed), so a subscriber who just rotated an endpoint,
  rotated a signing secret, or replaced an event filter can
  verify the endpoint works — and that the signature
  verifies — without waiting for (or forcing) a feed
  update. The test is the subscriber's explicit act, so
  like an on-demand redelivery it is sent regardless of
  the webhook's event filter; unlike a redelivery it is a
  fresh payload (the feed's current version, etag, timeline
  names, and verdict), not a resend. A paused webhook is
  refused (``webhook_paused``), exactly as for redelivery —
  resuming is how a paused endpoint comes back, and the
  test delivery counts toward the webhook's delivery
  health like any other push, so testing a still-failing
  endpoint can itself reach the pause threshold.
- an auto-pause is a circuit breaker, not a life sentence:
  the store stamps it with its reason (``auto``) and the
  moment it paused, and after ``PAUSE_COOLDOWN_SECONDS``
  the next feed update *probes* the endpoint with that
  update's own push (the delivery record is stamped
  ``probe``). A delivered probe closes the breaker — the
  webhook resumes with its count cleared; a failed probe
  re-pauses it and restarts the cooldown from that moment.
  A webhook paused *by hand* (reason ``manual``) is never
  probed: only the subscriber's resume restarts it, because
  a deliberate pause is an instruction, not a symptom.

Email notifications are deliberately absent: sending mail needs
an account and credentials, both outside the build's hard
boundaries. The remaining step to a hosted push service is a
named provider's own API behind this same registry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_WEBHOOK_URL_CHARS = 500
MAX_WEBHOOKS_PER_FEED = 4
MAX_DELIVERIES = 20
MAX_ATTEMPTS = 3
PAUSE_AFTER_CONSECUTIVE_FAILURES = 3
PAUSE_COOLDOWN_SECONDS = 3600
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
MIN_SIGNING_SECRET_CHARS = 16
MAX_SIGNING_SECRET_CHARS = 200
SIGNATURE_HEADER = "X-TastePilot-Signature"
EVENT_HEADER = "X-TastePilot-Event"
WEBHOOK_EVENTS = ("multi_feed.subscribed", "multi_feed.updated",
                  "multi_feed.created", "multi_feed.test")
"""Every event a multi-feed push can carry, in canonical
order. ``multi_feed.created`` is listed for completeness —
a feed has no webhooks at its own creation moment, so in
practice pushes carry ``subscribed`` (the registration
test delivery), ``updated`` (an in-place update), or
``test`` (an on-demand test push the subscriber asked
for via the webhook's test route)."""


def validate_webhook_url(url) -> str:
    """Validate a webhook URL; returns it stripped.

    Categorised ValueErrors, never an echo of the URL beyond its
    category: ``bad_webhook_url`` for shape problems (type,
    length, whitespace, userinfo), ``unsupported`` for a
    non-http(s) scheme or a missing host."""
    if not isinstance(url, str):
        raise ValueError("bad_webhook_url: webhook URL must be a string")
    url = url.strip()
    if not url or len(url) > MAX_WEBHOOK_URL_CHARS:
        raise ValueError("bad_webhook_url: webhook URL length")
    if any(ch.isspace() for ch in url):
        raise ValueError("bad_webhook_url: webhook URL has whitespace")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("unsupported: webhook URL must be an "
                         "http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ValueError("bad_webhook_url: webhook URL must not "
                         "carry credentials")
    return url


def webhook_host_label(url: str) -> str:
    """Provenance label for a webhook URL: the host (with port
    when present) and nothing else — no scheme, path, userinfo,
    or query string ever reaches a response or a log line."""
    parsed = urllib.parse.urlparse(url)
    label = parsed.hostname or ""
    if parsed.port:
        label = f"{label}:{parsed.port}"
    return label


def webhook_id_for(url: str) -> str:
    """Deterministic id for a webhook URL: registering the same
    URL twice on one feed is idempotent (same id), and the id
    reveals nothing about the URL beyond what its host label
    already shows."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def validate_webhook_events(events):
    """Validate a webhook event filter; returns the filter
    as a list in canonical ``WEBHOOK_EVENTS`` order with
    duplicates removed, or None when ``events`` is None —
    the no-filter value, meaning every event. Anything else
    malformed raises a categorised ValueError
    (``bad_webhook_events``) that never echoes the supplied
    values: a non-list, an empty list (an empty subscription
    is a removal, not a filter), a non-string entry, or a
    name outside ``WEBHOOK_EVENTS``."""
    if events is None:
        return None
    if isinstance(events, tuple):
        events = list(events)
    if not isinstance(events, list) or not events:
        raise ValueError("bad_webhook_events: events must be "
                         "a non-empty list of event names")
    for event in events:
        if not isinstance(event, str) \
                or event not in WEBHOOK_EVENTS:
            raise ValueError("bad_webhook_events: unknown "
                             "event name in filter")
    return [event for event in WEBHOOK_EVENTS if event in events]


def webhook_subscribes_to(entry, event) -> bool:
    """True when an automatic push carrying ``event`` should
    go to this webhook entry: a webhook with no stored filter
    (None, missing, or a malformed/empty value — the store
    coerces those to no-filter on load) subscribes to every
    event; a filtered webhook subscribes only to the events
    its filter names."""
    if not isinstance(entry, dict):
        return True
    events = entry.get("events")
    if not isinstance(events, list) or not events:
        return True
    return event in events


def validate_signing_secret(secret) -> str:
    """Validate a subscriber-supplied signing secret; returns
    it stripped. Categorised ValueError (``bad_signing_secret``),
    never echoing the secret: a string of 16–200 chars with no
    whitespace."""
    if not isinstance(secret, str):
        raise ValueError("bad_signing_secret: signing secret "
                         "must be a string")
    secret = secret.strip()
    if not MIN_SIGNING_SECRET_CHARS <= len(secret) \
            <= MAX_SIGNING_SECRET_CHARS:
        raise ValueError("bad_signing_secret: signing secret "
                         "length")
    if any(ch.isspace() for ch in secret):
        raise ValueError("bad_signing_secret: signing secret "
                         "has whitespace")
    return secret


def generate_signing_secret() -> str:
    """A fresh server-generated signing secret (48 hex chars).
    It is returned to the subscriber exactly once, in the
    registration response; everywhere else only its existence
    is ever reported."""
    return secrets.token_hex(24)


def sign_payload(secret: str, body: bytes) -> str:
    """The signature header value for one delivery body:
    ``sha256=<HMAC-SHA256 hex>`` over the exact body bytes."""
    digest = hmac.new(secret.encode(), body,
                      hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret, body: bytes, header) -> bool:
    """What a subscriber runs on receipt: True iff ``header``
    is the signature of ``body`` under ``secret``. Constant-
    time comparison; any malformed input is simply False."""
    if not isinstance(secret, str) or not isinstance(header, str) \
            or not isinstance(body, (bytes, bytearray)):
        return False
    return hmac.compare_digest(sign_payload(secret, bytes(body)),
                               header)


def delivery_id_for(feed_id: str, webhook_id, event, version,
                    at: str, seq: int) -> str:
    """Deterministic id for one stamped delivery record: a
    SHA-256 prefix over the feed, webhook, event, version, the
    store-stamped time, and the record's position in the feed's
    delivery log — unique per record (two records stamped in
    the same second for the same event still differ), and
    revealing nothing beyond what the record itself shows."""
    material = json.dumps(
        [feed_id, webhook_id, event, version, at, seq],
        ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def build_notification(feed_id: str, kind: str, version: int,
                         etag: str, timelines, all_passed,
                         at: str) -> dict:
    """The pushed payload: exactly the facts the feed's change
    log records (kind, version, etag, timeline names, the
    recomputed-at-write verdict) plus the feed id and the
    event name — nothing else, and never a plan body."""
    event = {"created": "multi_feed.created",
             "updated": "multi_feed.updated",
             "test": "multi_feed.test",
             "subscribed": "multi_feed.subscribed"}.get(
                 kind, f"multi_feed.{kind}")
    return {"event": event, "kind": kind, "feed_id": feed_id,
            "version": version, "etag": etag,
            "timelines": list(timelines or []),
            "all_passed": all_passed, "at": at}


def deliver_notification(url: str, payload: dict, transport=None,
                         timeout: float = 5.0,
                         max_attempts: int = MAX_ATTEMPTS,
                         sleeper=None, secret=None) -> dict:
    """POST one notification to one webhook; returns its
    delivery record.

    ``transport`` is injectable for tests: a callable
    ``(url, body_bytes, timeout) -> (status, body_bytes)``. The
    default transport is a plain urllib POST — no key or
    credential is ever sent. When ``secret`` is given, the
    default transport also sends the HMAC signature header
    (``sign_payload`` over the exact body bytes) and the event
    header, so the subscriber can verify the push; the record
    then carries ``signed: True`` — never the secret or the
    signature's inputs beyond the payload itself. Transient
    failures are retried with exponential backoff through the
    injectable ``sleeper``; every other outcome is final. The
    record names the webhook by id + host label only and
    carries an error *category*, never the URL, its query
    string, or a response body.
    """
    url = validate_webhook_url(url)
    record = {"webhook_id": webhook_id_for(url),
              "host": webhook_host_label(url),
              "event": payload.get("event"),
              "version": payload.get("version"),
              "status": "failed", "attempts": 0,
              "http_status": None, "error": None,
              "signed": bool(secret)}
    body = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode()
    sleep = sleeper or time.sleep
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        record["attempts"] = attempt + 1
        try:
            if transport is not None:
                status, _resp = transport(url, body, timeout)
            else:
                headers = {"Content-Type": "application/json",
                           "accept": "application/json",
                           EVENT_HEADER: str(payload.get("event"))}
                if secret:
                    headers[SIGNATURE_HEADER] = sign_payload(
                        secret, body)
                req = urllib.request.Request(
                    url, data=body, method="POST", headers=headers)
                try:
                    with urllib.request.urlopen(
                            req, timeout=timeout) as resp:
                        status, _resp = resp.status, resp.read()
                except urllib.error.HTTPError as exc:  # live path
                    status, _resp = exc.code, b""
        except (urllib.error.URLError, OSError) as exc:
            # Only the exception type is named — a URLError's own
            # text can embed the full URL, query string included.
            record["error"] = f"network_error ({type(exc).__name__})"
            if attempt + 1 < attempts:
                sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            return record
        record["http_status"] = status
        if 200 <= status < 300:
            record["status"] = "delivered"
            record["error"] = None
            return record
        record["error"] = f"http_error (HTTP {status})"
        if status in TRANSIENT_STATUSES and attempt + 1 < attempts:
            sleep(min(0.5 * (2 ** attempt), 4.0))
            continue
        return record
    return record


def skipped_record(webhook: dict, payload: dict) -> dict:
    """The delivery record for a push that was never attempted
    because its webhook is paused: status ``skipped``, zero
    attempts, error category ``webhook_paused``. It has exactly
    the shape ``deliver_notification`` returns, so the feed
    store stamps and logs it like any other record — and
    because the store still attaches the attempted payload,
    the missed change stays redeliverable once the webhook is
    resumed. The webhook is named by id + host label only."""
    url = webhook.get("url") if isinstance(webhook, dict) else None
    webhook_id = webhook.get("webhook_id") \
        if isinstance(webhook, dict) else None
    host = webhook.get("host") if isinstance(webhook, dict) else None
    if webhook_id is None and isinstance(url, str):
        webhook_id = webhook_id_for(url)
    if host is None and isinstance(url, str):
        host = webhook_host_label(url)
    return {"webhook_id": webhook_id, "host": host,
            "event": payload.get("event"),
            "version": payload.get("version"),
            "status": "skipped", "attempts": 0,
            "http_status": None, "error": "webhook_paused",
            "signed": bool(isinstance(webhook, dict)
                           and webhook.get("signing_secret"))}


def deliver_to_webhooks(webhooks, payload: dict, **kwargs) -> list:
    """Deliver one payload to every webhook entry (``{"url"}``
    dicts); one record per webhook, in registration order. An
    entry's own ``signing_secret`` (when present) signs its
    delivery, unless the caller passed a ``secret`` kwarg,
    which then applies to every entry. A malformed stored URL
    yields a failed record instead of raising — delivery
    problems are data, never exceptions."""
    records = []
    for entry in webhooks or []:
        url = entry.get("url") if isinstance(entry, dict) else None
        per_entry = dict(kwargs)
        if "secret" not in per_entry and isinstance(entry, dict):
            secret = entry.get("signing_secret")
            if isinstance(secret, str) and secret:
                per_entry["secret"] = secret
        try:
            records.append(
                deliver_notification(url, payload, **per_entry))
        except ValueError as exc:  # invalid stored URL
            records.append({
                "webhook_id": None, "host": None,
                "event": payload.get("event"),
                "version": payload.get("version"),
                "status": "failed", "attempts": 0,
                "http_status": None,
                "error": str(exc).split(":")[0],
                "signed": False})
    return records
