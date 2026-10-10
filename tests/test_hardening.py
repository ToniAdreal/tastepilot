"""Hardening + hosted-demo readiness (Oct 21–24 milestone): input caps,
security headers, no-leak error paths, a bounded evidence cache, and the
deployment surface (host flag, PORT env, deployment artifacts)."""
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import MAX_TEXT_CHARS, TastePilotAgent
from tastepilot.server import (MAX_BODY_BYTES, SECURITY_HEADERS,
                               make_handler)

APP_ROOT = Path(__file__).resolve().parent.parent


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post_json(url, obj):
    return _request(url, method="POST",
                    body=json.dumps(obj).encode(),
                    headers={"Content-Type": "application/json"})


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# --- security headers -----------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/api/health", "/nope"])
def test_security_headers_on_every_response(server_url, path):
    _, headers, _ = _request(server_url + path)
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value


def test_api_responses_are_no_store(server_url):
    _, headers, _ = _request(server_url + "/api/health")
    assert headers.get("Cache-Control") == "no-store"


# --- input validation -----------------------------------------------------

def test_oversize_body_rejected_413(server_url):
    body = b'{"text": "' + b"x" * MAX_BODY_BYTES + b'"}'
    status, _, raw = _request(server_url + "/api/plan", method="POST",
                              body=body,
                              headers={"Content-Type": "application/json"})
    assert status == 413
    assert json.loads(raw)["error"] == "request body too large"


def test_malformed_json_rejected_400(server_url):
    status, _, raw = _request(server_url + "/api/plan", method="POST",
                              body=b"{not json",
                              headers={"Content-Type": "application/json"})
    assert status == 400
    assert json.loads(raw)["error"] == "invalid JSON body"


def test_non_object_and_missing_text_rejected_400(server_url):
    status, _, _ = _post_json(server_url + "/api/plan", ["not", "a", "dict"])
    assert status == 400
    status, _, raw = _post_json(server_url + "/api/plan", {"text": "  "})
    assert status == 400
    assert json.loads(raw)["error"] == "text is required"
    status, _, _ = _post_json(server_url + "/api/plan", {"text": 42})
    assert status == 400


def test_overlong_text_rejected_413(server_url):
    status, _, raw = _post_json(server_url + "/api/plan",
                                {"text": "x" * (MAX_TEXT_CHARS + 1)})
    assert status == 413
    assert json.loads(raw)["error"] == "text too long"


def test_agent_enforces_the_same_text_cap():
    agent = TastePilotAgent()
    with pytest.raises(ValueError):
        agent.run("x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(ValueError):
        agent.run("   ")


def test_unsupported_methods_get_405_json(server_url):
    for method in ("PUT", "PATCH", "DELETE"):
        status, _, raw = _request(server_url + "/api/plan", method=method)
        assert status == 405
        assert json.loads(raw)["error"] == "method not allowed"


def test_internal_error_is_generic_and_leak_free():
    class BoomAgent(TastePilotAgent):
        def run(self, text):
            raise RuntimeError("internal-secret-detail-123")

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(BoomAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/plan"
        status, _, raw = _post_json(url, {"text": "a weekend in Kyoto"})
        assert status == 500
        assert json.loads(raw)["error"] == "internal error"
        assert b"internal-secret-detail-123" not in raw
    finally:
        server.shutdown()


# --- bounded evidence cache ------------------------------------------------

def test_evidence_cache_evicts_oldest_first():
    handler = make_handler(TastePilotAgent(), max_evidence_records=4)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, _, raw = _post_json(base + "/api/plan",
                               {"text": "a slow weekend in Kyoto, "
                                        "I love Spirited Away"})
        plan = json.loads(raw)
        refs = [item["evidence_ref"] for item in plan["items"]]
        assert refs, "plan must carry evidence refs"
        # Each plan seals more Qloo responses (searches + one insights
        # call per domain) than the cap of 4, and a second plan pushes
        # more through: the oldest plan's first ref must be evicted.
        _post_json(base + "/api/plan",
                   {"text": "a first date in NYC, we love Kind of Blue"})
        status, _, _ = _request(base + "/api/evidence/" +
                                urllib.parse.quote(refs[0], safe=""))
        assert status == 404
        # …while the newest plan's last ref is still served.
        _, _, raw2 = _post_json(base + "/api/plan",
                                {"text": "a birthday gift shortlist, "
                                         "she loves Amelie"})
        last_ref = json.loads(raw2)["items"][-1]["evidence_ref"]
        status, _, _ = _request(base + "/api/evidence/" +
                                urllib.parse.quote(last_ref, safe=""))
        assert status == 200
    finally:
        server.shutdown()


# --- deployment surface ------------------------------------------------------

def test_cli_serve_help_offers_host_flag():
    out = subprocess.run([sys.executable, "-m", "tastepilot", "serve",
                          "--help"], capture_output=True, text=True,
                         cwd=APP_ROOT)
    assert out.returncode == 0
    assert "--host" in out.stdout
    assert "--port" in out.stdout


def test_deployment_artifacts_present_and_safe():
    dockerfile = (APP_ROOT / "Dockerfile").read_text()
    assert "python:3.12-slim" in dockerfile
    assert "USER tastepilot" in dockerfile          # non-root
    assert "HEALTHCHECK" in dockerfile
    assert "--host\", \"0.0.0.0" in dockerfile or "--host 0.0.0.0" in dockerfile
    assert "QLOO_API_KEY=" not in dockerfile         # key never baked in
    dockerignore = (APP_ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in dockerignore                    # nor copied in
    procfile = (APP_ROOT / "Procfile").read_text()
    assert "serve --host 0.0.0.0" in procfile
    deploy = (APP_ROOT / "DEPLOY.md").read_text()
    assert "PORT" in deploy and "QLOO_API_KEY" in deploy


def test_normal_plan_still_works_after_hardening(server_url):
    status, _, raw = _post_json(server_url + "/api/plan",
                                {"text": "a slow weekend in Kyoto, "
                                         "I love Spirited Away"})
    assert status == 200
    plan = json.loads(raw)
    assert plan["items"] and plan["checker"]["passed"] is True
    assert plan["checker"]["chain_valid"] is True
