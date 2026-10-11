"""Minimal stdlib HTTP server: chat UI + JSON plan API (Judge Mode demo).

Kept dependency-free so a judge can run the hosted-demo equivalent with
nothing but Python 3.11+:  python -m tastepilot serve --port 8765

Routes:
  GET  /                       demo UI (chat + grouped plan + evidence viewer)
  GET  /api/health             {"ok": true, "mode": ..., "explainer": {...}}
  POST /api/plan               {"text": ...} -> full plan result (agent.run).
                               The body may also carry browser-supplied
                               file contents — "imports": [{"filename",
                               "content"}, ...] (taste exports) and
                               "timetables": [{"filename", "content"}, ...]
                               (scheduled services) — parsed by the same
                               byte parsers as the CLI's --import-file /
                               --timetable-file, so the hosted demo offers
                               the import paths without any upload store:
                               contents are parsed for this plan only and
                               never persisted; only basename + format +
                               items are recorded as plan provenance.
                               A plain plan body stays capped at
                               MAX_BODY_BYTES; a body carrying attachments
                               may reach MAX_PLAN_BODY_BYTES. Malformed
                               attachments are categorised 400s that never
                               echo the submitted content.
  GET  /api/evidence/<id>      one sealed Qloo response from a plan this
                               server produced, plus its record's hash and
                               chain validity — the evidence-record viewer
                               backend. Unknown ids 404; nothing is served
                               that was not sealed into a plan's record.
  POST /api/verify             {"items": [...], "evidence": {...}} (or a
                               bare evidence record) -> the chain validity
                               and per-item checker verdict, recomputed
                               from the submitted record alone — the
                               record's own verdict field is reported
                               back as an untrusted claim, never used.
                               This is how anyone holding a shared plan
                               JSON re-checks it in the browser.
  POST /api/share              a plan JSON (same shape as /api/verify)
                               -> {"share_id", "url", "verify_url"}: the
                               plan is stored under a content-addressed
                               id (SHA-256 of its canonical JSON), so
                               anyone with the link /?share=<id> can
                               fetch and re-verify it without pasting
                               JSON by hand. Storing never trusts the
                               plan: verification is always recomputed.
  GET  /api/share/<id>         the stored plan JSON (404 if unknown,
                               expired, revoked, or evicted from the
                               bounded share store).
  GET  /api/share/<id>/verify  the recomputed chain + per-item checker
                               verdict for the stored plan — exactly the
                               /api/verify report, plus the share id
                               and the link's expiry.
  POST /api/export?format=...  a plan JSON (same shape/gates as
                               /api/verify) -> the take-away itinerary
                               in markdown (default), text, or ics,
                               rendered by tastepilot.export with its
                               verification recomputed — a tampered
                               plan exports with a NOT VERIFIED banner,
                               never a clean one. ics on a plan with no
                               dated leg -> 400.
  POST /api/suggest            {"plan": {...}, "limit": 5,
                               "history": [{...}, ...]} -> guided
                               branch suggestions: every admissible
                               primary/alternative pair ranked by
                               affinity delta + taste coverage + the
                               learned adjustment from the sealed
                               swap history (the plan's own plus any
                               history plans; an unverifiable one is
                               ignored and counted), as branch specs
                               /api/branches accepts. When the caller
                               also presents an ``X-Owner-Token`` with
                               a stored preference profile (see
                               /api/profile), that profile is merged
                               into the ranking under the same caps
                               and the result's learning field reports
                               ``stored_applied``. Suggestions
                               propose only — nothing is applied,
                               mutated, or re-sealed.
  POST /api/diff               {"base": {...}, "other": {...}} -> the
                               plan comparison (tastepilot.diff):
                               items added/removed/kept between the
                               two plans, affinity changes on kept
                               slots, alternatives and schedule
                               changes, and BOTH plans' verdicts
                               recomputed from their own sealed
                               records — a stored checker field is
                               never read on either side.
  POST /api/timeline           {"base": {...}, "branches":
                               [{"name": ..., "plan": {...}}, ...]}
                               -> the visual branch timeline data
                               (tastepilot.timeline): recommendation
                               slots grouped by leg, one column per
                               branch, every cell kept/added/removed/
                               re-scored/absent, and every column's
                               verdict recomputed from its own sealed
                               record — a tampered branch is a FAIL
                               column in the grid, never a 400.
  POST /api/export-timeline?format=markdown|text|ics
                               {"base": {...}, "branches": [...]}
                               -> the branch timeline as a take-away
                               export (tastepilot.export.export_timeline):
                               same gates as /api/timeline, rendered
                               as a download whose banner and column
                               verdicts are recomputed — a tampered
                               branch exports as a FAIL column under
                               a NOT VERIFIED banner, never clean.
                               ics renders one event series per
                               branch; a dateless timeline -> 400
                               (no_dated_legs).
  POST /api/feeds              {"base": {...}, "branches": [...]}
                               -> create a live calendar subscription
                               feed for that branch timeline: the
                               timeline is gated exactly as the ics
                               export is (a dateless timeline is
                               refused no_dated_legs), stored under
                               a stable feed id, and served at the
                               returned url so a calendar app can
                               subscribe and poll it.
  GET  /api/feeds/<id>.ics     the feed's current timeline, rendered
                               fresh as iCalendar on every fetch —
                               when the feed is updated, the next
                               poll serves the new series under the
                               same URL. A tampered branch's series
                               reads NOT VERIFIED, never PASS.
  GET  /api/feeds/<id>         the feed's metadata plus its timeline
                               verdict, recomputed on the spot.
  POST /api/feeds/<id>         replace the feed's timeline in place
                               (same id, expiry renewed) — how a
                               re-sealed branch reaches subscribers.
                               An owner-managed feed requires its
                               owner's token (403 otherwise).
  DELETE /api/feeds/<id>       revoke a feed: its URL 404s from then
                               on; the ownership rule matches share
                               revocation (an unowned feed may be
                               revoked by whoever holds its URL).
  POST /api/multi-feeds        {"timelines": [{"name", "base",
                               "branches"}, ...]} -> create a
                               multi-timeline subscription feed: one
                               stable URL whose calendar carries
                               every timeline in the bundle (each
                               gated exactly like the ics export).
                               The feed is versioned (version 1 at
                               creation) with a capped change log.
  GET  /api/multi-feeds/<id>.ics
                               the bundle rendered fresh as ONE
                               iCalendar on every fetch, with an
                               ETag: a poller presenting the etag in
                               If-None-Match gets 304 while nothing
                               changed — the poll-based change
                               notification. Push is opt-in per feed
                               via registered webhooks (below).
  GET  /api/multi-feeds/<id>   metadata + per-timeline recomputed
                               verdicts + version + etag.
  GET  /api/multi-feeds/<id>/changes
                               the change log: one entry per
                               creation/update (version, time,
                               timeline names, verdict at write).
  POST /api/multi-feeds/<id>   replace the bundle in place (same
                               id, version incremented, change
                               logged, expiry renewed; owner rule
                               as for feeds). A successful update
                               also pushes a multi_feed.updated
                               notification to every webhook
                               registered on the feed (below) and
                               records each delivery.
  DELETE /api/multi-feeds/<id> revoke a multi-feed (owner rule as
                               for feeds).
  POST /api/multi-feeds/<id>/webhooks
                               {"url": ..., "secret": ...,
                               "events": [...]} ->
                               register a push webhook
                               (tastepilot.notify) on the feed:
                               the URL is stored inside the feed
                               entry (it is the delivery address)
                               and named everywhere else by host
                               label only. An optional "events"
                               list sets the webhook's event
                               filter (no list = every event).
                               Registration fires one
                               multi_feed.subscribed test delivery
                               to the new webhook — unless its
                               own filter excludes that event.
                               Deliveries are
                               HMAC-signed when the webhook has
                               a signing secret: the subscriber
                               supplies "secret", or the server
                               generates one and returns it in
                               this response only (an explicit
                               null secret stays unsigned). Owner
                               rule as for the feed; at most 4
                               webhooks per feed.
  GET  /api/multi-feeds/<id>/webhooks
                               the feed's registered webhooks
                               (webhook id + host label + created
                               time + signed flag; never the
                               stored URLs or secrets).
  DELETE /api/multi-feeds/<id>/webhooks/<webhook_id>
                               remove one webhook (owner rule as
                               for the feed).
  POST /api/multi-feeds/<id>/webhooks/<webhook_id>/pause
                               pause one webhook (owner rule as
                               for the feed): later pushes to it
                               are recorded as skipped, never
                               attempted. A webhook also pauses
                               itself after 3 consecutive failed
                               deliveries (reason "auto",
                               stamped with the moment); after
                               a 1-hour cooldown the next update
                               push probes an auto-paused
                               webhook — a hand pause (reason
                               "manual") is never probed.
  POST /api/multi-feeds/<id>/webhooks/<webhook_id>/resume
                               resume a paused webhook (owner
                               rule as for the feed), clearing
                               its consecutive-failure count.
  POST /api/multi-feeds/<id>/webhooks/<webhook_id>/rotate-secret
                               replace a webhook's signing
                               secret in place (owner rule as
                               for the feed): no body generates
                               a fresh secret, returned in
                               this response only; {"secret":
                               ...} installs the subscriber's
                               own; an explicit null un-signs
                               the webhook. The old secret
                               stops verifying immediately;
                               pause state and failure count
                               are untouched.
  POST /api/multi-feeds/<id>/webhooks/<webhook_id>/rotate-url
                               point a webhook at a new
                               endpoint URL in place (owner
                               rule as for the feed):
                               {"url": ...} is required and
                               validated exactly as at
                               registration. The webhook's id
                               is recomputed from the new URL;
                               its signing secret, pause
                               state, failure count, and
                               registration moment survive,
                               and the rotation moment is
                               stamped (url_rotated_at).
                               Rotating onto a URL already
                               registered on the feed is
                               refused; rotating to the
                               current URL is idempotent.
                               Delivery records are never
                               edited — they keep the old id
                               and host as the audit trail.
  POST /api/multi-feeds/<id>/webhooks/<webhook_id>/set-events
                               replace a webhook's event
                               filter in place (owner rule as
                               for the feed): {"events":
                               [...]} names the events the
                               webhook subscribes to
                               (validated exactly as at
                               registration, where an optional
                               "events" list sets the initial
                               filter); an explicit null
                               resets it to every event. The
                               change is stamped
                               (events_updated_at); the id,
                               URL, signing secret, pause
                               state, failure count, and
                               registration moment survive,
                               and setting the current filter
                               is idempotent. A push whose
                               event a webhook filtered out
                               is not sent to it and leaves
                               no delivery record.
  POST /api/multi-feeds/<id>/webhooks/<webhook_id>/test
                               fire one on-demand test push
                               (event multi_feed.test) to that
                               webhook alone (owner rule as
                               for the feed): the subscriber's
                               explicit act, sent regardless
                               of the webhook's event filter,
                               carrying the feed's current
                               version, etag, timeline names,
                               and verdict. A paused webhook
                               is refused (webhook_paused);
                               the delivery is recorded like
                               any push and counts toward the
                               webhook's delivery health.
  GET  /api/multi-feeds/<id>/notifications
                               the feed's capped push delivery
                               log: one record per delivery
                               attempt (registration pings and
                               update pushes), with its outcome —
                               how a subscriber audits what was
                               pushed without any registry beyond
                               their own feed's webhooks.
  POST /api/multi-feeds/<id>/notifications/<delivery_id>/redeliver
                               resend one FAILED delivery's
                               stored payload to its webhook's
                               currently registered URL (owner
                               rule as for the feed); the resend
                               is recorded as a new delivery
                               naming the original
                               (``redelivery_of``) and the
                               original record is never edited.
  POST /api/multi-feeds/<id>/notifications/redeliver-failed
                               bulk-resend every still-actionable
                               failed delivery in one call (owner
                               rule as for the feed): the latest
                               attempt of each failed chain is
                               resent and recorded as above; a
                               failed record a later resend
                               already superseded is reported
                               under ``skipped``, never resent.
  GET  /api/shares             the caller's own share links (dashboard):
                               requires an ``X-Owner-Token`` header and
                               returns only the shares that token owns —
                               id, url, expiry, item count, and the
                               recomputed checker verdict per link.
  GET  /api/profile            the caller's persistent preference
                               profile: requires an ``X-Owner-Token``
                               header; returns the stored profile (the
                               learned ranking's tag/domain weights,
                               derived only from sealed swap histories)
                               or ``exists: false`` with a zero profile.
  POST /api/profile            {"history": [{...plan...}, ...]} ->
                               derive the lesson from those plans'
                               sealed swap histories and merge it into
                               the caller's stored profile (additive,
                               under the learner's caps; a chain-broken
                               plan is ignored and counted). There is
                               no way to store caller-supplied weights.
  DELETE /api/profile          clear the caller's stored profile
                               (200 when one existed, 404 otherwise).
                               DELETE anywhere else outside /api/share
                               and /api/feeds/<id> stays 405 like the
                               other methods.
  DELETE /api/share/<id>       revoke a share link: the stored plan is
                               dropped and the id 404s from then on
                               ({"revoked": true}). For an owner-managed
                               link (below) only the owner's token can
                               revoke it (403 otherwise); for a legacy
                               link created without a token, knowing the
                               id remains the capability — whoever holds
                               the link can revoke it; that is the
                               documented demo trade-off for unowned
                               links. DELETE is otherwise supported
                               only for /api/profile (above) and
                               /api/feeds/<id>; every other DELETE
                               target stays 405 like the other
                               unsupported methods.

Owner-managed shares (no account system):
  * A client may send an ``X-Owner-Token`` header when creating a
    share. The token is a bearer credential the client generates and
    keeps (the demo UI generates one and stores it in the browser's
    localStorage — a per-browser "account", no registration, no
    password, no email). The server stores only the token's SHA-256
    hash alongside the entry, never the token itself, so a leaked
    share file does not leak owner credentials.
  * The first token to store a plan owns it: re-storing the same plan
    is still idempotent (same id, original expiry) and never
    transfers ownership to a later token.
  * ``GET /api/shares`` lists only the presenting token's shares;
    revoking an owned share requires that same token. Shares created
    without a token ("legacy" shares) keep the original link-holder
    behaviour and never appear in any owner's list.
  * The token is portable across devices: pasting the same token
    into another browser gives that browser the same dashboard —
    that is the cross-device sync story without an account system.
    Responses name the token only by its fingerprint (the first 12
    hex chars of its hash), so two browsers can confirm they hold
    the same token without exposing the credential.

Persistence, expiry, revocation:
  * The share store is in-memory by default (the original behaviour).
    With a file path — ``serve(..., share_file=...)``, the CLI's
    ``--share-file``, or the ``TASTEPILOT_SHARE_FILE`` env var — it is
    backed by a JSON file written atomically (temp file + rename) on
    every mutation, so links survive a restart. A missing or corrupt
    file starts an empty store instead of crashing the demo; the file
    holds only shared plan payloads (fictional demo data, no keys).
  * Every link expires: ``DEFAULT_SHARE_TTL_SECONDS`` (7 days) after
    it was first stored, re-checks 404 exactly like an unknown id.
    Re-storing the same plan is idempotent and does not extend the
    original expiry. Expired entries are pruned lazily on access and
    eagerly on every store.
  * The store stays bounded (``MAX_SHARED_PLANS``, oldest evicted
    first) in both modes.
  * Timeline feeds follow the same pattern in a TimelineFeedStore:
    in-memory by default, file-backed via ``serve(...,
    feed_file=...)``, ``--feed-file``, or ``TASTEPILOT_FEED_FILE``;
    bounded (``MAX_TIMELINE_FEEDS``); expiring after
    ``DEFAULT_FEED_TTL_SECONDS`` (30 days) unless the owner updates
    the feed, which renews it; revocable via DELETE. A feed's .ics
    response is the one API response that is not ``no-store`` — it
    carries a short public cache lifetime so calendar apps re-poll
    instead of caching the series forever.
  * Multi-timeline feeds follow the same pattern in a
    MultiTimelineFeedStore: file-backed via ``serve(...,
    multi_feed_file=...)``, ``--multi-feed-file``, or
    ``TASTEPILOT_MULTI_FEED_FILE``; bounded
    (``MAX_MULTI_TIMELINE_FEEDS``); same 30-day expiry/renewal and
    revocation. Each entry additionally carries a version and a
    capped change log (persisted with the entry), and its .ics
    response carries an ETag so pollers can detect changes with
    If-None-Match instead of downloading the calendar. A feed
    entry also carries its registered webhooks and its capped
    delivery log (both persisted with the entry): on an in-place
    update the server pushes a notification to every webhook
    via tastepilot.notify and records each delivery; a failed
    delivery never fails the update.

Hardening (Oct 21–24 milestone):
  * Request bodies are capped (``MAX_BODY_BYTES``) and plan text is capped
    (``MAX_TEXT_CHARS``, enforced again inside the agent) — oversize
    requests get 413, malformed JSON / missing text gets 400, and an
    unexpected agent failure gets a generic 500 that never leaks the
    internal exception text.
  * Every response carries security headers (nosniff, frame denial,
    no-referrer, a CSP that only allows this page's own inline UI code)
    and API responses are ``Cache-Control: no-store``.
  * The evidence cache is bounded (``MAX_EVIDENCE_RECORDS``, oldest
    evicted first) so a long-lived hosted demo cannot grow without limit;
    the shared-plan store behind verification links is bounded the same
    way (``MAX_SHARED_PLANS``).
  * ``serve()`` takes an explicit host: local runs keep the safe
    127.0.0.1 default; hosted deployments pass ``--host 0.0.0.0`` and the
    port from the platform's ``PORT`` env var (see DEPLOY.md).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .checker import check_plan, item_reports
from .diff import diff_plans
from .evidence import qloo_responses, verify_chain
from .export import (TIMELINE_FORMATS, export_multi_timeline_ics,
                     export_plan, export_timeline, media_for,
                     validate_multi_timelines)
from .imports import parse_import_bytes
from .notify import (MAX_DELIVERIES, MAX_WEBHOOKS_PER_FEED,
                     PAUSE_AFTER_CONSECUTIVE_FAILURES,
                     PAUSE_COOLDOWN_SECONDS, WEBHOOK_EVENTS,
                     build_notification, deliver_to_webhooks,
                     delivery_id_for, generate_signing_secret,
                     skipped_record, validate_signing_secret,
                     validate_webhook_events,
                     validate_webhook_url,
                     webhook_host_label, webhook_id_for,
                     webhook_subscribes_to)
from .profiles import ProfileStore
from .timeline import branch_timeline
from .swap import (explore_branches, suggest_branches, swap_history,
                   swap_plan, undo_last_swap)
from .timetable import parse_timetable_bytes

STATIC = Path(__file__).resolve().parent / "static"

MAX_BODY_BYTES = 16_384          # a plan request is a short JSON object
MAX_VERIFY_BODY_BYTES = 524_288  # a submitted plan + sealed record (~25 KiB
                                 # for the shipped samples) needs more room,
                                 # but stays bounded like every other input
MAX_TEXT_CHARS = 2_000           # matches the agent-level guard
MAX_PLAN_BODY_BYTES = MAX_VERIFY_BODY_BYTES  # a plan request carrying
                                 # browser-supplied taste/timetable file
                                 # contents needs the verify cap, not the
                                 # plain-body cap (which still applies to
                                 # any body without attachments)
MAX_PLAN_ATTACHMENTS = 4         # import (and timetable) files per plan
MAX_ATTACHMENT_NAME_CHARS = 120  # a filename is a label, not a document
MAX_EVIDENCE_RECORDS = 512       # sealed responses kept for the viewer
MAX_SHARED_PLANS = 128           # shared plans kept for link verification
MAX_SHARE_BODY_BYTES = MAX_VERIFY_BODY_BYTES  # a shared plan is a plan
DEFAULT_SHARE_TTL_SECONDS = 7 * 24 * 3600  # share links live for 7 days
SHARE_FILE_ENV_VAR = "TASTEPILOT_SHARE_FILE"  # opt-in persistence path
MAX_TIMELINE_FEEDS = 64          # live calendar feeds kept at once
DEFAULT_FEED_TTL_SECONDS = 30 * 24 * 3600  # feeds live for 30 days;
                                 # an explicit update renews a feed,
                                 # because a subscription is long-lived
                                 # by design (unlike a share link)
FEED_FILE_ENV_VAR = "TASTEPILOT_FEED_FILE"  # opt-in persistence path
MAX_MULTI_TIMELINE_FEEDS = 32    # multi-timeline feeds kept at once
MAX_MULTI_FEED_BODY_BYTES = 2 * 1024 * 1024  # a bundle carries up to
                                 # six timelines, each a base plan plus
                                 # branch plans — bounded, but larger
                                 # than the single-plan verify cap
MULTI_FEED_FILE_ENV_VAR = "TASTEPILOT_MULTI_FEED_FILE"  # persistence
MAX_WEBHOOK_BODY_BYTES = MAX_BODY_BYTES  # a webhook registration is
                                 # one small {"url": ...} object
PROFILE_FILE_ENV_VAR = "TASTEPILOT_PROFILE_FILE"  # opt-in profile store
MAX_PROFILES = 256               # owners with a persistent profile
OWNER_TOKEN_HEADER = "X-Owner-Token"  # per-browser owner credential
MIN_OWNER_TOKEN_CHARS = 16   # shorter than this is not a credential
MAX_OWNER_TOKEN_CHARS = 200

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The demo UI is a single self-contained page with inline style/script;
    # everything else defaults to same-origin only.
    "Content-Security-Policy":
        "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
}


def parse_plan_attachments(payload: dict):
    """Parse a plan body's browser-supplied file contents.

    ``payload["imports"]`` / ``payload["timetables"]`` are lists of
    ``{"filename", "content"}`` objects — the file contents the demo
    page read in the browser (File.text()), not an upload store: they
    are parsed here by the exact byte parsers the CLI uses for
    --import-file / --timetable-file and are never persisted. Returns
    ``(imported, timetables)`` in the shapes agent.run consumes.
    Raises ValueError with a categorised, content-free message on any
    malformed shape or unparseable file (the parsers' own categories —
    bad_json / no_items / too_large / ... — never echo file contents).
    """
    imported: list[dict] = []
    timetables: list[dict] = []
    for key, parser, label in (
            ("imports", parse_import_bytes, "import"),
            ("timetables", parse_timetable_bytes, "timetable")):
        raw = payload.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(f"{label} attachments must be a list")
        if len(raw) > MAX_PLAN_ATTACHMENTS:
            raise ValueError(f"too_many: at most {MAX_PLAN_ATTACHMENTS} "
                             f"{label} files per plan")
        for entry in raw:
            if not isinstance(entry, dict) or \
                    not isinstance(entry.get("filename"), str) or \
                    not isinstance(entry.get("content"), str):
                raise ValueError(
                    f"bad_attachment: each {label} file needs a string "
                    f"filename and string content")
            filename = entry["filename"]
            if len(filename) > MAX_ATTACHMENT_NAME_CHARS:
                raise ValueError("bad_attachment: filename too long")
            try:
                parsed = parser(entry["content"].encode("utf-8"),
                                filename=filename)
            except ValueError as exc:
                raise ValueError(f"{label} failed: {exc}") from exc
            (imported if key == "imports" else timetables).append(parsed)
    return imported, timetables


def share_id_for(payload: dict) -> str:
    """Content-addressed id for a plan payload: SHA-256 (truncated to
    32 hex chars) of its canonical JSON. Identical plans share an id —
    storing the same plan twice is idempotent — and the id reveals
    nothing beyond the plan itself, which the link holder can fetch."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:32]


