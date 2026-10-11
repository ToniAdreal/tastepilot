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
  transport for tests. No credential or key is ever sent: the
  only headers are the content type and accept;
- every attempt's outcome is a delivery record (webhook id,
  host, event, feed version, delivered/failed, attempt count,
  last HTTP status, error category). The feed store keeps the
  last ``MAX_DELIVERIES`` records per feed. A failed delivery
  never fails the feed operation that triggered it — the feed
  update stands, the failure is recorded, and the subscriber can
  read it back from the notifications surface;
- webhook URLs carrying userinfo (``user:pass@host``) are
  refused: a credential does not belong in a stored URL.

Email notifications are deliberately absent: sending mail needs
an account and credentials, both outside the build's hard
boundaries. The remaining step to a hosted push service is a
named provider's own API behind this same registry.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_WEBHOOK_URL_CHARS = 500
MAX_WEBHOOKS_PER_FEED = 4
MAX_DELIVERIES = 20
MAX_ATTEMPTS = 3
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


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


def build_notification(feed_id: str, kind: str, version: int,
                         etag: str, timelines, all_passed,
                         at: str) -> dict:
    """The pushed payload: exactly the facts the feed's change
    log records (kind, version, etag, timeline names, the
    recomputed-at-write verdict) plus the feed id and the
    event name — nothing else, and never a plan body."""
    event = {"created": "multi_feed.created",
             "updated": "multi_feed.updated",
             "subscribed": "multi_feed.subscribed"}.get(
                 kind, f"multi_feed.{kind}")
    return {"event": event, "kind": kind, "feed_id": feed_id,
            "version": version, "etag": etag,
            "timelines": list(timelines or []),
            "all_passed": all_passed, "at": at}


def deliver_notification(url: str, payload: dict, transport=None,
                         timeout: float = 5.0,
                         max_attempts: int = MAX_ATTEMPTS,
                         sleeper=None) -> dict:
    """POST one notification to one webhook; returns its
    delivery record.

    ``transport`` is injectable for tests: a callable
    ``(url, body_bytes, timeout) -> (status, body_bytes)``. The
    default transport is a plain urllib POST with no headers
    beyond the JSON content type — no key or credential is ever
    sent. Transient failures are retried with exponential
    backoff through the injectable ``sleeper``; every other
    outcome is final. The record names the webhook by id + host
    label only and carries an error *category*, never the URL,
    its query string, or a response body.
    """
    url = validate_webhook_url(url)
    record = {"webhook_id": webhook_id_for(url),
              "host": webhook_host_label(url),
              "event": payload.get("event"),
              "version": payload.get("version"),
              "status": "failed", "attempts": 0,
              "http_status": None, "error": None}
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
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json",
                             "accept": "application/json"})
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


def deliver_to_webhooks(webhooks, payload: dict, **kwargs) -> list:
    """Deliver one payload to every webhook entry (``{"url"}``
    dicts); one record per webhook, in registration order. A
    malformed stored URL yields a failed record instead of
    raising — delivery problems are data, never exceptions."""
    records = []
    for entry in webhooks or []:
        url = entry.get("url") if isinstance(entry, dict) else None
        try:
            records.append(deliver_notification(url, payload, **kwargs))
        except ValueError as exc:  # invalid stored URL
            records.append({
                "webhook_id": None, "host": None,
                "event": payload.get("event"),
                "version": payload.get("version"),
                "status": "failed", "attempts": 0,
                "http_status": None,
                "error": str(exc).split(":")[0]})
    return records
