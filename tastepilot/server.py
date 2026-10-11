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
  POST /api/suggest            {"plan": {...}, "limit": 5} -> guided
                               branch suggestions: every admissible
                               primary/alternative pair ranked by
                               affinity delta + taste coverage, as
                               branch specs /api/branches accepts.
                               Suggestions propose only — nothing is
                               applied, mutated, or re-sealed.
  GET  /api/shares             the caller's own share links (dashboard):
                               requires an ``X-Owner-Token`` header and
                               returns only the shares that token owns —
                               id, url, expiry, item count, and the
                               recomputed checker verdict per link.
  DELETE /api/share/<id>       revoke a share link: the stored plan is
                               dropped and the id 404s from then on
                               ({"revoked": true}). For an owner-managed
                               link (below) only the owner's token can
                               revoke it (403 otherwise); for a legacy
                               link created without a token, knowing the
                               id remains the capability — whoever holds
                               the link can revoke it; that is the
                               documented demo trade-off for unowned
                               links. DELETE anywhere else stays 405
                               like the other unsupported methods.

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
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .checker import check_plan, item_reports
from .evidence import qloo_responses, verify_chain
from .export import export_plan, media_for
from .imports import parse_import_bytes
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


def make_handler(agent, max_evidence_records: int = MAX_EVIDENCE_RECORDS,
                 max_shared_plans: int = MAX_SHARED_PLANS,
                 share_ttl_seconds: float = DEFAULT_SHARE_TTL_SECONDS,
                 share_file: str | Path | None = None,
                 share_clock=None):
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

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif path == "/api/health":
                self._send_json(200, {"ok": True, "mode": agent.mode,
                                      "explainer": agent.explainer_info,
                                      "shares": share_store.info()})
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

        def _handle_suggest(self):
            """Guided branch suggestions for one base plan.

            Body: {"plan": {...}, "limit": 5 (optional)}. The plan
            is untrusted input held to the swap endpoint's gates;
            swap.suggest_branches verifies the plan first, ranks
            only admissible pairs by affinity delta + taste
            coverage, and returns branch specs the /api/branches
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
            try:
                result = suggest_branches(plan, limit=limit)
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
            """DELETE is supported for exactly one route: revoking a
            share link. Every other DELETE target stays 405, matching
            the hardening contract for unsupported methods."""
            path = urlparse(self.path).path
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
          share_ttl_seconds: float = DEFAULT_SHARE_TTL_SECONDS) -> None:
    if share_file is None:
        share_file = os.environ.get(SHARE_FILE_ENV_VAR) or None
    handler = make_handler(agent, share_file=share_file,
                           share_ttl_seconds=share_ttl_seconds)
    server = ThreadingHTTPServer((host, port), handler)
    shares = "persistent" if share_file else "in-memory"
    print(f"TastePilot listening on http://{host}:{port} "
          f"(mode={agent.mode}, explainer={agent.explainer_info['source']}, "
          f"shares={shares})",
          flush=True)  # hosted log streams must see startup immediately
    server.serve_forever()
