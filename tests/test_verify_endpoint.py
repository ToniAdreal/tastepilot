"""Browser plan verification (POST /api/verify): anyone holding a shared
plan JSON can re-check its evidence chain and per-item checker verdicts.
The submitted record is untrusted input — verdicts are always recomputed,
malformed shapes are 400s (never 500s), and the body stays size-capped."""
import copy
import http.client
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.evidence import verify_chain
from tastepilot.server import MAX_VERIFY_BODY_BYTES, SECURITY_HEADERS, make_handler

APP_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = APP_ROOT / "samples" / "plan_weekend_kyoto.json"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post_raw(url, raw: bytes):
    return _request(url, method="POST", body=raw,
                    headers={"Content-Type": "application/json"})


def _post_json(url, obj):
    return _post_raw(url, json.dumps(obj).encode())


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def sample_plan():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# --- the honest path --------------------------------------------------------

def test_shipped_sample_plan_verifies(server_url, sample_plan):
    status, headers, raw = _post_json(server_url + "/api/verify", sample_plan)
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value
    data = json.loads(raw)
    assert data["passed"] is True and data["chain_valid"] is True
    assert data["item_count"] == len(sample_plan["items"]) > 0
    assert data["sealed_responses"] > 0
    assert data["violations"] == []
    assert len(data["items"]) == data["item_count"]
    assert all(r["admissible"] and r["reason"] == "ok"
               for r in data["items"])
    # The record's own verdict is echoed as a claim, not used as truth.
    assert data["record_verdict"] == sample_plan["evidence"]["verdict"]


def test_fresh_server_plan_round_trips_through_verify(server_url):
    status, _, raw = _post_json(server_url + "/api/plan",
                                {"text": "a first date in NYC, "
                                         "we love Kind of Blue"})
    assert status == 200
    plan = json.loads(raw)
    status, _, raw = _post_json(server_url + "/api/verify", plan)
    assert status == 200
    data = json.loads(raw)
    assert data["passed"] is True and data["chain_valid"] is True
    assert data["item_count"] == len(plan["items"])


def test_bare_evidence_record_verifies_its_chain(server_url, sample_plan):
    status, _, raw = _post_json(server_url + "/api/verify",
                                sample_plan["evidence"])
    assert status == 200
    data = json.loads(raw)
    assert data["chain_valid"] is True and data["item_count"] == 0


# --- tampering is caught by recomputation, not by trusting the record -----

def test_tampered_item_affinity_fails_with_reason(server_url, sample_plan):
    plan = copy.deepcopy(sample_plan)
    plan["items"][0]["affinity"] += 0.01
    status, _, raw = _post_json(server_url + "/api/verify", plan)
    assert status == 200
    data = json.loads(raw)
    assert data["passed"] is False and data["chain_valid"] is True
    bad = [r for r in data["items"] if not r["admissible"]]
    assert len(bad) == 1 and bad[0]["reason"] == "affinity_mismatch"
    assert {"item": bad[0]["item"], "reason": "affinity_mismatch"} \
        in data["violations"]


def test_tampered_sealed_response_breaks_the_chain(server_url, sample_plan):
    plan = copy.deepcopy(sample_plan)
    for entry in plan["evidence"]["entries"]:
        if entry.get("kind") == "qloo_call":
            entry["response"]["results"][0]["affinity"] = 0.01
            break
    status, _, raw = _post_json(server_url + "/api/verify", plan)
    assert status == 200
    data = json.loads(raw)
    assert data["chain_valid"] is False and data["passed"] is False
    assert {"item": None, "reason": "evidence_chain_invalid"} \
        in data["violations"]


def test_claimed_verdict_is_never_trusted(server_url, sample_plan):
    # A record that claims PASS in its verdict field but carries a
    # hallucinated item (no evidence ref at all) must still FAIL.
    plan = copy.deepcopy(sample_plan)
    plan["items"].append({
        "entity": {"id": "mov_invented", "name": "Invented Hallucination",
                   "domain": "film", "tags": [], "location": None},
        "domain": "film", "affinity": 0.99, "shared_tags": [],
        "evidence_ref": "resp_does_not_exist", "why": "trust me"})
    assert plan["evidence"]["verdict"] == "PASS"
    status, _, raw = _post_json(server_url + "/api/verify", plan)
    data = json.loads(raw)
    assert data["passed"] is False
    assert {"item": "Invented Hallucination",
            "reason": "missing_or_unknown_evidence_ref"} in data["violations"]


# --- untrusted-input handling: 400s and caps, never 500s ---------------------

def test_verify_malformed_json_and_non_object(server_url):
    status, _, raw = _post_raw(server_url + "/api/verify", b"{not json")
    assert status == 400
    assert json.loads(raw)["error"] == "invalid JSON body"
    status, _, _ = _post_json(server_url + "/api/verify", [1, 2, 3])
    assert status == 400


def test_verify_missing_or_malformed_evidence(server_url):
    status, _, raw = _post_json(server_url + "/api/verify", {"items": []})
    assert status == 400
    assert "evidence" in json.loads(raw)["error"]
    status, _, _ = _post_json(server_url + "/api/verify",
                              {"evidence": {"entries": "nope"}, "items": []})
    assert status == 400
    status, _, _ = _post_json(server_url + "/api/verify",
                              {"evidence": {"entries": []}, "items": "nope"})
    assert status == 400


def test_verify_garbage_entries_are_400_not_500(server_url):
    payload = {"evidence": {"entries": ["junk", 42, None],
                            "verdict": "PASS",
                            "record_hash": "x", "signature": "y"},
               "items": []}
    status, _, raw = _post_json(server_url + "/api/verify", payload)
    assert status in (200, 400)  # either way: a verdict, not a crash
    if status == 200:
        data = json.loads(raw)
        assert data["chain_valid"] is False and data["passed"] is False
    assert b"Traceback" not in raw


def test_verify_oversize_body_rejected_413(server_url):
    # Rejection happens on the declared Content-Length alone (the body is
    # never read), so declare an oversize length and send almost nothing:
    # streaming 512 KiB at a server that has already answered races the
    # connection close and flakes.
    host = server_url.removeprefix("http://")
    hostname, port = host.rsplit(":", 1)
    conn = http.client.HTTPConnection(hostname, int(port), timeout=10)
    try:
        conn.putrequest("POST", "/api/verify")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_VERIFY_BODY_BYTES + 1))
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        raw = resp.read()
        assert resp.status == 413
        assert json.loads(raw)["error"] == "plan too large"
    finally:
        conn.close()


# --- the hardening underneath, and the CLI contract it protects -------------

def test_verify_chain_tolerates_garbage_records():
    assert verify_chain({"entries": ["junk"]}) is False
    assert verify_chain({"entries": []}) is False
    assert verify_chain({}) is False
    assert verify_chain({"entries": [{"kind": "header"}]}) is False


def test_cli_verify_on_garbage_record_exits_1_without_traceback(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(
        {"evidence": {"entries": ["junk"], "verdict": "PASS",
                      "record_hash": "x", "signature": "y"},
         "items": []}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "tastepilot", "verify",
                          "--record", str(bad)],
                         capture_output=True, text=True, cwd=APP_ROOT)
    assert out.returncode == 1
    assert "Traceback" not in out.stderr


# --- UI wiring ----------------------------------------------------------------

def test_ui_page_carries_the_verify_panel(server_url):
    status, _, raw = _request(server_url + "/")
    assert status == 200
    page = raw.decode()
    assert 'id="verifybox"' in page and 'id="vtext"' in page
    assert "/api/verify" in page
