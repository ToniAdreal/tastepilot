"""Minimal stdlib HTTP server: chat UI + JSON plan API (Judge Mode demo).

Kept dependency-free so a judge can run the hosted-demo equivalent with
nothing but Python 3.11+:  python -m tastepilot serve --port 8765

Routes:
  GET  /                       demo UI (chat + grouped plan + evidence viewer)
  GET  /api/health             {"ok": true, "mode": ..., "explainer": {...}}
  POST /api/plan               {"text": ...} -> full plan result (agent.run)
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
  DELETE /api/share/<id>       revoke a share link: the stored plan is
                               dropped and the id 404s from then on
                               ({"revoked": true}). Knowing the id is
                               the capability — whoever holds the link
                               can revoke it; that is the documented
                               demo trade-off. DELETE anywhere else
                               stays 405 like the other unsupported
                               methods.

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
from urllib.parse import unquote, urlparse

from .checker import check_plan, item_reports
from .evidence import qloo_responses, verify_chain

STATIC = Path(__file__).resolve().parent / "static"

MAX_BODY_BYTES = 16_384          # a plan request is a short JSON object
MAX_VERIFY_BODY_BYTES = 524_288  # a submitted plan + sealed record (~25 KiB
                                 # for the shipped samples) needs more room,
                                 # but stays bounded like every other input
MAX_TEXT_CHARS = 2_000           # matches the agent-level guard
MAX_EVIDENCE_RECORDS = 512       # sealed responses kept for the viewer
MAX_SHARED_PLANS = 128           # shared plans kept for link verification
MAX_SHARE_BODY_BYTES = MAX_VERIFY_BODY_BYTES  # a shared plan is a plan
DEFAULT_SHARE_TTL_SECONDS = 7 * 24 * 3600  # share links live for 7 days
SHARE_FILE_ENV_VAR = "TASTEPILOT_SHARE_FILE"  # opt-in persistence path

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The demo UI is a single self-contained page with inline style/script;
    # everything else defaults to same-origin only.
    "Content-Security-Policy":
        "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
}


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
    return None


def verification_report(payload: dict) -> dict:
    """Recompute the chain + per-item checker verdict for a payload.

    The payload must already have passed validate_plan_payload. The
    record's own verdict field is echoed only as an untrusted claim.
    Raises the checker's malformed-input errors for the caller to map
    to a 400."""
    record = payload.get("evidence", payload)
    items = payload.get("items", [])
    report = check_plan(items, record)
    return {
        "chain_valid": report["chain_valid"],
        "passed": report["passed"],
        "violations": report["violations"],
        "items": item_reports(items, record),
        "item_count": len(items),
        "sealed_responses": len(qloo_responses(record)),
        "record_verdict": record.get("verdict"),
    }


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


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
            self._entries[share_id] = {
                "payload": copy.deepcopy(payload),
                "created_at": created_at if isinstance(
                    created_at, (int, float)) else expires_at,
                "expires_at": float(expires_at),
            }
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
        return {"share_id": share_id,
                "created_at": _iso(entry["created_at"]),
                "expires_at": _iso(entry["expires_at"])}

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

    def put(self, payload: dict) -> dict:
        """Store a plan payload and return its link metadata.

        Idempotent for identical content: re-storing the same plan
        returns the original entry — the expiry is NOT extended, so a
        link's lifetime cannot be quietly renewed by re-posting it."""
        share_id = share_id_for(payload)
        with self._lock:
            self._prune_expired()
            entry = self._entries.get(share_id)
            if entry is None:
                now = self._clock()
                entry = {"payload": copy.deepcopy(payload),
                         "created_at": now,
                         "expires_at": now + self.ttl_seconds}
                self._entries[share_id] = entry
                while len(self._entries) > self.max_plans:
                    self._entries.popitem(last=False)
            else:
                self._entries.move_to_end(share_id)
            self._save()
            return self._meta(share_id, entry)

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

    def revoke(self, share_id: str) -> bool:
        """Drop an entry now. True when something was revoked; an
        unknown or already-expired id revokes nothing (False)."""
        with self._lock:
            if self._prune_expired():
                self._save()
            if share_id not in self._entries:
                return False
            del self._entries[share_id]
            self._save()
            return True


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

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            if ctype.startswith("application/json"):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif path == "/api/health":
                self._send_json(200, {"ok": True, "mode": agent.mode,
                                      "explainer": agent.explainer_info,
                                      "shares": share_store.info()})
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
            meta = share_store.put(payload)
            share_id = meta["share_id"]
            self._send_json(201, {
                "share_id": share_id,
                "url": f"/?share={share_id}",
                "verify_url": f"/api/share/{share_id}/verify",
                "expires_at": meta["expires_at"],
            })

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/verify":
                self._handle_verify()
                return
            if path == "/api/share":
                self._handle_share()
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
            if length > MAX_BODY_BYTES:
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
            text = payload["text"].strip()
            if not text:
                self._send_json(400, {"error": "text is required"})
                return
            if len(text) > MAX_TEXT_CHARS:
                self._send_json(413, {"error": "text too long"})
                return
            try:
                result = agent.run(text)
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
                if share_id and "/" not in share_id and \
                        share_store.revoke(share_id):
                    self._send_json(200, {"revoked": True,
                                          "share_id": share_id})
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
