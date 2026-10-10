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
    evicted first) so a long-lived hosted demo cannot grow without limit.
  * ``serve()`` takes an explicit host: local runs keep the safe
    127.0.0.1 default; hosted deployments pass ``--host 0.0.0.0`` and the
    port from the platform's ``PORT`` env var (see DEPLOY.md).
"""
from __future__ import annotations

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

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The demo UI is a single self-contained page with inline style/script;
    # everything else defaults to same-origin only.
    "Content-Security-Policy":
        "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
}


def make_handler(agent, max_evidence_records: int = MAX_EVIDENCE_RECORDS):
    # response_id -> (record, response) for plans produced by this server,
    # bounded FIFO: the oldest sealed responses are evicted first.
    evidence_cache: OrderedDict[str, tuple[dict, dict]] = OrderedDict()

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
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "plan must be a JSON object"})
                return
            record = payload.get("evidence", payload)
            items = payload.get("items", [])
            if not isinstance(record, dict) or \
                    not isinstance(record.get("entries"), list):
                self._send_json(400,
                                {"error": "evidence record missing or malformed"})
                return
            if not isinstance(items, list) or \
                    any(not isinstance(i, dict) for i in items):
                self._send_json(400,
                                {"error": "items must be a list of objects"})
                return
            try:
                report = check_plan(items, record)
                reports = item_reports(items, record)
                sealed = len(qloo_responses(record))
            except (KeyError, TypeError, ValueError, AttributeError,
                    IndexError):
                self._send_json(400,
                                {"error": "malformed plan or evidence record"})
                return
            self._send_json(200, {
                "chain_valid": report["chain_valid"],
                "passed": report["passed"],
                "violations": report["violations"],
                "items": reports,
                "item_count": len(items),
                "sealed_responses": sealed,
                # Informational only: what the record claims about
                # itself. The verdicts above are recomputed, never read
                # from this field.
                "record_verdict": record.get("verdict"),
            })

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/verify":
                self._handle_verify()
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