def validate_plan_payload(payload) -> str | None:
    """Shape gate shared by /api/verify and /api/share.

    Returns an error string for a malformed payload, else None. This is
    a shape check only — it never judges the plan's evidence, which the
    checker recomputes separately on every read."""
    if not isinstance(payload, dict):
        return "plan must be a JSON object"
    record = payload.get("evidence", payload)
    if not isinstance(record, dict) or \
            not isinstance(record.get("entries"), list):
        return "evidence record missing or malformed"
    items = payload.get("items", [])
    if not isinstance(items, list) or \
            any(not isinstance(i, dict) for i in items):
        return "items must be a list of objects"
    alternatives = payload.get("alternatives", [])
    if not isinstance(alternatives, list) or \
            any(not isinstance(i, dict) for i in alternatives):
        return "alternatives must be a list of objects"
    return None


def verification_report(payload: dict) -> dict:
    """Recompute the chain + per-item checker verdict for a payload.

    The payload must already have passed validate_plan_payload. The
    record's own verdict field is echoed only as an untrusted claim.
    Raises the checker's malformed-input errors for the caller to map
    to a 400."""
    record = payload.get("evidence", payload)
    items = payload.get("items", [])
    alternatives = payload.get("alternatives", [])
    # Alternatives are recommendations too: their reports are
    # recomputed here and a bad alternative fails the whole report.
    alt_reports = item_reports(alternatives, record)
    report = check_plan(items, record)
    violations = list(report["violations"]) + [
        {"item": r["item"], "reason": r["reason"]}
        for r in alt_reports if not r["admissible"]]
    # The swap audit trail is recomputed from the sealed swap_event
    # entries; the plan's stored swaps list is only a claim. A plan
    # whose chain does not verify gets an empty, unverified history
    # alongside its FAIL verdict — never a 400, never the claimed
    # events presented as audited.
    try:
        history = swap_history(payload if "evidence" in payload
                               else {"evidence": record})
        swaps_verified = True
    except ValueError:
        history, swaps_verified = [], False
    claimed = payload.get("swaps")
    return {
        "chain_valid": report["chain_valid"],
        "passed": report["passed"] and
                  all(r["admissible"] for r in alt_reports),
        "violations": violations,
        "items": item_reports(items, record),
        "alternatives": alt_reports,
        "item_count": len(items),
        "alternative_count": len(alternatives),
        "sealed_responses": len(qloo_responses(record)),
        "record_verdict": record.get("verdict"),
        "swap_history": history,
        "swap_count": len(history),
        "swaps_verified": swaps_verified,
        "swaps_claimed": len(claimed) if isinstance(claimed, list) else 0,
    }


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _epoch_of(iso_value) -> float | None:
    """Epoch seconds for an ``_iso`` timestamp, or None when
    the stored value is missing or malformed — a bad stored
    stamp means 'no known moment', never a guessed one."""
    if not isinstance(iso_value, str):
        return None
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def owner_token_hash(token) -> str | None:
    """SHA-256 hash of an owner token, or None for no/invalid token.

    Only the hash is ever stored: a share-file leak then exposes plan
    payloads (fictional demo data) but not the owner credentials that
    manage them. A token is valid at 16–200 chars with no whitespace."""
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not MIN_OWNER_TOKEN_CHARS <= len(token) <= MAX_OWNER_TOKEN_CHARS:
        return None
    if any(ch.isspace() for ch in token):
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def owner_fingerprint(token) -> str | None:
    """Short, non-credential identifier for an owner token: the first
    12 hex chars of its stored hash, or None for an invalid token.

    A fingerprint lets two browsers confirm they hold the *same*
    token (cross-device portability) without either side exposing
    the token or its full hash — 48 bits identify the token for a
    human check but cannot be used as the credential, and reversing
    them into a valid token is a preimage search."""
    digest = owner_token_hash(token)
    return digest[:12] if digest is not None else None


