"""Per-client POST rate limiting (public hosted-demo hardening): a
fixed-window limiter (tastepilot.ratelimit) gates every POST route
by remote IP, answers 429 {"error": "rate limited"} with Retry-After
plus the usual security headers / no-store when the quota is spent,
and never limits GETs or /api/health — which reports the limiter's
configuration, in the same style as the share-store info."""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.ratelimit import RateLimiter
from tastepilot.server import SECURITY_HEADERS, make_handler

APP_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = APP_ROOT / "samples" / "plan_weekend_kyoto.json"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


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


def _post_from_ip(base, path, obj, source_ip):
    parts = base.split("://", 1)[1]
    host, port = parts.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=5,
                                      source_address=(source_ip, 0))
    try:
        conn.request("POST", path, body=json.dumps(obj).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# --- the limiter itself ---------------------------------------------------

def test_limiter_allows_under_limit_then_refuses_with_retry_after():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    assert limiter.check("203.0.113.7") is None
    assert limiter.check("203.0.113.7") is None
    assert limiter.check("203.0.113.7") is None
    assert limiter.check("203.0.113.7") == 60
    clock.advance(30)
    # A refused attempt neither consumes quota nor extends the window.
    assert limiter.check("203.0.113.7") == 30
    with pytest.raises(ValueError):
        RateLimiter(limit=0)
    with pytest.raises(ValueError):
        RateLimiter(window_seconds=0)
    with pytest.raises(ValueError):
        RateLimiter(max_clients=0)


def test_limiter_other_client_unaffected():
    limiter = RateLimiter(limit=1, window_seconds=60, clock=FakeClock())
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is not None
    assert limiter.check("198.51.100.2") is None


def test_limiter_window_recovers_after_time_passes():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    assert limiter.check("192.0.2.9") is None
    assert limiter.check("192.0.2.9") is None
    assert limiter.check("192.0.2.9") is not None
    clock.advance(61)
    # The full quota is back once the window has rolled over.
    assert limiter.check("192.0.2.9") is None
    assert limiter.check("192.0.2.9") is None
    assert limiter.check("192.0.2.9") is not None


def test_limiter_state_is_bounded_and_evicts_oldest():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, max_clients=2,
                          clock=clock)
    assert limiter.check("client-a") is None
    assert limiter.check("client-b") is None
    assert limiter.check("client-c") is None  # evicts client-a
    assert limiter.info()["clients"] == 2
    assert limiter.info()["max_clients"] == 2
    # client-a was evicted, so its quota starts fresh (and its return
    # evicts the now-least-recently-seen client-b, never growing past 2).
    assert limiter.check("client-a") is None
    assert limiter.info()["clients"] == 2
    clock.advance(3600)
    assert limiter.info()["clients"] == 0  # expired windows drop out


# --- the HTTP contract ------------------------------------------------------

def test_http_over_limit_is_429_with_retry_after_and_hardening_headers():
    clock = FakeClock()
    server, base = _serve(make_handler(
        TastePilotAgent(), rate_limit=3, rate_window_seconds=60,
        rate_clock=clock))
    try:
        for _ in range(3):
            status, _, _ = _post_json(base + "/api/verify", _sample())
            assert status == 200
        status, headers, raw = _post_json(base + "/api/verify",
                                          _sample())
        assert status == 429
        assert json.loads(raw) == {"error": "rate limited"}
        retry = int(headers["Retry-After"])
        assert 1 <= retry <= 60
        for name, value in SECURITY_HEADERS.items():
            assert headers.get(name) == value
        assert headers.get("Cache-Control") == "no-store"
        # The quota is per client across POST routes, not per route:
        # the agent route is limited by the same spent window.
        status, _, raw = _post_json(base + "/api/plan",
                                    {"text": "a weekend in Kyoto"})
        assert status == 429
        assert json.loads(raw)["error"] == "rate limited"
    finally:
        server.shutdown()


def test_http_different_client_ip_is_unaffected():
    server, base = _serve(make_handler(
        TastePilotAgent(), rate_limit=1, rate_window_seconds=60,
        rate_clock=FakeClock()))
    try:
        status, _, _ = _post_from_ip(base, "/api/verify", _sample(),
                                     "127.0.0.1")
        assert status == 200
        status, _, _ = _post_from_ip(base, "/api/verify", _sample(),
                                     "127.0.0.1")
        assert status == 429
        status, _, _ = _post_from_ip(base, "/api/verify", _sample(),
                                     "127.0.0.2")
        assert status == 200
    finally:
        server.shutdown()


