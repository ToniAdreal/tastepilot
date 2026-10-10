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
  GET  /api/share/<id>         the stored plan JSON (404 if unknown or
                               evicted from the bounded share store).
  GET  /api/share/<id>/verify  the recomputed chain + per-item checker
                               verdict for the stored plan — exactly the
                               /api/verify report, plus the share id.

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
from collections import OrderedDict
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


def make_handler(agent, max_evidence_records: int = MAX_EVIDENCE_RECORDS,
                 max_shared_plans: int = MAX_SHARED_PLANS):
    # response_id -> (record, response) for plans produced by this server,
    # bounded FIFO: the oldest sealed responses are evicted first.
    evidence_cache: OrderedDict[str, tuple[dict, dict]] = OrderedDict()
    # share_id -> plan payload, bounded FIFO like the evidence cache: a
    # long-lived hosted demo cannot accumulate shared plans without
    # limit, and an evicted id simply 404s like an unknown one.
    shared_plans: OrderedDict[str, dict] = OrderedDict()

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
                                      "explainer": agent.explainer_info})
            elif path.startswith("/api/share/"):
                rest = unquote(path[len("/api/share/"):])
                want_verify = rest.endswith("/verify")
                share_id = rest[:-len("/verify")] if want_verify else rest
                plan = shared_plans.get(share_id)
                if plan is None or "/" in share_id:
                    self._send_json(404, {"error": "unknown share id"})
                    return
                shared_plans.move_to_end(share_id)
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
            share_id = share_id_for(payload)
            shared_plans[share_id] = copy.deepcopy(payload)
            shared_plans.move_to_end(share_id)
            while len(shared_plans) > max_shared_plans:
                shared_plans.popitem(last=False)
            self._send_json(201, {
                "share_id": share_id,
                "url": f"/?share={share_id}",
                "verify_url": f"/api/share/{share_id}/verify",
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
        do_DELETE = _method_not_allowed

    return Handler


def serve(agent, port: int = 8765, host: str = "127.0.0.1") -> None:
    server = ThreadingHTTPServer((host, port), make_handler(agent))
    print(f"TastePilot listening on http://{host}:{port} "
          f"(mode={agent.mode}, explainer={agent.explainer_info['source']})",
          flush=True)  # hosted log streams must see startup immediately
    server.serve_forever()