class ShareStore:
    """Bounded store behind the shareable verification links.

    Entries are content-addressed (see ``share_id_for``) and carry
    ``created_at`` / ``expires_at`` stamps from an injectable clock.
    An entry whose expiry has passed is indistinguishable from one
    that never existed: every read prunes it first. ``revoke`` drops
    an entry immediately. With ``path`` set, every mutation rewrites
    that JSON file atomically (temp file in the same directory +
    ``os.replace``), so a restart reloads the surviving links; a
    missing, unreadable, or corrupt file loads as an empty store —
    persistence must never take the demo down. Payloads are
    deep-copied in and out so callers can never mutate stored state.
    """

    def __init__(self, max_plans: int = MAX_SHARED_PLANS,
                 ttl_seconds: float = DEFAULT_SHARE_TTL_SECONDS,
                 path: str | Path | None = None, clock=time.time):
        if max_plans < 1:
            raise ValueError("max_plans must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.max_plans = max_plans
        self.ttl_seconds = ttl_seconds
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        # share_id -> {"payload": dict, "created_at": float,
        #              "expires_at": float}, oldest first.
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data["entries"]
            if not isinstance(entries, list):
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return  # missing/corrupt file: start empty, never crash
        now = self._clock()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            share_id = entry.get("share_id")
            payload = entry.get("payload")
            expires_at = entry.get("expires_at")
            if not isinstance(share_id, str) or \
                    not isinstance(payload, dict) or \
                    not isinstance(expires_at, (int, float)) or \
                    expires_at <= now:
                continue  # skip malformed or already-expired rows
            created_at = entry.get("created_at")
            loaded = {
                "payload": copy.deepcopy(payload),
                "created_at": created_at if isinstance(
                    created_at, (int, float)) else expires_at,
                "expires_at": float(expires_at),
            }
            # Owner hashes survive restarts; a malformed one is
            # dropped (the entry degrades to a legacy unowned share)
            # rather than failing the row.
            if isinstance(entry.get("owner_hash"), str):
                loaded["owner_hash"] = entry["owner_hash"]
            self._entries[share_id] = loaded
            self._entries.move_to_end(share_id)
        while len(self._entries) > self.max_plans:
            self._entries.popitem(last=False)

    def _save(self) -> None:
        """Persist the current entries atomically. Caller holds the
        lock. A write failure (e.g. a read-only filesystem in a
        container) degrades to in-memory behaviour for this mutation
        instead of failing the request — the demo stays up."""
        if self.path is None:
            return
        data = {"version": 1, "entries": [
            {"share_id": share_id, **entry}
            for share_id, entry in self._entries.items()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- internals ---------------------------------------------------------

    def _prune_expired(self) -> bool:
        """Drop expired entries. Caller holds the lock. Returns True
        when anything was dropped (so the caller persists the change)."""
        now = self._clock()
        expired = [sid for sid, e in self._entries.items()
                   if e["expires_at"] <= now]
        for sid in expired:
            del self._entries[sid]
        return bool(expired)

    @staticmethod
    def _meta(share_id: str, entry: dict) -> dict:
        owner_hash = entry.get("owner_hash")
        return {"share_id": share_id,
                "created_at": _iso(entry["created_at"]),
                "expires_at": _iso(entry["expires_at"]),
                "owned": owner_hash is not None,
                "owner_fingerprint": owner_hash[:12]
                if owner_hash is not None else None}

    # -- public API --------------------------------------------------------

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def info(self) -> dict:
        with self._lock:
            if self._prune_expired():
                self._save()
            return {"persistent": self.persistent,
                    "count": len(self._entries),
                    "max": self.max_plans,
                    "ttl_seconds": self.ttl_seconds}

    def put(self, payload: dict, owner_token: str | None = None) -> dict:
        """Store a plan payload and return its link metadata.

        Idempotent for identical content: re-storing the same plan
        returns the original entry — the expiry is NOT extended, so a
        link's lifetime cannot be quietly renewed by re-posting it,
        and ownership is NOT transferred: the first token to store a
        plan owns it, and a later re-store (with or without a token)
        changes neither fact. Only the owner token's hash is kept;
        an invalid token is treated as no token (the HTTP layer
        rejects invalid tokens with a 400 before they reach here)."""
        share_id = share_id_for(payload)
        with self._lock:
            self._prune_expired()
            entry = self._entries.get(share_id)
            if entry is None:
                now = self._clock()
                entry = {"payload": copy.deepcopy(payload),
                         "created_at": now,
                         "expires_at": now + self.ttl_seconds}
                digest = owner_token_hash(owner_token)
                if digest is not None:
                    entry["owner_hash"] = digest
                self._entries[share_id] = entry
                while len(self._entries) > self.max_plans:
                    self._entries.popitem(last=False)
            else:
                self._entries.move_to_end(share_id)
            self._save()
            return self._meta(share_id, entry)

    def list_for_owner(self, owner_token: str | None) -> list[dict]:
        """Link metadata for every live share the token owns, oldest
        first, each with its plan's item count. An absent or invalid
        token owns nothing (empty list) — and legacy unowned shares
        never appear in any owner's list."""
        digest = owner_token_hash(owner_token)
        if digest is None:
            return []
        with self._lock:
            if self._prune_expired():
                self._save()
            out = []
            for share_id, entry in self._entries.items():
                if entry.get("owner_hash") != digest:
                    continue
                meta = self._meta(share_id, entry)
                items = entry["payload"].get("items")
                meta["item_count"] = len(items) \
                    if isinstance(items, list) else 0
                out.append(meta)
            return out

    def get(self, share_id: str) -> dict | None:
        """The stored payload (a deep copy), or None when the id is
        unknown, expired, revoked, or evicted."""
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(share_id)
            if entry is None:
                return None
            self._entries.move_to_end(share_id)
            return copy.deepcopy(entry["payload"])

    def meta(self, share_id: str) -> dict | None:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(share_id)
            return None if entry is None else self._meta(share_id, entry)

    def revoke_status(self, share_id: str,
                      owner_token: str | None = None) -> str:
        """Attempt a revocation and say exactly what happened:
        ``"revoked"``, ``"not_found"`` (unknown or already-expired
        id), or ``"forbidden"`` — the share is owner-managed and the
        presented token is absent, invalid, or not the owner's.
        Legacy unowned shares may be revoked by any caller, exactly
        as before owner management existed."""
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(share_id)
            if entry is None:
                return "not_found"
            digest = entry.get("owner_hash")
            if digest is not None and \
                    owner_token_hash(owner_token) != digest:
                return "forbidden"
            del self._entries[share_id]
            self._save()
            return "revoked"

    def revoke(self, share_id: str,
               owner_token: str | None = None) -> bool:
        """Drop an entry now. True when something was revoked; an
        unknown or already-expired id revokes nothing (False), and
        neither does a non-owner token on an owned share."""
        return self.revoke_status(share_id, owner_token) == "revoked"


class TimelineFeedStore:
    """Bounded store behind the live calendar subscription feeds.

    A feed holds one branch-timeline payload (``{"base", "branches"}``)
    under a stable, unguessable id (128 bits from ``secrets`` — unlike
    a share id it is NOT content-addressed, because a subscription's
    whole point is that its content changes under the same URL when
    the owner updates it in place). Every fetch re-renders the stored
    timeline, so an update — a branch re-sealed after another swap —
    is what subscribers see on their next poll. Entries carry
    ``created_at`` / ``updated_at`` / ``expires_at`` stamps from an
    injectable clock; expiry makes a feed indistinguishable from an
    unknown one, and an explicit update is the only renewal (it is an
    owner's deliberate act, not the share store's idempotent re-post).
    Optional ownership mirrors the share store: only the token's
    SHA-256 hash is kept, and updating or revoking an owned feed
    requires that token. With ``path`` set, every mutation rewrites
    that JSON file atomically, exactly like the share store; a
    missing or corrupt file starts an empty store. Payloads are
    deep-copied in and out."""

    def __init__(self, max_feeds: int = MAX_TIMELINE_FEEDS,
                 ttl_seconds: float = DEFAULT_FEED_TTL_SECONDS,
                 path: str | Path | None = None, clock=time.time):
        if max_feeds < 1:
            raise ValueError("max_feeds must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.max_feeds = max_feeds
        self.ttl_seconds = ttl_seconds
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data["entries"]
            if not isinstance(entries, list):
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return
        now = self._clock()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            feed_id = entry.get("feed_id")
            payload = entry.get("payload")
            expires_at = entry.get("expires_at")
            if not isinstance(feed_id, str) or \
                    not isinstance(payload, dict) or \
                    not isinstance(payload.get("base"), dict) or \
                    not isinstance(payload.get("branches"), list) or \
                    not isinstance(expires_at, (int, float)) or \
                    expires_at <= now:
                continue
            created_at = entry.get("created_at")
            updated_at = entry.get("updated_at")
            loaded = {
                "payload": copy.deepcopy(payload),
                "created_at": created_at if isinstance(
                    created_at, (int, float)) else expires_at,
                "updated_at": updated_at if isinstance(
                    updated_at, (int, float)) else expires_at,
                "expires_at": float(expires_at),
            }
            if isinstance(entry.get("owner_hash"), str):
                loaded["owner_hash"] = entry["owner_hash"]
            self._entries[feed_id] = loaded
            self._entries.move_to_end(feed_id)
        while len(self._entries) > self.max_feeds:
            self._entries.popitem(last=False)

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"version": 1, "entries": [
            {"feed_id": feed_id, **entry}
            for feed_id, entry in self._entries.items()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune_expired(self) -> bool:
        now = self._clock()
        expired = [fid for fid, e in self._entries.items()
                   if e["expires_at"] <= now]
        for fid in expired:
            del self._entries[fid]
        return bool(expired)

    @staticmethod
    def _meta(feed_id: str, entry: dict) -> dict:
        owner_hash = entry.get("owner_hash")
        payload = entry["payload"]
        branches = payload.get("branches") or []
        return {"feed_id": feed_id,
                "created_at": _iso(entry["created_at"]),
                "updated_at": _iso(entry["updated_at"]),
                "expires_at": _iso(entry["expires_at"]),
                "owned": owner_hash is not None,
                "owner_fingerprint": owner_hash[:12]
                if owner_hash is not None else None,
                "branches": [b.get("name") if isinstance(b, dict)
                             else None for b in branches],
                "columns": 1 + len(branches)}

    def _new_id(self) -> str:
        for _ in range(100):
            feed_id = secrets.token_hex(16)
            if feed_id not in self._entries:
                return feed_id
        raise ValueError("feed_id_exhausted")

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def info(self) -> dict:
        with self._lock:
            if self._prune_expired():
                self._save()
            return {"persistent": self.persistent,
                    "count": len(self._entries),
                    "max": self.max_feeds,
                    "ttl_seconds": self.ttl_seconds}

    def create(self, payload: dict, owner_token=None) -> dict:
        with self._lock:
            self._prune_expired()
            feed_id = self._new_id()
            now = self._clock()
            entry = {"payload": copy.deepcopy(payload),
                     "created_at": now, "updated_at": now,
                     "expires_at": now + self.ttl_seconds}
            digest = owner_token_hash(owner_token)
            if digest is not None:
                entry["owner_hash"] = digest
            self._entries[feed_id] = entry
            while len(self._entries) > self.max_feeds:
                self._entries.popitem(last=False)
            self._save()
            return self._meta(feed_id, entry)

    def get(self, feed_id: str) -> dict | None:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return None
            return copy.deepcopy(entry["payload"])

    def meta(self, feed_id: str) -> dict | None:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            return None if entry is None else self._meta(feed_id, entry)

    def update_status(self, feed_id: str, payload: dict, owner_token=None):
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return "not_found", None
            digest = entry.get("owner_hash")
            if digest is not None and \
                    owner_token_hash(owner_token) != digest:
                return "forbidden", None
            now = self._clock()
            entry["payload"] = copy.deepcopy(payload)
            entry["updated_at"] = now
            entry["expires_at"] = now + self.ttl_seconds
            self._entries.move_to_end(feed_id)
            self._save()
            return "updated", self._meta(feed_id, entry)

    def revoke_status(self, feed_id: str, owner_token=None) -> str:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return "not_found"
            digest = entry.get("owner_hash")
            if digest is not None and \
                    owner_token_hash(owner_token) != digest:
                return "forbidden"
            del self._entries[feed_id]
            self._save()
            return "revoked"

    def revoke(self, feed_id: str, owner_token=None) -> bool:
        return self.revoke_status(feed_id, owner_token) == "revoked"


def multi_feed_etag(payload: dict, version: int) -> str:
    """Change detector for a multi-timeline feed: a truncated
    SHA-256 of the canonical payload + version. Any update — a
    replaced timeline, an added one — changes the etag, so a
    subscriber polling with If-None-Match learns "changed" (200
    with a new etag) vs "unchanged" (304) without downloading
    the calendar. The etag reveals nothing beyond the payload
    the feed URL already serves."""
    canonical = json.dumps({"version": version, "payload": payload},
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:32]


class MultiTimelineFeedStore:
    """Bounded store behind multi-timeline calendar subscriptions
    with change notification.

    One feed carries a whole bundle — {"timelines": [{"name",
    "base", "branches"}, ...]} — under one stable, unguessable
    id, so a single calendar subscription can carry many trips
    at once instead of one feed per timeline. Change notification
    is poll-based and explicit: every entry carries a version
    (incremented on each in-place update) and a capped change
    log (version, timestamp, kind, timeline names, recomputed
    all_passed), and the feed's etag (see multi_feed_etag) lets
    a poller — a calendar app or the changes endpoint — detect
    a change cheaply. There is no push channel and no subscriber
    registry: TastePilot keeps no list of who polls, and sends
    nothing to anyone. The generic push half built later
    (tastepilot.notify) is layered on this store as opt-in,
    per-feed webhook registrations with a capped delivery log;
    a feed without webhooks keeps exactly this poll-only
    behaviour.

    Everything else mirrors TimelineFeedStore: 30-day expiry
    renewed only by an explicit owner update, DELETE revocation,
    optional owner token stored only as its SHA-256 hash, and
    optional atomic JSON file persistence (a missing/corrupt
    file starts an empty store; malformed rows are skipped).
    Payloads are deep-copied in and out."""

    MAX_CHANGES = 20

    @staticmethod
    def _clean_webhook_row(row: dict) -> dict:
        """A webhook row as loaded from disk: copied, with a
        malformed signing secret dropped (the webhook then
        delivers unsigned) instead of failing the row — a bad
        stored secret must never take a feed down."""
        row = dict(row)
        secret = row.get("signing_secret")
        if secret is not None:
            try:
                row["signing_secret"] = \
                    validate_signing_secret(secret)
            except ValueError:
                row.pop("signing_secret", None)
        # Delivery-health fields, coerced to their defaults
        # when malformed (a legacy row has neither): a bad
        # stored counter must never take a feed down either.
        row["paused"] = row.get("paused") is True
        failures = row.get("consecutive_failures")
        row["consecutive_failures"] = failures \
            if isinstance(failures, int) \
            and not isinstance(failures, bool) and failures >= 0 \
            else 0
        # Pause provenance, coerced the same way: only a
        # stamped "auto"/"manual" reason survives, and only
        # alongside its moment. A paused legacy row carries
        # neither, so it reads as reason None — and a reason
        # that is not "auto" is never probe-eligible, which
        # keeps pre-probe rows (and hand pauses) manual-only.
        reason = row.get("paused_reason")
        row["paused_reason"] = reason \
            if row["paused"] and reason in ("auto", "manual") \
            else None
        paused_at = row.get("paused_at")
        row["paused_at"] = paused_at \
            if row["paused"] and _epoch_of(paused_at) is not None \
            else None
        # Endpoint-rotation provenance, coerced the same way:
        # only a parseable moment survives; a legacy row has
        # never been rotated and reads as None.
        rotated_at = row.get("url_rotated_at")
        row["url_rotated_at"] = rotated_at \
            if _epoch_of(rotated_at) is not None else None
        # Event filter, coerced the same way: a stored list
        # that validates survives (canonicalised); anything
        # else — missing (a legacy row), malformed, empty —
        # degrades to None, the no-filter value meaning every
        # event, so a bad filter never silences a webhook or
        # takes a feed down. The filter's update stamp is
        # coerced like the rotation stamp.
        try:
            row["events"] = validate_webhook_events(
                row.get("events"))
        except ValueError:
            row["events"] = None
        events_at = row.get("events_updated_at")
        row["events_updated_at"] = events_at \
            if _epoch_of(events_at) is not None else None
        return row

    def __init__(self, max_feeds: int = MAX_MULTI_TIMELINE_FEEDS,
                 ttl_seconds: float = DEFAULT_FEED_TTL_SECONDS,
                 path: str | Path | None = None, clock=time.time):
        if max_feeds < 1:
            raise ValueError("max_feeds must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.max_feeds = max_feeds
        self.ttl_seconds = ttl_seconds
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._load()

    @staticmethod
    def _timelines_of(payload) -> list:
        timelines = payload.get("timelines") if isinstance(
            payload, dict) else None
        return timelines if isinstance(timelines, list) else []

    @classmethod
    def _names(cls, payload) -> list:
        return [t.get("name") if isinstance(t, dict) else None
                for t in cls._timelines_of(payload)]

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data["entries"]
            if not isinstance(entries, list):
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return
        now = self._clock()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            feed_id = entry.get("feed_id")
            payload = entry.get("payload")
            expires_at = entry.get("expires_at")
            if not isinstance(feed_id, str) or \
                    not isinstance(payload, dict) or \
                    not self._timelines_of(payload) or \
                    not isinstance(expires_at, (int, float)) or \
                    expires_at <= now:
                continue
            created_at = entry.get("created_at")
            updated_at = entry.get("updated_at")
            version = entry.get("version")
            changes = entry.get("changes")
            loaded = {
                "payload": copy.deepcopy(payload),
                "created_at": created_at if isinstance(
                    created_at, (int, float)) else expires_at,
                "updated_at": updated_at if isinstance(
                    updated_at, (int, float)) else expires_at,
                "expires_at": float(expires_at),
                "version": version if isinstance(version, int)
                and version >= 1 else 1,
                "changes": [c for c in changes
                            if isinstance(c, dict)][-self.MAX_CHANGES:]
                if isinstance(changes, list) else [],
                "webhooks": [self._clean_webhook_row(w)
                             for w in entry.get("webhooks", [])
                             if isinstance(w, dict)
                             and isinstance(w.get("url"), str)
                             and isinstance(w.get("webhook_id"), str)
                             ][:MAX_WEBHOOKS_PER_FEED]
                if isinstance(entry.get("webhooks"), list) else [],
                "deliveries": [d for d in entry.get("deliveries", [])
                               if isinstance(d, dict)][-MAX_DELIVERIES:]
                if isinstance(entry.get("deliveries"), list) else [],
            }
            if isinstance(entry.get("owner_hash"), str):
                loaded["owner_hash"] = entry["owner_hash"]
            self._entries[feed_id] = loaded
            self._entries.move_to_end(feed_id)
        while len(self._entries) > self.max_feeds:
            self._entries.popitem(last=False)

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"version": 1, "entries": [
            {"feed_id": feed_id, **entry}
            for feed_id, entry in self._entries.items()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune_expired(self) -> bool:
        now = self._clock()
        expired = [fid for fid, e in self._entries.items()
                   if e["expires_at"] <= now]
        for fid in expired:
            del self._entries[fid]
        return bool(expired)

    def _change(self, entry: dict, kind: str, all_passed) -> dict:
        return {"version": entry["version"],
                "at": _iso(entry["updated_at"]), "kind": kind,
                "timelines": self._names(entry["payload"]),
                "all_passed": all_passed}

    def _meta(self, feed_id: str, entry: dict) -> dict:
        owner_hash = entry.get("owner_hash")
        return {"feed_id": feed_id,
                "created_at": _iso(entry["created_at"]),
                "updated_at": _iso(entry["updated_at"]),
                "expires_at": _iso(entry["expires_at"]),
                "owned": owner_hash is not None,
                "owner_fingerprint": owner_hash[:12]
                if owner_hash is not None else None,
                "timelines": self._names(entry["payload"]),
                "version": entry["version"],
                "etag": multi_feed_etag(entry["payload"],
                                         entry["version"]),
                "changes": len(entry["changes"]),
                "webhooks": len(entry.get("webhooks", [])),
                "deliveries": len(entry.get("deliveries", []))}

    def _new_id(self) -> str:
        for _ in range(100):
            feed_id = secrets.token_hex(16)
            if feed_id not in self._entries:
                return feed_id
        raise ValueError("feed_id_exhausted")

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def info(self) -> dict:
        with self._lock:
            if self._prune_expired():
                self._save()
            return {"persistent": self.persistent,
                    "count": len(self._entries),
                    "max": self.max_feeds,
                    "ttl_seconds": self.ttl_seconds}

    def create(self, payload: dict, owner_token=None,
               all_passed=None) -> dict:
        with self._lock:
            self._prune_expired()
            feed_id = self._new_id()
            now = self._clock()
            entry = {"payload": copy.deepcopy(payload),
                     "created_at": now, "updated_at": now,
                     "expires_at": now + self.ttl_seconds,
                     "version": 1, "changes": [],
                     "webhooks": [], "deliveries": []}
            digest = owner_token_hash(owner_token)
            if digest is not None:
                entry["owner_hash"] = digest
            entry["changes"].append(
                self._change(entry, "created", all_passed))
            self._entries[feed_id] = entry
            while len(self._entries) > self.max_feeds:
                self._entries.popitem(last=False)
            self._save()
            return self._meta(feed_id, entry)

    def get(self, feed_id: str) -> dict | None:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return None
            return copy.deepcopy(entry["payload"])

    def meta(self, feed_id: str) -> dict | None:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            return None if entry is None else self._meta(feed_id, entry)

    def changes(self, feed_id: str) -> dict | None:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return None
            return {"feed_id": feed_id,
                    "version": entry["version"],
                    "etag": multi_feed_etag(entry["payload"],
                                             entry["version"]),
                    "updated_at": _iso(entry["updated_at"]),
                    "changes": copy.deepcopy(entry["changes"])}

    def etag(self, feed_id: str) -> str | None:
        with self._lock:
            entry = self._entries.get(feed_id)
            if entry is None or entry["expires_at"] <= self._clock():
                return None
            return multi_feed_etag(entry["payload"], entry["version"])

    def update_status(self, feed_id: str, payload: dict,
                      owner_token=None, all_passed=None):
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return "not_found", None
            digest = entry.get("owner_hash")
            if digest is not None and \
                    owner_token_hash(owner_token) != digest:
                return "forbidden", None
            now = self._clock()
            entry["payload"] = copy.deepcopy(payload)
            entry["updated_at"] = now
            entry["expires_at"] = now + self.ttl_seconds
            entry["version"] += 1
            entry["changes"].append(
                self._change(entry, "updated", all_passed))
            entry["changes"] = entry["changes"][-self.MAX_CHANGES:]
            self._entries.move_to_end(feed_id)
            self._save()
            return "updated", self._meta(feed_id, entry)

    def revoke_status(self, feed_id: str, owner_token=None) -> str:
        with self._lock:
            if self._prune_expired():
                self._save()
            entry = self._entries.get(feed_id)
            if entry is None:
                return "not_found"
            digest = entry.get("owner_hash")
            if digest is not None and \
                    owner_token_hash(owner_token) != digest:
                return "forbidden"
            del self._entries[feed_id]
            self._save()
            return "revoked"

    def revoke(self, feed_id: str, owner_token=None) -> bool:
        return self.revoke_status(feed_id, owner_token) == "revoked"

    # -- push webhooks (tastepilot.notify) ----------------------------
    # A webhook is a subscriber-supplied http(s) URL stored inside
    # the feed entry (it is the delivery address). Every listing
    # names it by host label only. Registration/management follows
    # the feed's ownership rule: an owned feed answers only its
    # owner's token; an unowned feed keeps the link-holder rule.

    def _webhook_gate(self, feed_id: str, owner_token):
        """(entry, status): the live entry when access is
        allowed, else None + not_found/forbidden. The caller
        already pruned expired entries via any store read, but
        every webhook method prunes first like the others."""
        if self._prune_expired():
            self._save()
        entry = self._entries.get(feed_id)
        if entry is None:
            return None, "not_found"
        digest = entry.get("owner_hash")
        if digest is not None and \
                owner_token_hash(owner_token) != digest:
            return None, "forbidden"
        return entry, "ok"

    @staticmethod
    def _webhook_meta(webhook: dict) -> dict:
        failures = webhook.get("consecutive_failures")
        paused = webhook.get("paused") is True
        reason = webhook.get("paused_reason") if paused else None
        paused_at = webhook.get("paused_at") if paused else None
        epoch = _epoch_of(paused_at)
        probe_eligible_at = _iso(epoch + PAUSE_COOLDOWN_SECONDS) \
            if paused and reason == "auto" and epoch is not None \
            else None
        return {"webhook_id": webhook.get("webhook_id"),
                "host": webhook.get("host"),
                "created_at": webhook.get("created_at"),
                "signed": bool(webhook.get("signing_secret")),
                "paused": paused,
                "consecutive_failures": failures
                if isinstance(failures, int)
                and not isinstance(failures, bool) and failures >= 0
                else 0,
                "paused_reason": reason,
                "paused_at": paused_at,
                "probe_eligible_at": probe_eligible_at,
                "url_rotated_at": webhook.get("url_rotated_at")
                if isinstance(webhook.get("url_rotated_at"), str)
                else None,
                "events": list(webhook.get("events"))
                if isinstance(webhook.get("events"), list)
                and webhook.get("events")
                else list(WEBHOOK_EVENTS),
                "events_updated_at":
                webhook.get("events_updated_at")
                if isinstance(webhook.get("events_updated_at"), str)
                else None}

    def add_webhook(self, feed_id: str, url, owner_token=None,
                    signing_secret="generate", secret_out=None,
                    events=None):
        """Register (or re-register) a webhook URL on a feed.
        Returns (status, meta): status is ok / not_found /
        forbidden / too_many_webhooks; re-registering the same
        URL is idempotent (same deterministic id, original
        created_at, original signing secret — a later secret
        never replaces the first). The URL itself is validated
        by the caller via notify.validate_webhook_url before
        this is called for a new URL, and re-validated here
        for safety.

        ``signing_secret``: ``"generate"`` (the default) mints
        a fresh secret for a NEW webhook and, when
        ``secret_out`` (a dict) is given, places it there —
        that dict is the secret's only exit; meta and listings
        report only ``signed``. A string is the subscriber's
        own secret (validated here, never reported back, the
        subscriber already knows it). None registers an
        unsigned webhook, by the subscriber's choice.

        ``events``: the subscriber's event filter (validated
        here via notify.validate_webhook_events; a categorised
        ValueError leaves the feed unchanged). None — the
        default — means every event. The filter is stored
        only for a NEW webhook: a re-registration never
        replaces the first registration's filter either (the
        same first-registration-wins rule as the secret); the
        in-place remedy is set_webhook_events."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            url = validate_webhook_url(url)
            events = validate_webhook_events(events)
            webhook_id = webhook_id_for(url)
            for webhook in entry.get("webhooks", []):
                if webhook.get("webhook_id") == webhook_id:
                    return "ok", self._webhook_meta(webhook)
            if len(entry.get("webhooks", [])) >= MAX_WEBHOOKS_PER_FEED:
                return "too_many_webhooks", None
            webhook = {"webhook_id": webhook_id, "url": url,
                       "host": webhook_host_label(url),
                       "created_at": _iso(self._clock()),
                       "paused": False, "consecutive_failures": 0,
                       "paused_reason": None, "paused_at": None,
                       "url_rotated_at": None,
                       "events": events,
                       "events_updated_at": None}
            generated = signing_secret == "generate"
            secret = generate_signing_secret() if generated \
                else signing_secret
            if secret is not None:
                webhook["signing_secret"] = \
                    validate_signing_secret(secret)
                if generated and secret_out is not None:
                    secret_out["signing_secret"] = \
                        webhook["signing_secret"]
            entry.setdefault("webhooks", []).append(webhook)
            self._save()
            return "ok", self._webhook_meta(webhook)

    def webhooks(self, feed_id: str, owner_token=None):
        """(status, [webhook meta]) — host labels only, never
        the stored URLs."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            return "ok", [self._webhook_meta(w)
                          for w in entry.get("webhooks", [])]

    def webhook_entries(self, feed_id: str) -> list:
        """The raw webhook entries (URLs included) for delivery.
        Internal only: handlers call this after the operation
        that needed the URLs has already passed its gate."""
        with self._lock:
            entry = self._entries.get(feed_id)
            if entry is None:
                return []
            return copy.deepcopy(entry.get("webhooks", []))

    def remove_webhook(self, feed_id: str, webhook_id: str,
                       owner_token=None) -> str:
        """ok status is "removed"; also not_found (feed),
        forbidden, webhook_not_found."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status
            webhooks = entry.get("webhooks", [])
            kept = [w for w in webhooks
                    if w.get("webhook_id") != webhook_id]
            if len(kept) == len(webhooks):
                return "webhook_not_found"
            entry["webhooks"] = kept
            self._save()
            return "removed"

    def rotate_webhook_secret(self, feed_id: str, webhook_id: str,
                              owner_token=None, signing_secret="generate",
                              secret_out=None):
        """(status, meta): replace one webhook's signing secret
        in place, under the feed's owner rule (via the webhook
        gate). Status is ok / not_found (feed) / forbidden /
        webhook_not_found. ``signing_secret`` follows the
        registration contract exactly: ``"generate"`` (the
        default) mints a fresh secret and, when ``secret_out``
        (a dict) is given, places it there — that dict is the
        secret's only exit; a string is the subscriber's own
        secret (validated here, never reported back); None
        un-signs the webhook. The old secret stops verifying
        the moment the new one is stored — there is no grace
        window, because a rotation usually means the old secret
        leaked. Rotation is a management act, not a delivery:
        the webhook's pause state, failure count, and
        created_at are untouched, and a paused webhook can be
        rotated (it stays paused). Meta and listings report
        only the ``signed`` flag, as everywhere else."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            webhook = next(
                (w for w in entry.get("webhooks", [])
                 if w.get("webhook_id") == webhook_id), None)
            if webhook is None:
                return "webhook_not_found", None
            generated = signing_secret == "generate"
            secret = generate_signing_secret() if generated \
                else signing_secret
            if secret is None:
                webhook.pop("signing_secret", None)
            else:
                webhook["signing_secret"] = \
                    validate_signing_secret(secret)
                if generated and secret_out is not None:
                    secret_out["signing_secret"] = \
                        webhook["signing_secret"]
            self._save()
            return "ok", self._webhook_meta(webhook)

    def rotate_webhook_url(self, feed_id: str, webhook_id: str,
                           new_url, owner_token=None):
        """(status, meta): point one webhook at a new endpoint
        URL in place, under the feed's owner rule (via the
        webhook gate). Status is ok / not_found (feed) /
        forbidden / webhook_not_found / webhook_exists (the
        new URL is already registered on this feed under a
        different webhook — two registrations for one endpoint
        would double-deliver every push). ``new_url`` is
        validated exactly as at registration, here, before
        any assignment, so a categorised ValueError leaves the
        webhook byte-unchanged. Rotating to the webhook's
        current URL is idempotent: ok, and nothing is stamped.

        A real rotation updates the stored URL and host label,
        recomputes the webhook id from the new URL (the id is
        the URL's hash everywhere in this store — listings,
        delivery records, and redelivery all key on it), and
        stamps ``url_rotated_at``. Everything else about the
        webhook survives: the signing secret (a secret signs
        payloads, not addresses), the pause state and its
        provenance, the consecutive-failure count, and
        created_at — the registration moment is when the
        subscription began, not when its endpoint last moved.
        A paused webhook can be rotated (it stays paused).
        Rotation is a management act, not a delivery: nothing
        is sent to either endpoint. Delivery records are never
        edited — records from before the rotation keep the old
        webhook id and host label as the audit trail, which
        also means a failed pre-rotation record can no longer
        be redelivered (its webhook id names no current
        registration); the missed change itself is superseded
        by the next update push to the new endpoint."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            webhook = next(
                (w for w in entry.get("webhooks", [])
                 if w.get("webhook_id") == webhook_id), None)
            if webhook is None:
                return "webhook_not_found", None
            url = validate_webhook_url(new_url)
            if url == webhook.get("url"):
                return "ok", self._webhook_meta(webhook)
            new_id = webhook_id_for(url)
            for other in entry.get("webhooks", []):
                if other is not webhook \
                        and other.get("webhook_id") == new_id:
                    return "webhook_exists", None
            webhook["url"] = url
            webhook["host"] = webhook_host_label(url)
            webhook["webhook_id"] = new_id
            webhook["url_rotated_at"] = _iso(self._clock())
            self._save()
            return "ok", self._webhook_meta(webhook)

    def set_webhook_events(self, feed_id: str, webhook_id: str,
                           events, owner_token=None):
        """(status, meta): replace one webhook's event filter
        in place, under the feed's owner rule (via the webhook
        gate). Status is ok / not_found (feed) / forbidden /
        webhook_not_found. ``events`` is validated exactly as
        at registration, here, before any assignment, so a
        categorised ValueError leaves the webhook
        byte-unchanged; an explicit None resets the webhook
        to every event (the no-filter default). Setting the
        filter the webhook already effectively has — the same
        set, or None on an unfiltered webhook — is idempotent:
        ok, and nothing is stamped. A real change stores the
        canonicalised filter and stamps ``events_updated_at``.

        Everything else about the webhook survives: the id
        (a filter is not an address), the URL, the signing
        secret, the pause state and its provenance, the
        consecutive-failure count, and created_at — changing
        what a subscription carries is a management act, not
        a new subscription. A paused webhook's filter can be
        set (it stays paused). Setting delivers nothing, and
        delivery records are never edited: a record from
        before the change keeps the event it attempted."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            webhook = next(
                (w for w in entry.get("webhooks", [])
                 if w.get("webhook_id") == webhook_id), None)
            if webhook is None:
                return "webhook_not_found", None
            events = validate_webhook_events(events)
            current = webhook.get("events")
            current = current \
                if isinstance(current, list) and current else None
            if events == current:
                return "ok", self._webhook_meta(webhook)
            webhook["events"] = events
            webhook["events_updated_at"] = _iso(self._clock())
            self._save()
            return "ok", self._webhook_meta(webhook)

    def _note_delivery_outcomes(self, entry: dict, records) -> None:
        """Fold recorded delivery outcomes into each webhook's
        delivery health (called with the store lock held, from
        record_deliveries — the one path every push takes, so
        registration pings, update pushes, and redeliveries all
        count the same way). A delivered record resets its
        webhook's consecutive-failure count — and revives a
        paused webhook outright, because a delivery that just
        landed is the endpoint proving itself alive (this is
        how a successful half-open probe closes the breaker);
        a failed record increments it, and at
        notify.PAUSE_AFTER_CONSECUTIVE_FAILURES the webhook is
        auto-paused, stamped with the reason and the moment —
        later pushes to it are recorded as skipped instead of
        attempted, until the subscriber resumes it or a probe
        succeeds. A failed record recorded while a webhook is
        already auto-paused can only be a probe: the failure
        count grows and the cooldown restarts from that
        moment. A skipped record is not an attempt and never
        counts. Records naming no registered webhook change
        nothing."""
        webhooks = {w.get("webhook_id"): w
                    for w in entry.get("webhooks", [])
                    if isinstance(w, dict)}
        for record in records or []:
            if not isinstance(record, dict):
                continue
            webhook = webhooks.get(record.get("webhook_id"))
            if webhook is None:
                continue
            status = record.get("status")
            if status == "delivered":
                webhook["consecutive_failures"] = 0
                if webhook.get("paused") is True:
                    webhook["paused"] = False
                    webhook["paused_reason"] = None
                    webhook["paused_at"] = None
            elif status == "failed":
                was_paused = webhook.get("paused") is True
                failures = webhook.get("consecutive_failures")
                failures = failures + 1 \
                    if isinstance(failures, int) \
                    and not isinstance(failures, bool) \
                    and failures >= 0 else 1
                webhook["consecutive_failures"] = failures
                if was_paused and \
                        webhook.get("paused_reason") == "auto":
                    webhook["paused_at"] = _iso(self._clock())
                elif not was_paused and \
                        failures >= PAUSE_AFTER_CONSECUTIVE_FAILURES:
                    webhook["paused"] = True
                    webhook["paused_reason"] = "auto"
                    webhook["paused_at"] = _iso(self._clock())

    def set_webhook_paused(self, feed_id: str, webhook_id: str,
                           paused: bool, owner_token=None):
        """(status, meta): pause or resume one webhook on a
        feed, under the feed's owner rule (via the webhook
        gate). Status is ok / not_found (feed) / forbidden /
        webhook_not_found. Resuming also clears the
        consecutive-failure count — the subscriber's
        deliberate act starts the endpoint fresh; pausing by
        hand leaves the count as the record of why, stamps
        the pause as ``manual`` (converting even an auto
        pause — a deliberate instruction replaces the
        symptom), and is therefore never probe-eligible."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            webhook = next(
                (w for w in entry.get("webhooks", [])
                 if w.get("webhook_id") == webhook_id), None)
            if webhook is None:
                return "webhook_not_found", None
            webhook["paused"] = bool(paused)
            if paused:
                webhook["paused_reason"] = "manual"
                webhook["paused_at"] = _iso(self._clock())
            else:
                webhook["consecutive_failures"] = 0
                webhook["paused_reason"] = None
                webhook["paused_at"] = None
            self._save()
            return "ok", self._webhook_meta(webhook)

    def probe_due_ids(self, feed_id: str) -> list:
        """The feed's webhooks whose auto-pause cooldown has
        elapsed at the store clock: paused with reason
        ``auto`` and a known pause moment at least
        notify.PAUSE_COOLDOWN_SECONDS ago. These — and only
        these — are attempted as half-open probes by the next
        push; a manual pause (or a legacy paused row with no
        stamped reason/moment) is never due. Unknown feed →
        empty list."""
        with self._lock:
            entry = self._entries.get(feed_id)
            if entry is None:
                return []
            now = self._clock()
            due = []
            for webhook in entry.get("webhooks", []):
                if not isinstance(webhook, dict) \
                        or webhook.get("paused") is not True \
                        or webhook.get("paused_reason") != "auto":
                    continue
                epoch = _epoch_of(webhook.get("paused_at"))
                if epoch is not None and \
                        now >= epoch + PAUSE_COOLDOWN_SECONDS:
                    due.append(webhook.get("webhook_id"))
            return due

    def record_deliveries(self, feed_id: str, records,
                          payload=None, redelivery_of=None) -> list:
        """Append delivery records (stamped with the store
        clock), capped at notify.MAX_DELIVERIES per feed, and
        return the stamped copies. Each record is also stamped
        with a ``delivery_id`` (notify.delivery_id_for) and —
        when the caller supplies the attempted ``payload``, as
        every push path does — a deep copy of that payload plus
        a ``redelivery_of`` pointer (None for a first attempt),
        which is what makes a later on-demand redelivery of a
        failed record possible without re-deriving anything.
        A feed that vanished between the operation and the
        recording simply keeps nothing."""
        with self._lock:
            entry = self._entries.get(feed_id)
            if entry is None:
                return []
            stamped = []
            base = len(entry.get("deliveries", []))
            for seq, record in enumerate(records or []):
                item = copy.deepcopy(record)
                item["at"] = _iso(self._clock())
                item["feed_id"] = feed_id
                item["delivery_id"] = delivery_id_for(
                    feed_id, item.get("webhook_id"),
                    item.get("event"), item.get("version"),
                    item["at"], base + seq)
                item["payload"] = copy.deepcopy(payload) \
                    if payload is not None else None
                item["redelivery_of"] = redelivery_of
                stamped.append(item)
            entry.setdefault("deliveries", []).extend(stamped)
            entry["deliveries"] = \
                entry["deliveries"][-MAX_DELIVERIES:]
            self._note_delivery_outcomes(entry, stamped)
            self._save()
            return stamped

    def redelivery_target(self, feed_id: str, delivery_id: str,
                          owner_token=None):
        """(status, target): what an on-demand redelivery of one
        recorded delivery would resend, and where. Status is
        ok / not_found (feed) / forbidden / delivery_not_found /
        already_delivered / not_redeliverable (a record stamped
        before payloads were kept, or without one) /
        webhook_not_found (the webhook was removed after the
        original attempt — there is no current address to send
        to) / webhook_paused (the webhook is paused after
        consecutive failures — resume it first, then resend).
        On ok, target is ``{record, payload, webhook}``:
        deep copies of the original record, the payload it
        attempted, and the webhook's CURRENT registered entry
        (a re-registered endpoint receives the resend)."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            record = next(
                (d for d in entry.get("deliveries", [])
                 if isinstance(d, dict)
                 and d.get("delivery_id") == delivery_id), None)
            if record is None:
                return "delivery_not_found", None
            if record.get("status") == "delivered":
                return "already_delivered", None
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return "not_redeliverable", None
            webhook = next(
                (w for w in entry.get("webhooks", [])
                 if w.get("webhook_id") == record.get("webhook_id")),
                None)
            if webhook is None:
                return "webhook_not_found", None
            if webhook.get("paused") is True:
                return "webhook_paused", None
            return "ok", {"record": copy.deepcopy(record),
                          "payload": copy.deepcopy(payload),
                          "webhook": copy.deepcopy(webhook)}

    def failed_redelivery_targets(self, feed_id: str,
                                  owner_token=None):
        """(status, result): every still-actionable failed
        delivery on a feed, for a bulk redelivery. Status is
        ok / not_found (feed) / forbidden — the feed's owner
        rule via the webhook gate, exactly as for a single
        redelivery. On ok, result is ``{targets, skipped}``:
        targets are the latest attempt of each failed delivery
        chain (``{record, payload, webhook}`` deep copies, in
        log order — the same triple ``redelivery_target``
        returns for one record), and skipped names every other
        failed record with why it was left alone:
        ``superseded`` (a later record already redelivers it —
        resending it again would duplicate a push its chain
        has moved past), ``not_redeliverable`` (a legacy
        record stamped without a payload),
        ``webhook_not_found`` (the webhook was removed; there
        is no current address), or ``webhook_paused`` (the
        webhook is paused — resume it, then resend).
        Delivered records are neither
        targets nor skipped — they are done."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            records = [d for d in entry.get("deliveries", [])
                       if isinstance(d, dict)]
            superseded_by = {}
            for record in records:
                origin = record.get("redelivery_of")
                if isinstance(origin, str) and origin:
                    superseded_by[origin] = record.get("delivery_id")
            targets, skipped = [], []
            for record in records:
                if record.get("status") == "delivered":
                    continue
                delivery_id = record.get("delivery_id")
                if delivery_id in superseded_by:
                    skipped.append({
                        "delivery_id": delivery_id,
                        "reason": "superseded",
                        "superseded_by": superseded_by[delivery_id]})
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    skipped.append({
                        "delivery_id": delivery_id,
                        "reason": "not_redeliverable"})
                    continue
                webhook = next(
                    (w for w in entry.get("webhooks", [])
                     if w.get("webhook_id")
                     == record.get("webhook_id")), None)
                if webhook is None:
                    skipped.append({
                        "delivery_id": delivery_id,
                        "reason": "webhook_not_found"})
                    continue
                if webhook.get("paused") is True:
                    skipped.append({
                        "delivery_id": delivery_id,
                        "reason": "webhook_paused"})
                    continue
                targets.append({
                    "record": copy.deepcopy(record),
                    "payload": copy.deepcopy(payload),
                    "webhook": copy.deepcopy(webhook)})
            return "ok", {"targets": targets, "skipped": skipped}

    def deliveries(self, feed_id: str, owner_token=None):
        """(status, [delivery record]) — the feed's capped push
        delivery log, oldest first."""
        with self._lock:
            entry, status = self._webhook_gate(feed_id, owner_token)
            if entry is None:
                return status, None
            return "ok", copy.deepcopy(entry.get("deliveries", []))

def make_handler(agent, max_evidence_records: int = MAX_EVIDENCE_RECORDS,
                 max_shared_plans: int = MAX_SHARED_PLANS,
                 share_ttl_seconds: float = DEFAULT_SHARE_TTL_SECONDS,
                 share_file: str | Path | None = None,
                 share_clock=None,
                 max_profiles: int = MAX_PROFILES,
                 profile_file: str | Path | None = None,
                 profile_clock=None,
                 max_feeds: int = MAX_TIMELINE_FEEDS,
                 feed_ttl_seconds: float = DEFAULT_FEED_TTL_SECONDS,
                 feed_file: str | Path | None = None,
                 feed_clock=None,
                 max_multi_feeds: int = MAX_MULTI_TIMELINE_FEEDS,
                 multi_feed_ttl_seconds: float = DEFAULT_FEED_TTL_SECONDS,
                 multi_feed_file: str | Path | None = None,
                 multi_feed_clock=None,
                 webhook_transport=None, webhook_sleeper=None):
    # response_id -> (record, response) for plans produced by this server,
    # bounded FIFO: the oldest sealed responses are evicted first.
    evidence_cache: OrderedDict[str, tuple[dict, dict]] = OrderedDict()
    # Share links live in a ShareStore: bounded FIFO like the evidence
    # cache, plus per-link expiry, revocation, and — when share_file is
    # given — file persistence across restarts. An evicted, expired,
    # or revoked id simply 404s like an unknown one.
    share_kwargs = {"max_plans": max_shared_plans,
                    "ttl_seconds": share_ttl_seconds, "path": share_file}
    if share_clock is not None:
        share_kwargs["clock"] = share_clock
    share_store = ShareStore(**share_kwargs)
    # Persistent preference profiles live in a ProfileStore: keyed by
    # owner-token hash, writable only from sealed swap histories, and
    # — when profile_file is given — file-persistent across restarts
    # exactly like the share store.
    profile_kwargs = {"max_profiles": max_profiles, "path": profile_file}
    if profile_clock is not None:
        profile_kwargs["clock"] = profile_clock
    profile_store = ProfileStore(**profile_kwargs)
    # Live calendar feeds live in a TimelineFeedStore: bounded,
    # expiring, revocable, optionally owner-managed, and — when
    # feed_file is given — file-persistent across restarts exactly
    # like the share store.
    feed_kwargs = {"max_feeds": max_feeds,
                   "ttl_seconds": feed_ttl_seconds, "path": feed_file}
    if feed_clock is not None:
        feed_kwargs["clock"] = feed_clock
    feed_store = TimelineFeedStore(**feed_kwargs)
    # Multi-timeline feeds live in a MultiTimelineFeedStore: the
    # same contract, carrying a whole bundle of timelines under
    # one id, versioned with a change log + etag for poll-based
    # change notification.
    multi_feed_kwargs = {"max_feeds": max_multi_feeds,
                         "ttl_seconds": multi_feed_ttl_seconds,
                         "path": multi_feed_file}
    if multi_feed_clock is not None:
        multi_feed_kwargs["clock"] = multi_feed_clock
    multi_feed_store = MultiTimelineFeedStore(**multi_feed_kwargs)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep demo logs clean
            pass

        def _send(self, code, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            if ctype.startswith("application/json"):
                self.send_header("Cache-Control", "no-store")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _owner_token(self):
            """The presented owner token: (token, invalid). ``token``
            is None when no header was sent; ``invalid`` is True when
            a header was sent but is not a usable credential — the
            caller decides whether that is a 400 (share creation,
            revocation) or simply owns nothing."""
            raw = self.headers.get(OWNER_TOKEN_HEADER)
            if raw is None:
                return None, False
            token = raw.strip()
            if owner_token_hash(token) is None:
                return None, True
            return token, False

        def _handle_shares(self):
            """The owner's dashboard list: only the presenting
            token's shares, each with its recomputed checker verdict
            — listing grants nothing, it just names what the token
            already owns. No token → 401; a malformed one → 400."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            if token is None:
                self._send_json(401, {"error": "owner token required"})
                return
            shares = []
            for meta in share_store.list_for_owner(token):
                entry = dict(meta)
                entry["url"] = f"/?share={meta['share_id']}"
                entry["verify_url"] = \
                    f"/api/share/{meta['share_id']}/verify"
                plan = share_store.get(meta["share_id"])
                try:
                    report = verification_report(plan) \
                        if plan is not None else None
                except (KeyError, TypeError, ValueError, AttributeError,
                        IndexError):
                    report = None
                entry["passed"] = report["passed"] \
                    if report is not None else None
                entry["chain_valid"] = report["chain_valid"] \
                    if report is not None else None
                shares.append(entry)
            self._send_json(200, {"shares": shares,
                                  "count": len(shares),
                                  "owner_fingerprint":
                                      owner_fingerprint(token)})

        def _profile_token(self):
            """Owner-token gate for the profile routes, mirroring
            the shares dashboard: no token → 401, malformed → 400.
            Returns the token, or None after sending the error."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return None
            if token is None:
                self._send_json(401, {"error": "owner token required"})
                return None
            return token

        def _handle_profile_get(self):
            """The caller's persistent preference profile: the
            stored weights (derived only from sealed swap
            histories), or exists=false with a zero profile when
            this token has never saved one."""
            from .swap import empty_profile
            token = self._profile_token()
            if token is None:
                return
            profile = profile_store.get(token)
            meta = profile_store.meta(token)
            self._send_json(200, {
                "exists": profile is not None,
                "profile": profile if profile is not None
                else empty_profile(),
                "owner_fingerprint": meta["owner_fingerprint"],
                "updated_at": meta["updated_at"]})

        def _handle_profile_update(self):
            """Merge a lesson into the caller's stored profile.

            Body: {"history": [{...plan...}, ...]} — each plan is
            shape-gated here, then profiles derives the lesson
            from the plans' sealed swap histories alone (a
            chain-broken plan is ignored and counted, never
            believed) and merges it additively under the caps.
            Caller-supplied weights are never accepted: there is
            no field for them."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            token = self._profile_token()
            if token is None:
                return
            if not isinstance(payload, dict) \
                    or not isinstance(payload.get("history"), list) \
                    or not payload["history"] \
                    or any(not isinstance(p, dict)
                           for p in payload["history"]):
                self._send_json(
                    400, {"error": "profile failed: bad_history"})
                return
            for past in payload["history"]:
                if validate_plan_payload(past) is not None:
                    self._send_json(
                        400, {"error": "profile failed: bad_history"})
                    return
            try:
                result = profile_store.update_from_plans(
                    token, payload["history"])
            except ValueError as exc:  # categorised refusal
                self._send_json(
                    400, {"error": f"profile failed: {exc}"})
                return
            self._send_json(200, {
                "profile": result["profile"],
                "derived": result["derived"],
                "owner_fingerprint": result["meta"]["owner_fingerprint"],
                "updated_at": result["meta"]["updated_at"]})

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif path == "/api/health":
                self._send_json(200, {"ok": True, "mode": agent.mode,
                                      "explainer": agent.explainer_info,
                                      "shares": share_store.info(),
                                      "profiles": profile_store.info(),
                                      "feeds": feed_store.info(),
                                      "multi_feeds":
                                          multi_feed_store.info()})
            elif path == "/api/profile":
                self._handle_profile_get()
            elif path == "/api/shares":
                self._handle_shares()
            elif path.startswith("/api/share/"):
                rest = unquote(path[len("/api/share/"):])
                want_verify = rest.endswith("/verify")
                share_id = rest[:-len("/verify")] if want_verify else rest
                plan = None if "/" in share_id else share_store.get(share_id)
                if plan is None:
                    self._send_json(404, {"error": "unknown share id"})
                    return
                if not want_verify:
                    self._send_json(200, plan)
                    return
                try:
                    report = verification_report(plan)
                except (KeyError, TypeError, ValueError, AttributeError,
                        IndexError):
                    self._send_json(400, {"error": "malformed plan or "
                                                   "evidence record"})
                    return
                report["share_id"] = share_id
                meta = share_store.meta(share_id)
                if meta is not None:
                    report["expires_at"] = meta["expires_at"]
                self._send_json(200, report)
            elif path.startswith("/api/multi-feeds/"):
                rest = unquote(path[len("/api/multi-feeds/"):])
                if rest.endswith(".ics"):
                    self._handle_multi_feed_fetch(
                        rest[:-len(".ics")])
                elif rest.endswith("/changes") and \
                        "/" not in rest[:-len("/changes")]:
                    self._handle_multi_feed_changes(
                        rest[:-len("/changes")])
                elif rest.endswith("/webhooks") and \
                        "/" not in rest[:-len("/webhooks")]:
                    self._handle_webhooks_list(
                        rest[:-len("/webhooks")])
                elif rest.endswith("/notifications") and \
                        "/" not in rest[:-len("/notifications")]:
                    self._handle_notifications(
                        rest[:-len("/notifications")])
                elif rest and "/" not in rest:
                    self._handle_multi_feed_meta(rest)
                else:
                    self._send_json(
                        404, {"error": "unknown multi-feed id"})
            elif path.startswith("/api/feeds/"):
                rest = unquote(path[len("/api/feeds/"):])
                if rest.endswith(".ics"):
                    self._handle_feed_fetch(rest[:-len(".ics")])
                elif rest and "/" not in rest:
                    self._handle_feed_meta(rest)
                else:
                    self._send_json(404, {"error": "unknown feed id"})
            elif path.startswith("/api/evidence/"):
                response_id = unquote(path[len("/api/evidence/"):])
                hit = evidence_cache.get(response_id)
                if hit is None:
                    self._send_json(404, {"error": "unknown evidence id"})
                    return
                record, response = hit
                self._send_json(200, {
                    "response_id": response_id,
                    "endpoint": response.get("endpoint"),
                    "source": response.get("source"),
                    "request": response.get("request"),
                    "results": response.get("results"),
                    "record_hash": record.get("record_hash"),
                    "record_verdict": record.get("verdict"),
                    "chain_valid": verify_chain(record),
                })
            else:
                self._send(404, b"not found", "text/plain")

        def _handle_verify(self):
            """Re-verify a submitted plan/record from its sealed evidence.

            The submitted record is untrusted input: it is size-capped,
            shape-validated, and every verdict in the response is
            recomputed deterministically (hash chain + checker). A
            malformed record is a 400, never a 500 or a traceback.
            """
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            error = validate_plan_payload(payload)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            try:
                report = verification_report(payload)
            except (KeyError, TypeError, ValueError, AttributeError,
                    IndexError):
                self._send_json(400,
                                {"error": "malformed plan or evidence record"})
                return
            # Informational only: record_verdict is what the record
            # claims about itself. The verdicts above are recomputed,
            # never read from that field.
            self._send_json(200, report)

        def _handle_export(self, fmt: str):
            """Render a submitted plan as a take-away itinerary.

            Same untrusted-input gates as /api/verify (size cap +
            shape validation). The rendered banner carries the
            recomputed verdict — a tampered plan exports as NOT
            VERIFIED, never clean — and the response is a download
            (Content-Disposition) with no-store, like every API body.
            """
            try:
                ctype, ext = media_for(fmt)
            except ValueError:
                self._send_json(400, {"error": "unsupported export format"})
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            error = validate_plan_payload(payload)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            try:
                content, _verified = export_plan(payload, fmt)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send(200, content.encode("utf-8"), ctype, extra={
                "Cache-Control": "no-store",
                "Content-Disposition":
                    f'attachment; filename="tastepilot-itinerary.{ext}"'})

        def _handle_swap(self):
            """Exchange a primary item with a verified alternative.

            Body: {"plan": {...}, "primary": <selector>,
            "alternative": <selector>} where a selector is a name or
            {name/entity_id/domain/location/evidence_ref}. The plan
            is untrusted input held to the verify endpoint's gates;
            swap.swap_plan recomputes the chain + checker verdicts,
            refuses anything unverified, and re-seals the evidence
            record around the swap — the response is the new plan.
            Refusals are categorised 400s, never a 500 or traceback.
            """
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("plan"), dict):
                self._send_json(400, {"error": "plan is required"})
                return
            if "primary" not in payload or "alternative" not in payload:
                self._send_json(
                    400, {"error": "primary and alternative are required"})
                return
            plan = payload["plan"]
            error = validate_plan_payload(plan)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            try:
                result = swap_plan(plan, payload["primary"],
                                   payload["alternative"])
            except ValueError as exc:  # categorised refusal
                self._send_json(400, {"error": f"swap failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(400, {"error": "swap failed: bad_plan"})
                return
            record = result.get("evidence") or {}
            for rid, resp in qloo_responses(record).items():
                evidence_cache[rid] = (record, resp)
                evidence_cache.move_to_end(rid)
                while len(evidence_cache) > max_evidence_records:
                    evidence_cache.popitem(last=False)
            self._send_json(200, result)

        def _handle_undo(self):
            """Reverse a plan's most recent sealed swap.

            Body: {"plan": {...}}. The plan is untrusted input held
            to exactly the swap endpoint's gates; swap.undo_last_swap
            derives the reversal from the sealed history (never the
            stored swaps mirror), re-checks every precondition, and
            re-seals with the undo as a further swap_event — the
            response is the new plan. Refusals (including
            no_swaps_to_undo) are categorised 400s."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("plan"), dict):
                self._send_json(400, {"error": "plan is required"})
                return
            plan = payload["plan"]
            error = validate_plan_payload(plan)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            try:
                result = undo_last_swap(plan)
            except ValueError as exc:  # categorised refusal
                self._send_json(400, {"error": f"undo failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(400, {"error": "undo failed: bad_plan"})
                return
            record = result.get("evidence") or {}
            for rid, resp in qloo_responses(record).items():
                evidence_cache[rid] = (record, resp)
                evidence_cache.move_to_end(rid)
                while len(evidence_cache) > max_evidence_records:
                    evidence_cache.popitem(last=False)
            self._send_json(200, result)

        def _handle_branches(self):
            """Explore named swap branches from one base plan.

            Body: {"plan": {...}, "branches": [{name, swaps}]}.
            The plan is untrusted input held to the swap endpoint's
            gates; swap.explore_branches forks each branch
            independently (every swap re-checked and re-sealed) and
            returns the comparison with each successful branch's own
            plan. A malformed spec is a categorised 400; a branch
            whose path does not apply is a per-branch result in the
            200 response, never a failed request."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("plan"), dict):
                self._send_json(400, {"error": "plan is required"})
                return
            if "branches" not in payload:
                self._send_json(400, {"error": "branches are required"})
                return
            plan = payload["plan"]
            error = validate_plan_payload(plan)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            try:
                result = explore_branches(plan, payload["branches"])
            except ValueError as exc:  # spec-shape refusal
                self._send_json(
                    400, {"error": f"branches failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "branches failed: bad_plan"})
                return
            for branch in result["branches"]:
                if not branch.get("ok"):
                    continue
                record = branch["plan"].get("evidence") or {}
                for rid, resp in qloo_responses(record).items():
                    evidence_cache[rid] = (record, resp)
                    evidence_cache.move_to_end(rid)
                    while len(evidence_cache) > max_evidence_records:
                        evidence_cache.popitem(last=False)
            self._send_json(200, result)

        def _handle_diff(self):
            """Compare two plans; both verdicts recomputed.

            Body: {"base": {...}, "other": {...}}. Both plans are
            untrusted input held to exactly the verify endpoint's
            gates; diff.diff_plans recomputes each side's chain +
            checker verdict from its own sealed record (neither
            plan's stored checker field is read) and reports the
            added/removed/kept items, affinity changes, alternatives
            and schedule changes between them. Malformed input is a
            categorised 400, never a 500 or traceback."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("base"), dict) or \
                    not isinstance(payload.get("other"), dict):
                self._send_json(
                    400, {"error": "base and other plans are required"})
                return
            for plan in (payload["base"], payload["other"]):
                error = validate_plan_payload(plan)
                if error is not None:
                    self._send_json(400, {"error": error})
                    return
            try:
                result = diff_plans(payload["base"], payload["other"])
            except ValueError as exc:  # categorised refusal
                self._send_json(400, {"error": f"diff failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(400, {"error": "diff failed: bad_plan"})
                return
            self._send_json(200, result)

        def _handle_timeline(self):
            """Visual branch timeline: branches over the base's legs.

            Body: {"base": {...}, "branches": [{"name": ...,
            "plan": {...}}, ...]}. All plans are untrusted input
            held to exactly the verify endpoint's gates; timeline.
            branch_timeline recomputes every column's verdict from
            its own sealed record. A tampered branch is a 200 with
            that column passed=false — a verdict, not an error.
            Malformed input is a categorised 400, never a 500."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("base"), dict) or \
                    not isinstance(payload.get("branches"), list):
                self._send_json(
                    400, {"error": "base plan and branches are required"})
                return
            error = validate_plan_payload(payload["base"])
            if error is not None:
                self._send_json(400, {"error": error})
                return
            for branch in payload["branches"]:
                if isinstance(branch, dict) and \
                        isinstance(branch.get("plan"), dict):
                    error = validate_plan_payload(branch["plan"])
                    if error is not None:
                        self._send_json(400, {"error": error})
                        return
            try:
                result = branch_timeline(payload["base"],
                                         payload["branches"])
            except ValueError as exc:  # categorised refusal
                self._send_json(
                    400, {"error": f"timeline failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "timeline failed: bad_plan"})
                return
            self._send_json(200, result)

        def _handle_export_timeline(self, fmt: str):
            """Branch timeline as a take-away export (download).

            Body: {"base": {...}, "branches": [{"name": ...,
            "plan": {...}}, ...]} — exactly the /api/timeline
            gates. The rendered banner and every column verdict
            are recomputed by export.export_timeline; a tampered
            branch exports as a FAIL column under a NOT VERIFIED
            banner (markdown/text) or a NOT VERIFIED event series
            (ics) — a 200 download, like a tampered single-plan
            export — never clean. ics on a timeline with no dated
            leg in any column is a categorised 400 (no_dated_legs),
            exactly as a dateless single-plan ICS export."""
            if fmt not in TIMELINE_FORMATS:
                self._send_json(
                    400, {"error": "unsupported timeline export format"})
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("base"), dict) or \
                    not isinstance(payload.get("branches"), list):
                self._send_json(
                    400, {"error": "base plan and branches are required"})
                return
            error = validate_plan_payload(payload["base"])
            if error is not None:
                self._send_json(400, {"error": error})
                return
            for branch in payload["branches"]:
                if isinstance(branch, dict) and \
                        isinstance(branch.get("plan"), dict):
                    error = validate_plan_payload(branch["plan"])
                    if error is not None:
                        self._send_json(400, {"error": error})
                        return
            try:
                content, _all_passed = export_timeline(
                    payload["base"], payload["branches"], fmt)
            except ValueError as exc:  # categorised refusal
                self._send_json(
                    400, {"error": f"timeline export failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "timeline export failed: bad_plan"})
                return
            _ctype, ext = media_for(fmt)
            self._send(200, content.encode("utf-8"), _ctype, extra={
                "Cache-Control": "no-store",
                "Content-Disposition":
                    f'attachment; filename="tastepilot-timeline.{ext}"'})

        def _timeline_body(self):
            """Read and shape-gate a {"base", "branches"} body under
            exactly the /api/timeline gates. Returns the payload, or
            None after sending the categorised error."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return None
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return None
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return None
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return None
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("base"), dict) or \
                    not isinstance(payload.get("branches"), list):
                self._send_json(
                    400, {"error": "base plan and branches are required"})
                return None
            error = validate_plan_payload(payload["base"])
            if error is not None:
                self._send_json(400, {"error": error})
                return None
            for branch in payload["branches"]:
                if isinstance(branch, dict) and \
                        isinstance(branch.get("plan"), dict):
                    error = validate_plan_payload(branch["plan"])
                    if error is not None:
                        self._send_json(400, {"error": error})
                        return None
            return payload

        def _feed_gate(self, payload):
            """Prove a timeline can be a calendar feed: rendering it
            as iCalendar recomputes every column's verdict and
            refuses a dateless timeline (no_dated_legs). Returns
            all_passed, or None after sending a categorised 400 —
            a tampered branch is NOT refused: it passes the gate and
            its series will read NOT VERIFIED, exactly as in the
            static export."""
            try:
                _content, all_passed = export_timeline(
                    payload["base"], payload["branches"], "ics")
            except ValueError as exc:  # categorised refusal
                self._send_json(
                    400, {"error": f"feed failed: {exc}"})
                return None
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "feed failed: bad_plan"})
                return None
            return all_passed

        def _handle_feed_create(self):
            """Create a live calendar subscription feed for a branch
            timeline: POST /api/feeds with the /api/timeline body.
            The feed id is stable and unguessable; subscribers poll
            its .ics URL, and the owner can replace the timeline in
            place or revoke the feed outright."""
            payload = self._timeline_body()
            if payload is None:
                return
            all_passed = self._feed_gate(payload)
            if all_passed is None:
                return
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            meta = feed_store.create(payload, owner_token=token)
            feed_id = meta["feed_id"]
            self._send_json(201, {
                **meta,
                "url": f"/api/feeds/{feed_id}.ics",
                "feed_url": f"/api/feeds/{feed_id}.ics",
                "all_passed": all_passed,
            })

        def _handle_feed_update(self, feed_id):
            """Replace a feed's timeline in place (POST
            /api/feeds/<id>): the same URL now serves the new
            series, and the expiry is renewed by this deliberate
            act. An owned feed requires its owner's token."""
            payload = self._timeline_body()
            if payload is None:
                return
            all_passed = self._feed_gate(payload)
            if all_passed is None:
                return
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, meta = feed_store.update_status(
                feed_id, payload, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can update "
                                   "this feed"})
                return
            self._send_json(200, {
                **meta,
                "url": f"/api/feeds/{feed_id}.ics",
                "feed_url": f"/api/feeds/{feed_id}.ics",
                "all_passed": all_passed,
                "updated": True,
            })

        def _handle_feed_fetch(self, feed_id):
            """Serve a feed's calendar: GET /api/feeds/<id>.ics.
            The stored timeline is rendered fresh on every fetch —
            verdicts recomputed, so a re-sealed (or tampered) branch
            shows exactly what it verifies as — with a short public
            cache lifetime so calendar apps actually re-poll."""
            payload = feed_store.get(feed_id) if feed_id else None
            if payload is None:
                self._send_json(404, {"error": "unknown feed id"})
                return
            try:
                content, _all_passed = export_timeline(
                    payload["base"], payload["branches"], "ics")
            except ValueError as exc:  # stored feeds were gated at
                # write time; a refusal here is a categorised 400.
                self._send_json(
                    400, {"error": f"feed failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "feed failed: bad_plan"})
                return
            self._send(200, content.encode("utf-8"), "text/calendar",
                       extra={"Cache-Control": "public, max-age=300"})

        def _handle_feed_meta(self, feed_id):
            """Feed metadata + the recomputed timeline verdict."""
            payload = feed_store.get(feed_id) if feed_id else None
            meta = feed_store.meta(feed_id) if payload is not None \
                else None
            if payload is None or meta is None:
                self._send_json(404, {"error": "unknown feed id"})
                return
            try:
                doc = branch_timeline(payload["base"],
                                      payload["branches"])
                all_passed = bool(doc["summary"]["all_passed"])
            except (ValueError, KeyError, TypeError, AttributeError,
                    IndexError):
                all_passed = None
            self._send_json(200, {
                **meta,
                "url": f"/api/feeds/{feed_id}.ics",
                "feed_url": f"/api/feeds/{feed_id}.ics",
                "all_passed": all_passed,
            })

        def _multi_timeline_body(self):
            """Read and shape-gate a {"timelines": [...]} body.
            Returns the payload, or None after sending the
            categorised error. Each timeline is gated exactly as
            /api/timeline gates one (base + every branch plan via
            validate_plan_payload), plus the bundle gate in
            export.validate_multi_timelines (count, unique
            names). The body cap is the multi-feed cap — a bundle
            holds up to six timelines, each a full plan set."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return None
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return None
            if length > MAX_MULTI_FEED_BODY_BYTES:
                self._send_json(413, {"error": "bundle too large"})
                return None
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return None
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("timelines"), list):
                self._send_json(
                    400, {"error": "timelines are required"})
                return None
            try:
                validate_multi_timelines(payload["timelines"])
            except ValueError as exc:
                self._send_json(
                    400, {"error": f"multi-feed failed: {exc}"})
                return None
            for timeline in payload["timelines"]:
                error = validate_plan_payload(timeline["base"])
                if error is not None:
                    self._send_json(400, {"error": error})
                    return None
                for branch in timeline["branches"]:
                    if isinstance(branch, dict) and \
                            isinstance(branch.get("plan"), dict):
                        error = validate_plan_payload(branch["plan"])
                        if error is not None:
                            self._send_json(400, {"error": error})
                            return None
            return payload

        def _multi_feed_gate(self, payload):
            """Prove a bundle can be a calendar feed: rendering it
            as one iCalendar recomputes every timeline's verdicts
            and refuses a bundle with no dated leg anywhere
            (no_dated_legs). Returns (all_passed, summaries), or
            None after sending a categorised 400 — a tampered
            branch passes the gate and its series reads NOT
            VERIFIED, exactly as in the single-timeline feed."""
            try:
                _content, all_passed, summaries = \
                    export_multi_timeline_ics(payload["timelines"])
            except ValueError as exc:  # categorised refusal
                self._send_json(
                    400, {"error": f"multi-feed failed: {exc}"})
                return None
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "multi-feed failed: bad_plan"})
                return None
            return all_passed, summaries

        def _handle_multi_feed_create(self):
            """Create a multi-timeline subscription feed:
            POST /api/multi-feeds {"timelines": [...]}. One stable
            URL serves every timeline in the bundle as one
            calendar; version starts at 1 and the change log
            records the creation."""
            payload = self._multi_timeline_body()
            if payload is None:
                return
            gated = self._multi_feed_gate(payload)
            if gated is None:
                return
            all_passed, summaries = gated
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            meta = multi_feed_store.create(
                payload, owner_token=token, all_passed=all_passed)
            feed_id = meta["feed_id"]
            self._send_json(201, {
                **meta,
                "url": f"/api/multi-feeds/{feed_id}.ics",
                "feed_url": f"/api/multi-feeds/{feed_id}.ics",
                "changes_url": f"/api/multi-feeds/{feed_id}/changes",
                "notifications_url":
                    f"/api/multi-feeds/{feed_id}/notifications",
                "all_passed": all_passed,
                "timelines_summary": summaries,
            })

        def _handle_multi_feed_update(self, feed_id):
            """Replace a multi-feed's bundle in place (POST
            /api/multi-feeds/<id>): same URL, version incremented,
            a change-log entry appended, expiry renewed — how
            subscribers learn a timeline changed. An owned feed
            requires its owner's token."""
            payload = self._multi_timeline_body()
            if payload is None:
                return
            gated = self._multi_feed_gate(payload)
            if gated is None:
                return
            all_passed, summaries = gated
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, meta = multi_feed_store.update_status(
                feed_id, payload, owner_token=token,
                all_passed=all_passed)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can update "
                                   "this multi-feed"})
                return
            # Push the change to registered webhooks (recorded in
            # the feed's delivery log; a failed delivery never
            # fails this update).
            deliveries = self._notify_multi_feed(
                feed_id, "updated", all_passed)
            self._send_json(200, {
                **meta,
                "url": f"/api/multi-feeds/{feed_id}.ics",
                "feed_url": f"/api/multi-feeds/{feed_id}.ics",
                "changes_url": f"/api/multi-feeds/{feed_id}/changes",
                "notifications_url":
                    f"/api/multi-feeds/{feed_id}/notifications",
                "all_passed": all_passed,
                "timelines_summary": summaries,
                "updated": True,
                "notifications": deliveries,
            })

        def _handle_multi_feed_fetch(self, feed_id):
            """Serve a multi-feed's calendar: GET
            /api/multi-feeds/<id>.ics. The stored bundle renders
            fresh on every fetch (verdicts recomputed per
            timeline). Change notification: the response carries
            the feed's ETag; a poller presenting that etag in
            If-None-Match gets a cheap 304 until an update
            changes it — then the next poll gets the new series
            and the new etag."""
            payload = multi_feed_store.get(feed_id) if feed_id else None
            if payload is None:
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            etag = multi_feed_store.etag(feed_id)
            quoted = f'"{etag}"'
            presented = (self.headers.get("If-None-Match") or "")
            presented = {p.strip().strip('"')
                         for p in presented.split(",")}
            if etag is not None and etag in presented:
                self.send_response(304)
                self.send_header("ETag", quoted)
                self.send_header("Cache-Control",
                                 "public, max-age=300")
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                content, _all, _summaries = export_multi_timeline_ics(
                    payload["timelines"])
            except ValueError as exc:
                self._send_json(
                    400, {"error": f"multi-feed failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(
                    400, {"error": "multi-feed failed: bad_plan"})
                return
            self._send(200, content.encode("utf-8"), "text/calendar",
                       extra={"Cache-Control": "public, max-age=300",
                              "ETag": quoted})

        def _handle_multi_feed_meta(self, feed_id):
            """Multi-feed metadata + per-timeline recomputed
            verdicts."""
            payload = multi_feed_store.get(feed_id) if feed_id else None
            meta = multi_feed_store.meta(feed_id) \
                if payload is not None else None
            if payload is None or meta is None:
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            try:
                _content, all_passed, summaries = \
                    export_multi_timeline_ics(payload["timelines"])
            except (ValueError, KeyError, TypeError, AttributeError,
                    IndexError):
                all_passed, summaries = None, []
            self._send_json(200, {
                **meta,
                "url": f"/api/multi-feeds/{feed_id}.ics",
                "feed_url": f"/api/multi-feeds/{feed_id}.ics",
                "changes_url": f"/api/multi-feeds/{feed_id}/changes",
                "notifications_url":
                    f"/api/multi-feeds/{feed_id}/notifications",
                "all_passed": all_passed,
                "timelines_summary": summaries,
            })

        def _handle_multi_feed_changes(self, feed_id):
            """The feed's change log: GET
            /api/multi-feeds/<id>/changes — the notification
            surface a subscriber (or their calendar tooling) polls
            to learn what changed and when, without downloading
            the calendar: version, etag, and one entry per
            creation/update with its timeline names and its
            recomputed-at-write-time verdict."""
            result = multi_feed_store.changes(feed_id) \
                if feed_id else None
            if result is None:
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            result["url"] = f"/api/multi-feeds/{feed_id}.ics"
            self._send_json(200, result)

        def _notify_multi_feed(self, feed_id, kind, all_passed,
                               only_webhook_id=None,
                               bypass_filter=False):
            """Push one notification for a multi-feed change to
            its registered webhooks and record the deliveries.
            Best-effort by design: a failing endpoint is recorded
            as a failed delivery and never fails (or rolls back)
            the feed operation that triggered it. Returns the
            stamped delivery records ([] when no webhook matches
            — including when every webhook's event filter
            excludes this push's event; a filtered-out webhook
            gets no record at all).
            """
            entries = multi_feed_store.webhook_entries(feed_id)
            if only_webhook_id is not None:
                entries = [e for e in entries
                           if e.get("webhook_id") == only_webhook_id]
            if not entries:
                return []
            meta = multi_feed_store.meta(feed_id)
            if meta is None:
                return []
            payload = build_notification(
                feed_id, kind, meta["version"], meta["etag"],
                meta["timelines"], all_passed,
                _iso(time.time()))
            # Event filter: a webhook that did not subscribe
            # to this push's event is not a recipient at all —
            # no attempt, no delivery record, no failure-count
            # effect. Opting out is not failing, and it is not
            # being paused either (a paused webhook's wanted
            # pushes are recorded as skipped, above): there is
            # simply nothing the subscriber asked to hear.
            if not bypass_filter:
                entries = [e for e in entries
                           if webhook_subscribes_to(
                               e, payload.get("event"))]
            if not entries:
                return []
            kwargs = {}
            if webhook_transport is not None:
                kwargs["transport"] = webhook_transport
            if webhook_sleeper is not None:
                kwargs["sleeper"] = webhook_sleeper
            # A paused webhook (consecutive failures at the
            # threshold) is not attempted: its push is recorded
            # as a skipped delivery carrying the payload, so
            # the missed change stays redeliverable after a
            # resume. One exception — the half-open probe: an
            # auto-paused webhook whose cooldown has elapsed
            # IS attempted with this push, its record stamped
            # "probe"; a delivered probe revives it and a
            # failed one restarts its cooldown (both folded in
            # at record time). A manually paused webhook is
            # never probed. Records stay in registration order.
            due = set(multi_feed_store.probe_due_ids(feed_id))

            def _attempted(entry):
                return entry.get("paused") is not True \
                    or entry.get("webhook_id") in due

            active = [e for e in entries if _attempted(e)]
            delivered = iter(deliver_to_webhooks(
                active, payload, **kwargs))
            records = []
            for e in entries:
                if _attempted(e):
                    record = next(delivered)
                    if e.get("paused") is True:
                        record = {**record, "probe": True}
                    records.append(record)
                else:
                    records.append(skipped_record(e, payload))
            return multi_feed_store.record_deliveries(
                feed_id, records, payload=payload)

        def _handle_notification_redeliver(self, feed_id, delivery_id):
            """Resend one failed push delivery on demand: POST
            /api/multi-feeds/<id>/notifications/<delivery_id>/
            redeliver. The stored payload goes to the webhook's
            currently registered URL through the same bounded-
            retry delivery path as the original push; the resend
            is recorded as a NEW delivery whose ``redelivery_of``
            names the original, and the original record is never
            edited. A delivered record, a payload-less legacy
            record, and a removed webhook are categorised 400s;
            an unknown feed or delivery id is a 404; a non-owner
            on an owned feed is a 403."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, target = multi_feed_store.redelivery_target(
                feed_id, delivery_id, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can redeliver "
                                   "notifications for this multi-feed"})
                return
            if status == "delivery_not_found":
                self._send_json(404, {"error": "unknown delivery id"})
                return
            if status != "ok":
                self._send_json(
                    400, {"error": f"redelivery failed: {status}"})
                return
            kwargs = {}
            if webhook_transport is not None:
                kwargs["transport"] = webhook_transport
            if webhook_sleeper is not None:
                kwargs["sleeper"] = webhook_sleeper
            records = deliver_to_webhooks(
                [target["webhook"]], target["payload"], **kwargs)
            stamped = multi_feed_store.record_deliveries(
                feed_id, records, payload=target["payload"],
                redelivery_of=delivery_id)
            self._send_json(200, {
                "feed_id": feed_id, "redelivery_of": delivery_id,
                "delivery": stamped[0] if stamped else None})

        def _handle_notifications_redeliver_failed(self, feed_id):
            """Bulk-redeliver a feed's failed pushes in one
            call: POST /api/multi-feeds/<id>/notifications/
            redeliver-failed. The store selects the latest
            attempt of each failed delivery chain (a failed
            record a later resend already superseded is
            reported under ``skipped``, never resent — the
            single-record endpoint remains for deliberately
            resending one record), and each selected payload
            goes to its webhook's currently registered URL
            through the same bounded-retry delivery path,
            recorded as a new delivery whose ``redelivery_of``
            names the record it resends. The response reports
            attempted/delivered/failed counts, one result per
            resent record, and the skipped records with their
            reasons. An unknown feed is a 404 and a non-owner
            on an owned feed a 403, exactly as for a single
            redelivery; a feed with nothing actionable is a
            200 with zero attempts, not an error."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, result = multi_feed_store \
                .failed_redelivery_targets(feed_id, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can redeliver "
                                   "notifications for this multi-feed"})
                return
            kwargs = {}
            if webhook_transport is not None:
                kwargs["transport"] = webhook_transport
            if webhook_sleeper is not None:
                kwargs["sleeper"] = webhook_sleeper
            results = []
            for target in result["targets"]:
                origin = target["record"].get("delivery_id")
                records = deliver_to_webhooks(
                    [target["webhook"]], target["payload"], **kwargs)
                stamped = multi_feed_store.record_deliveries(
                    feed_id, records, payload=target["payload"],
                    redelivery_of=origin)
                results.append({
                    "delivery_id": origin,
                    "event": target["record"].get("event"),
                    "version": target["record"].get("version"),
                    "status": stamped[0].get("status")
                    if stamped else "failed",
                    "new_delivery_id": stamped[0].get("delivery_id")
                    if stamped else None})
            self._send_json(200, {
                "feed_id": feed_id,
                "attempted": len(results),
                "delivered": sum(1 for r in results
                                 if r["status"] == "delivered"),
                "failed": sum(1 for r in results
                              if r["status"] != "delivered"),
                "results": results,
                "skipped": result["skipped"]})

        def _handle_webhook_create(self, feed_id):
            """Register a push webhook on a multi-feed:
            POST /api/multi-feeds/<id>/webhooks {"url": ...,
            "secret": ..., "events": [...]}. Registration fires
            one multi_feed.subscribed test delivery to the new
            webhook (recorded either way), so the subscriber
            learns immediately whether their endpoint works —
            unless their own "events" filter excludes
            multi_feed.subscribed, in which case no test
            delivery is sent or recorded, by their choice; the
            response then carries "delivery": null alongside
            the stored filter. The response names the webhook
            by id + host label only — the stored URL is never
            echoed.

            Signing: an optional "secret" is the subscriber's
            own signing secret (validated, never echoed — they
            already know it); an explicit null registers an
            unsigned webhook; with no "secret" key the server
            generates one and returns it in this response as
            "signing_secret" — the ONLY surface that ever
            shows it, so the subscriber saves it now to verify
            the HMAC signature header on every later push."""
            
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_WEBHOOK_BODY_BYTES:
                self._send_json(413, {"error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("url"), str):
                self._send_json(
                    400, {"error": "webhook failed: bad_webhook_url"})
                return
            try:
                url = validate_webhook_url(payload["url"])
            except ValueError as exc:  # categorised, never echoes URL
                self._send_json(
                    400, {"error": f"webhook failed: {exc}"})
                return
            if "secret" in payload:
                if payload["secret"] is None:
                    signing_secret = None
                else:
                    try:
                        signing_secret = validate_signing_secret(
                            payload["secret"])
                    except ValueError as exc:  # never echoes it
                        self._send_json(
                            400, {"error": f"webhook failed: {exc}"})
                        return
            else:
                signing_secret = "generate"
            if "events" in payload:
                try:
                    events = validate_webhook_events(
                        payload["events"])
                except ValueError as exc:  # never echoes values
                    self._send_json(
                        400, {"error": f"webhook failed: {exc}"})
                    return
            else:
                events = None
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            secret_out = {}
            status, meta = multi_feed_store.add_webhook(
                feed_id, url, owner_token=token,
                signing_secret=signing_secret, secret_out=secret_out,
                events=events)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can manage "
                                   "webhooks for this multi-feed"})
                return
            if status == "too_many_webhooks":
                self._send_json(
                    400, {"error": "webhook failed: too_many_webhooks"})
                return
            deliveries = self._notify_multi_feed(
                feed_id, "subscribed", None,
                only_webhook_id=meta["webhook_id"])
            response = {
                **meta, "feed_id": feed_id,
                "delivery": deliveries[0] if deliveries else None,
            }
            if "signing_secret" in secret_out:
                # Shown exactly once, here: the subscriber saves
                # it now to verify later pushes.
                response["signing_secret"] = secret_out["signing_secret"]
            self._send_json(201, response)

        def _handle_webhooks_list(self, feed_id):
            """The feed's registered webhooks (host labels only)."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, webhooks = multi_feed_store.webhooks(
                feed_id, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can view "
                                   "webhooks for this multi-feed"})
                return
            self._send_json(200, {"feed_id": feed_id,
                                  "webhooks": webhooks,
                                  "count": len(webhooks)})

        def _handle_webhook_pause(self, feed_id, webhook_id, paused):
            """Pause or resume one webhook on a multi-feed:
            POST /api/multi-feeds/<id>/webhooks/<webhook_id>/
            pause|resume. Pausing by hand stops pushes to a
            failing endpoint immediately; a webhook also pauses
            itself after PAUSE_AFTER_CONSECUTIVE_FAILURES
            consecutive failed deliveries. Resuming clears the
            consecutive-failure count and is the subscriber's
            deliberate act (owner rule as for the feed). The
            response is the webhook meta — host label, paused
            state, and failure count; never the stored URL."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, meta = multi_feed_store.set_webhook_paused(
                feed_id, webhook_id, paused, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can manage "
                                   "webhooks for this multi-feed"})
                return
            if status == "webhook_not_found":
                self._send_json(404, {"error": "unknown webhook id"})
                return
            self._send_json(200, {**meta, "feed_id": feed_id})

        def _handle_webhook_rotate_secret(self, feed_id, webhook_id):
            """Rotate one webhook's signing secret in place:
            POST /api/multi-feeds/<id>/webhooks/<webhook_id>/
            rotate-secret. The body is optional: no body, an
            empty object, or no "secret" key generates a fresh
            secret, returned in this response as
            "signing_secret" — the ONLY surface that ever shows
            it, exactly as at registration; {"secret": "..."}
            installs the subscriber's own secret (validated,
            never echoed — they already know it); an explicit
            {"secret": null} un-signs the webhook. The old
            secret stops verifying immediately. Rotation never
            delivers anything and leaves the webhook's pause
            state and failure count untouched (owner rule as
            for the feed)."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_WEBHOOK_BODY_BYTES:
                self._send_json(413, {"error": "request body too large"})
                return
            raw = self.rfile.read(length) if length else b""
            if raw.strip():
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(400, {"error": "invalid JSON body"})
                    return
                if not isinstance(payload, dict):
                    self._send_json(400, {"error": "invalid JSON body"})
                    return
            else:
                payload = {}
            if "secret" in payload:
                if payload["secret"] is None:
                    signing_secret = None
                else:
                    try:
                        signing_secret = validate_signing_secret(
                            payload["secret"])
                    except ValueError as exc:  # never echoes it
                        self._send_json(
                            400, {"error": f"webhook failed: {exc}"})
                        return
            else:
                signing_secret = "generate"
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            secret_out = {}
            status, meta = multi_feed_store.rotate_webhook_secret(
                feed_id, webhook_id, owner_token=token,
                signing_secret=signing_secret, secret_out=secret_out)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can manage "
                                   "webhooks for this multi-feed"})
                return
            if status == "webhook_not_found":
                self._send_json(404, {"error": "unknown webhook id"})
                return
            response = {**meta, "feed_id": feed_id}
            if "signing_secret" in secret_out:
                # Shown exactly once, here: the subscriber saves
                # it now to verify later pushes.
                response["signing_secret"] = secret_out["signing_secret"]
            self._send_json(200, response)

        def _handle_webhook_rotate_url(self, feed_id, webhook_id):
            """Point one webhook at a new endpoint URL in place:
            POST /api/multi-feeds/<id>/webhooks/<webhook_id>/
            rotate-url {"url": ...}. Until now a subscriber
            whose endpoint moved (a new domain, a rotated
            tunnel) had one remedy — remove the webhook and
            register it again, resetting its pause state,
            failure count, and registration moment, and
            orphaning its signing secret. Rotation is the
            managed-endpoint remedy: the URL is validated
            exactly as at registration (categorised 400s, the
            URL itself never echoed beyond its category), the
            webhook id is recomputed from the new URL, and the
            secret, pause state, failure count, and created_at
            all survive; the response meta names the new host
            label and the rotation moment. Rotating onto a URL
            another webhook on the feed already holds is a 400
            (webhook_exists) — one endpoint, one registration.
            Rotation never delivers anything (owner rule as
            for the feed)."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_WEBHOOK_BODY_BYTES:
                self._send_json(413, {"error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("url"), str):
                self._send_json(
                    400, {"error": "webhook failed: bad_webhook_url"})
                return
            try:
                url = validate_webhook_url(payload["url"])
            except ValueError as exc:  # categorised, never echoes URL
                self._send_json(
                    400, {"error": f"webhook failed: {exc}"})
                return
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, meta = multi_feed_store.rotate_webhook_url(
                feed_id, webhook_id, url, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can manage "
                                   "webhooks for this multi-feed"})
                return
            if status == "webhook_not_found":
                self._send_json(404, {"error": "unknown webhook id"})
                return
            if status == "webhook_exists":
                self._send_json(
                    400, {"error": "webhook failed: webhook_exists"})
                return
            self._send_json(200, {**meta, "feed_id": feed_id})

        def _handle_webhook_set_events(self, feed_id, webhook_id):
            """Replace one webhook's event filter in place:
            POST /api/multi-feeds/<id>/webhooks/<webhook_id>/
            set-events {"events": [...]}. Until now a
            subscriber who wanted fewer pushes (only updates,
            say, and not the registration test event on any
            future re-registration) had one remedy — remove
            the webhook and register it again, resetting its
            pause state, failure count, and signing secret.
            Setting the filter is the managed-endpoint remedy:
            the list is validated exactly as at registration
            (categorised 400s, the supplied values never
            echoed), an explicit {"events": null} resets the
            webhook to every event, and the change is stamped
            (events_updated_at) while the id, URL, secret,
            pause state, failure count, and created_at all
            survive. Setting the current filter is idempotent
            and stamps nothing. Setting delivers nothing
            (owner rule as for the feed)."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_WEBHOOK_BODY_BYTES:
                self._send_json(413, {"error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    "events" not in payload:
                self._send_json(
                    400, {"error": "webhook failed: bad_webhook_events"})
                return
            try:
                events = validate_webhook_events(payload["events"])
            except ValueError as exc:  # never echoes the values
                self._send_json(
                    400, {"error": f"webhook failed: {exc}"})
                return
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, meta = multi_feed_store.set_webhook_events(
                feed_id, webhook_id, events, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can manage "
                                   "webhooks for this multi-feed"})
                return
            if status == "webhook_not_found":
                self._send_json(404, {"error": "unknown webhook id"})
                return
            self._send_json(200, {**meta, "feed_id": feed_id})

        def _handle_webhook_test(self, feed_id, webhook_id):
            """Fire one on-demand test push to a webhook:
            POST /api/multi-feeds/<id>/webhooks/<webhook_id>/
            test (no body). Until now the only test delivery
            a webhook ever received was the registration ping:
            a subscriber who rotated an endpoint, rotated a
            signing secret, or replaced an event filter had no
            way to verify the endpoint worked — and that the
            signature verifies — without forcing a feed
            update. The test push carries the feed's current
            version, etag, timeline names, and recomputed
            verdict under the event multi_feed.test, is
            recorded like any push, and counts toward the
            webhook's delivery health like any push. It is
            the subscriber's explicit act, so it is sent
            regardless of the webhook's event filter (the
            filter governs automatic pushes only, exactly as
            for redelivery); a paused webhook is refused
            (webhook_paused, owner rule as for the feed) —
            resuming is how a paused endpoint comes back.
            The response is the webhook meta (refreshed after
            the delivery, so its failure count is current)
            plus the delivery record."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, webhooks = multi_feed_store.webhooks(
                feed_id, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can manage "
                                   "webhooks for this multi-feed"})
                return
            meta = next((w for w in webhooks
                         if w.get("webhook_id") == webhook_id), None)
            if meta is None:
                self._send_json(404, {"error": "unknown webhook id"})
                return
            if meta.get("paused"):
                self._send_json(
                    400, {"error": "webhook failed: webhook_paused"})
                return
            payload = multi_feed_store.get(feed_id)
            all_passed = None
            if payload is not None:
                try:
                    _content, all_passed, _summaries = \
                        export_multi_timeline_ics(payload["timelines"])
                except (ValueError, KeyError, TypeError,
                        AttributeError, IndexError):
                    all_passed = None
            deliveries = self._notify_multi_feed(
                feed_id, "test", all_passed,
                only_webhook_id=webhook_id, bypass_filter=True)
            status, webhooks = multi_feed_store.webhooks(
                feed_id, owner_token=token)
            if status == "ok":
                meta = next((w for w in webhooks
                             if w.get("webhook_id") == webhook_id),
                            meta)
            self._send_json(200, {
                **meta, "feed_id": feed_id,
                "delivery": deliveries[0] if deliveries else None})

        def _handle_notifications(self, feed_id):
            """The feed's push delivery log: one record per
            delivery attempt (registration pings + update pushes),
            oldest first — how a subscriber audits what was
            pushed, what arrived, and what failed, without any
            server-side subscriber registry beyond their own
            feed's webhooks."""
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            status, deliveries = multi_feed_store.deliveries(
                feed_id, owner_token=token)
            if status == "not_found":
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if status == "forbidden":
                self._send_json(
                    403, {"error": "only the owner can view "
                                   "notifications for this multi-feed"})
                return
            self._send_json(200, {"feed_id": feed_id,
                                  "deliveries": deliveries,
                                  "count": len(deliveries)})

        def _handle_suggest(self):
            """Guided branch suggestions for one base plan.

            Body: {"plan": {...}, "limit": 5 (optional),
            "history": [{...plan...}, ...] (optional)}. The plan
            is untrusted input held to the swap endpoint's gates;
            swap.suggest_branches verifies the plan first, ranks
            only admissible pairs by affinity delta + taste
            coverage + the learned adjustment from the sealed
            swap history (the base plan's own, plus any history
            plans — each shape-gated here, and one whose chain
            does not verify is ignored and counted, never
            believed), and returns branch specs the /api/branches
            endpoint accepts unchanged. Nothing is applied or
            re-sealed. Refusals are categorised 400s."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_VERIFY_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("plan"), dict):
                self._send_json(400, {"error": "plan is required"})
                return
            plan = payload["plan"]
            error = validate_plan_payload(plan)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            limit = payload.get("limit", 5)
            history = payload.get("history")
            history_plans = None
            if history is not None:
                if not isinstance(history, list) \
                        or any(not isinstance(p, dict) for p in history):
                    self._send_json(
                        400, {"error": "suggest failed: bad_history"})
                    return
                for past in history:
                    error = validate_plan_payload(past)
                    if error is not None:
                        self._send_json(
                            400, {"error": "suggest failed: bad_history"})
                        return
                history_plans = history
            # A presented owner token with a stored profile folds
            # that persistent profile into the ranking (reported as
            # learning.stored_applied); no token keeps the exact
            # per-request behaviour, a malformed one is a 400.
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            stored_profile = profile_store.get(token) \
                if token is not None else None
            try:
                result = suggest_branches(
                    plan, limit=limit, history_plans=history_plans,
                    stored_profile=stored_profile)
            except ValueError as exc:  # categorised refusal
                self._send_json(400, {"error": f"suggest failed: {exc}"})
                return
            except (KeyError, TypeError, AttributeError, IndexError):
                self._send_json(400, {"error": "suggest failed: bad_plan"})
                return
            self._send_json(200, result)

        def _handle_share(self):
            """Store a plan under its content-addressed share id.

            The payload is untrusted input, held to exactly the verify
            endpoint's caps and shape gate. Storing grants the plan no
            trust: every fetch-by-link verification recomputes the
            chain and checker verdicts from the sealed record."""
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_SHARE_BODY_BYTES:
                self._send_json(413, {"error": "plan too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            error = validate_plan_payload(payload)
            if error is not None:
                self._send_json(400, {"error": error})
                return
            token, invalid = self._owner_token()
            if invalid:
                self._send_json(400, {"error": "invalid owner token"})
                return
            meta = share_store.put(payload, owner_token=token)
            share_id = meta["share_id"]
            self._send_json(201, {
                "share_id": share_id,
                "url": f"/?share={share_id}",
                "verify_url": f"/api/share/{share_id}/verify",
                "expires_at": meta["expires_at"],
                "owned": meta["owned"],
                "owner_fingerprint": meta["owner_fingerprint"],
            })

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/verify":
                self._handle_verify()
                return
            if path == "/api/export":
                fmt = parse_qs(urlparse(self.path).query).get(
                    "format", ["markdown"])[0]
                self._handle_export(fmt)
                return
            if path == "/api/export-timeline":
                fmt = parse_qs(urlparse(self.path).query).get(
                    "format", ["markdown"])[0]
                self._handle_export_timeline(fmt)
                return
            if path == "/api/multi-feeds":
                self._handle_multi_feed_create()
                return
            if path.startswith("/api/multi-feeds/"):
                rest = unquote(path[len("/api/multi-feeds/"):])
                parts = rest.split("/")
                if len(parts) == 3 and parts[1] == "notifications" \
                        and parts[2] == "redeliver-failed" \
                        and parts[0]:
                    self._handle_notifications_redeliver_failed(
                        parts[0])
                elif len(parts) == 4 and parts[1] == "notifications" \
                        and parts[3] == "redeliver" \
                        and parts[0] and parts[2]:
                    self._handle_notification_redeliver(
                        parts[0], parts[2])
                elif len(parts) == 4 and parts[1] == "webhooks" \
                        and parts[3] in ("pause", "resume") \
                        and parts[0] and parts[2]:
                    self._handle_webhook_pause(
                        parts[0], parts[2], parts[3] == "pause")
                elif len(parts) == 4 and parts[1] == "webhooks" \
                        and parts[3] == "rotate-secret" \
                        and parts[0] and parts[2]:
                    self._handle_webhook_rotate_secret(
                        parts[0], parts[2])
                elif len(parts) == 4 and parts[1] == "webhooks" \
                        and parts[3] == "rotate-url" \
                        and parts[0] and parts[2]:
                    self._handle_webhook_rotate_url(
                        parts[0], parts[2])
                elif len(parts) == 4 and parts[1] == "webhooks" \
                        and parts[3] == "set-events" \
                        and parts[0] and parts[2]:
                    self._handle_webhook_set_events(
                        parts[0], parts[2])
                elif len(parts) == 4 and parts[1] == "webhooks" \
                        and parts[3] == "test" \
                        and parts[0] and parts[2]:
                    self._handle_webhook_test(
                        parts[0], parts[2])
                elif rest.endswith("/webhooks") and \
                        "/" not in rest[:-len("/webhooks")]:
                    self._handle_webhook_create(
                        rest[:-len("/webhooks")])
                elif rest and "/" not in rest:
                    self._handle_multi_feed_update(rest)
                else:
                    self._send_json(
                        404, {"error": "unknown multi-feed id"})
                return
            if path == "/api/feeds":
                self._handle_feed_create()
                return
            if path.startswith("/api/feeds/"):
                feed_id = unquote(path[len("/api/feeds/"):])
                if feed_id and "/" not in feed_id:
                    self._handle_feed_update(feed_id)
                else:
                    self._send_json(404, {"error": "unknown feed id"})
                return
            if path == "/api/share":
                self._handle_share()
                return
            if path == "/api/swap":
                self._handle_swap()
                return
            if path == "/api/undo":
                self._handle_undo()
                return
            if path == "/api/branches":
                self._handle_branches()
                return
            if path == "/api/suggest":
                self._handle_suggest()
                return
            if path == "/api/diff":
                self._handle_diff()
                return
            if path == "/api/timeline":
                self._handle_timeline()
                return
            if path == "/api/profile":
                self._handle_profile_update()
                return
            if path != "/api/plan":
                self._send(404, b"not found", "text/plain")
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_PLAN_BODY_BYTES:
                # Reject on the declared size alone; never read the body.
                self._send_json(413, {"error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            if not isinstance(payload, dict) or \
                    not isinstance(payload.get("text"), str):
                self._send_json(400, {"error": "text is required"})
                return
            if length > MAX_BODY_BYTES and "imports" not in payload \
                    and "timetables" not in payload:
                # The plain-body cap still applies in full: only a body
                # actually carrying file attachments may use the larger
                # plan cap (checked on the declared length, enforced
                # after parsing because the keys live inside the body).
                self._send_json(413, {"error": "request body too large"})
                return
            text = payload["text"].strip()
            if not text:
                self._send_json(400, {"error": "text is required"})
                return
            if len(text) > MAX_TEXT_CHARS:
                self._send_json(413, {"error": "text too long"})
                return
            try:
                imported, timetables = parse_plan_attachments(payload)
            except ValueError as exc:  # categorised, never echoes content
                self._send_json(400, {"error": str(exc)})
                return
            try:
                result = agent.run(text, imported=imported,
                                   timetables=timetables)
            except ValueError as exc:  # caller-fixable input problem
                self._send_json(400, {"error": str(exc)})
                return
            except Exception:  # never leak internal detail to the client
                self._send_json(500, {"error": "internal error"})
                return
            record = result.get("evidence") or {}
            for rid, resp in qloo_responses(record).items():
                evidence_cache[rid] = (record, resp)
                evidence_cache.move_to_end(rid)
                while len(evidence_cache) > max_evidence_records:
                    evidence_cache.popitem(last=False)
            self._send_json(200, result)

        def _method_not_allowed(self):
            self._send_json(405, {"error": "method not allowed"})

        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed

        def do_DELETE(self):
            """DELETE is supported for exactly three routes:
            revoking a share link, revoking a timeline feed, and
            clearing the caller's own preference profile. Every
            other DELETE target stays 405, matching the hardening
            contract for unsupported methods."""
            path = urlparse(self.path).path
            if path == "/api/profile":
                token = self._profile_token()
                if token is None:
                    return
                if profile_store.clear(token):
                    self._send_json(200, {"cleared": True})
                else:
                    self._send_json(404, {"error": "no profile stored"})
                return
            if path.startswith("/api/multi-feeds/"):
                rest = unquote(path[len("/api/multi-feeds/"):])
                parts = rest.split("/")
                if len(parts) == 3 and parts[1] == "webhooks" \
                        and parts[0] and parts[2]:
                    token, invalid = self._owner_token()
                    if invalid:
                        self._send_json(
                            400, {"error": "invalid owner token"})
                        return
                    status = multi_feed_store.remove_webhook(
                        parts[0], parts[2], owner_token=token)
                    if status == "removed":
                        self._send_json(200, {"removed": True,
                                              "feed_id": parts[0],
                                              "webhook_id": parts[2]})
                        return
                    if status == "forbidden":
                        self._send_json(
                            403, {"error": "only the owner can manage "
                                           "webhooks for this multi-feed"})
                        return
                    self._send_json(
                        404, {"error": "unknown multi-feed or webhook id"})
                    return
                feed_id = rest
                if not feed_id or "/" in feed_id:
                    self._send_json(
                        404, {"error": "unknown multi-feed id"})
                    return
                token, invalid = self._owner_token()
                if invalid:
                    self._send_json(400, {"error": "invalid owner token"})
                    return
                status = multi_feed_store.revoke_status(
                    feed_id, owner_token=token)
                if status == "revoked":
                    self._send_json(200, {"revoked": True,
                                          "feed_id": feed_id})
                    return
                if status == "forbidden":
                    self._send_json(
                        403, {"error": "only the owner can revoke "
                                       "this multi-feed"})
                    return
                self._send_json(404, {"error": "unknown multi-feed id"})
                return
            if path.startswith("/api/feeds/"):
                feed_id = unquote(path[len("/api/feeds/"):])
                if not feed_id or "/" in feed_id:
                    self._send_json(404, {"error": "unknown feed id"})
                    return
                token, invalid = self._owner_token()
                if invalid:
                    self._send_json(400, {"error": "invalid owner token"})
                    return
                status = feed_store.revoke_status(feed_id,
                                                  owner_token=token)
                if status == "revoked":
                    self._send_json(200, {"revoked": True,
                                          "feed_id": feed_id})
                    return
                if status == "forbidden":
                    self._send_json(
                        403, {"error": "only the owner can revoke "
                                       "this feed"})
                    return
                self._send_json(404, {"error": "unknown feed id"})
                return
            if path.startswith("/api/share/"):
                share_id = unquote(path[len("/api/share/"):])
                if not share_id or "/" in share_id:
                    self._send_json(404, {"error": "unknown share id"})
                    return
                token, invalid = self._owner_token()
                if invalid:
                    self._send_json(400, {"error": "invalid owner token"})
                    return
                status = share_store.revoke_status(share_id,
                                                     owner_token=token)
                if status == "revoked":
                    self._send_json(200, {"revoked": True,
                                          "share_id": share_id})
                    return
                if status == "forbidden":
                    self._send_json(
                        403, {"error": "only the owner can revoke "
                                       "this share"})
                    return
                self._send_json(404, {"error": "unknown share id"})
                return
            self._method_not_allowed()

    return Handler


def serve(agent, port: int = 8765, host: str = "127.0.0.1",
          share_file: str | Path | None = None,
          share_ttl_seconds: float = DEFAULT_SHARE_TTL_SECONDS,
          profile_file: str | Path | None = None,
          feed_file: str | Path | None = None,
          feed_ttl_seconds: float = DEFAULT_FEED_TTL_SECONDS,
          multi_feed_file: str | Path | None = None,
          multi_feed_ttl_seconds: float = DEFAULT_FEED_TTL_SECONDS) -> None:
    if share_file is None:
        share_file = os.environ.get(SHARE_FILE_ENV_VAR) or None
    if profile_file is None:
        profile_file = os.environ.get(PROFILE_FILE_ENV_VAR) or None
    if feed_file is None:
        feed_file = os.environ.get(FEED_FILE_ENV_VAR) or None
    if multi_feed_file is None:
        multi_feed_file = os.environ.get(MULTI_FEED_FILE_ENV_VAR) or None
    handler = make_handler(agent, share_file=share_file,
                           share_ttl_seconds=share_ttl_seconds,
                           profile_file=profile_file,
                           feed_file=feed_file,
                           feed_ttl_seconds=feed_ttl_seconds,
                           multi_feed_file=multi_feed_file,
                           multi_feed_ttl_seconds=multi_feed_ttl_seconds)
    server = ThreadingHTTPServer((host, port), handler)
    shares = "persistent" if share_file else "in-memory"
    profiles = "persistent" if profile_file else "in-memory"
    feeds = "persistent" if feed_file else "in-memory"
    multi_feeds = "persistent" if multi_feed_file else "in-memory"
    print(f"TastePilot listening on http://{host}:{port} "
          f"(mode={agent.mode}, explainer={agent.explainer_info['source']}, "
          f"shares={shares}, profiles={profiles}, feeds={feeds}, "
          f"multi_feeds={multi_feeds})",
          flush=True)  # hosted log streams must see startup immediately
    server.serve_forever()