def test_http_window_recovers_after_time_passes():
    clock = FakeClock()
    server, base = _serve(make_handler(
        TastePilotAgent(), rate_limit=1, rate_window_seconds=60,
        rate_clock=clock))
    try:
        status, _, _ = _post_json(base + "/api/share", _sample())
        assert status == 201
        status, _, _ = _post_json(base + "/api/share", _sample())
        assert status == 429
        clock.advance(61)
        status, _, _ = _post_json(base + "/api/share", _sample())
        assert status == 201
    finally:
        server.shutdown()


def test_get_and_health_are_never_limited_and_health_reports_config():
    server, base = _serve(make_handler(
        TastePilotAgent(), rate_limit=1, rate_window_seconds=60,
        rate_clock=FakeClock()))
    try:
        status, _, _ = _post_json(base + "/api/bundle", _sample())
        assert status == 200
        status, _, _ = _post_json(base + "/api/bundle", _sample())
        assert status == 429
        for _ in range(3):
            status, _, raw = _request(base + "/api/health")
            assert status == 200
            health = json.loads(raw)
            assert health["rate_limit"]["enabled"] is True
            assert health["rate_limit"]["limit"] == 1
            assert health["rate_limit"]["window_seconds"] == 60.0
            assert health["rate_limit"]["clients"] == 1
            status, _, _ = _request(base + "/")
            assert status == 200
    finally:
        server.shutdown()


def test_make_handler_rate_limit_none_disables_limiting():
    server, base = _serve(make_handler(TastePilotAgent(),
                                       rate_limit=None))
    try:
        for _ in range(5):
            status, _, _ = _post_json(base + "/api/verify", _sample())
            assert status == 200
        status, _, raw = _request(base + "/api/health")
        assert status == 200
        assert json.loads(raw)["rate_limit"]["enabled"] is False
    finally:
        server.shutdown()


# --- configuration surface --------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["serve", "--rate-limit", "0"],
    ["serve", "--rate-limit", "-5"],
    ["serve", "--rate-window-seconds", "0"],
    ["serve", "--rate-window-seconds", "-1"],
])
def test_cli_invalid_rate_values_exit_two(capsys, argv):
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "must be > 0" in err
    assert "Traceback" not in err


def test_cli_invalid_env_rate_limit_exits_two(capsys, monkeypatch):
    monkeypatch.setenv("TASTEPILOT_RATE_LIMIT", "not-a-number")
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "TASTEPILOT_RATE_LIMIT" in err
    assert "Traceback" not in err


def test_cli_serve_help_offers_rate_flags():
    out = subprocess.run([sys.executable, "-m", "tastepilot", "serve",
                          "--help"], capture_output=True, text=True,
                         cwd=APP_ROOT)
    assert out.returncode == 0
    assert "--rate-limit" in out.stdout
    assert "--rate-window-seconds" in out.stdout


def test_serve_env_var_sets_limit_and_startup_line_names_it():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, TASTEPILOT_RATE_LIMIT="7")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tastepilot", "serve",
         "--port", str(port)],
        cwd=APP_ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    try:
        health = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                status, _, raw = _request(
                    f"http://127.0.0.1:{port}/api/health")
                if status == 200:
                    health = json.loads(raw)
                    break
            except OSError:
                time.sleep(0.1)
        assert health is not None, "server did not come up"
        assert health["rate_limit"]["enabled"] is True
        assert health["rate_limit"]["limit"] == 7
        assert health["rate_limit"]["window_seconds"] == 60.0
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    assert "rate_limit=7/60s" in out


# --- docs / UI wiring ---------------------------------------------------------

def test_docs_describe_rate_limiting():
    readme = (APP_ROOT / "README.md").read_text(encoding="utf-8")
    assert "rate" in readme.lower()
    assert "429" in readme and "Retry-After" in readme
    assert "TASTEPILOT_RATE_LIMIT" in readme
    assert "--rate-limit" in readme
    submission = (APP_ROOT / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "rate limit" in submission.lower()
    assert "429" in submission
    deploy = (APP_ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "TASTEPILOT_RATE_LIMIT" in deploy
    assert "45. Rate limit probe:" in deploy
    assert "429" in deploy and "Retry-After" in deploy
