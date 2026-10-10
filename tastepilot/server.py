"""Minimal stdlib HTTP server: chat UI + JSON plan API (Judge Mode demo).

Kept dependency-free so a judge can run the hosted-demo equivalent with
nothing but Python 3.11+:  python -m tastepilot serve --port 8765

Routes:
  GET  /                       demo UI (chat + grouped plan + evidence viewer)
  GET  /api/health             {"ok": true, "mode": ...}
  POST /api/plan               {"text": ...} -> full plan result (agent.run)
  GET  /api/evidence/<id>      one sealed Qloo response from a plan this
                               server produced, plus its record's hash and
                               chain validity — the evidence-record viewer
                               backend. Unknown ids 404; nothing is served
                               that was not sealed into a plan's record.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .evidence import qloo_responses, verify_chain

STATIC = Path(__file__).resolve().parent / "static"


def make_handler(agent):
    # response_id -> (record, response) for plans produced by this server.
    evidence_cache: dict[str, tuple[dict, dict]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep demo logs clean
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
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
                self._send_json(200, {"ok": True, "mode": agent.mode})
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

        def do_POST(self):
            if urlparse(self.path).path != "/api/plan":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required")
                result = agent.run(text)
                record = result.get("evidence") or {}
                for rid, resp in qloo_responses(record).items():
                    evidence_cache[rid] = (record, resp)
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})

    return Handler


def serve(agent, port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(agent))
    print(f"TastePilot listening on http://127.0.0.1:{port} (mode={agent.mode})")
    server.serve_forever()
