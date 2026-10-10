"""Minimal stdlib HTTP server: chat UI + JSON plan API (Judge Mode demo).

Kept dependency-free so a judge can run the hosted-demo equivalent with
nothing but Python 3.11+:  python -m tastepilot serve --port 8765
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"


def make_handler(agent):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep demo logs clean
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif self.path == "/api/health":
                self._send(200, json.dumps({"ok": True, "mode": agent.mode}).encode(),
                           "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/plan":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("text is required")
                result = agent.run(text)
                self._send(200, json.dumps(result, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(),
                           "application/json")

    return Handler


def serve(agent, port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(agent))
    print(f"TastePilot listening on http://127.0.0.1:{port} (mode={agent.mode})")
    server.serve_forever()
