"""Oct 17-20 milestone (mock-side half): Nebius explanations live in the
demo path — `serve --llm nebius`, per-item explanation provenance in the
plan, plan-level explainer summary, and health reporting. All Nebius
calls here use a fake CLI runner; the live connector is smoked separately.
"""
import json
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.cli import _agent_for
from tastepilot.evidence import verify_chain
from tastepilot.llm import DEFAULT_MODEL, NebiusExplainer, TemplateExplainer
from tastepilot.server import make_handler

APP_DIR = Path(__file__).resolve().parent.parent
KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"


def _ok_runner(argv):
    assert "nebius_tf.py" in " ".join(argv)
    return 0, json.dumps({"status": 200, "content": "A live-model why line.",
                          "usage": {"total_tokens": 9}})


def _fail_runner(argv):
    return 1, '{"status": 500, "error": "boom"}'


def _neb_agent(runner):
    return TastePilotAgent(explainer=NebiusExplainer(runner=runner))


def test_agent_nebius_items_carry_full_provenance():
    result = _neb_agent(_ok_runner).run(KYOTO)
    assert result["checker"]["passed"] and result["items"]
    for item in result["items"]:
        ex = item["explanation"]
        assert ex["source"] == "nebius"
        assert ex["model"] == DEFAULT_MODEL
        assert ex["usage"]["total_tokens"] == 9
        assert item["explanation_source"] == "nebius"  # back-compat field
        assert item["why"] == "A live-model why line."
    summary = result["explainer"]
    assert summary["source"] == "nebius" and summary["model"] == DEFAULT_MODEL
    assert summary["items_explained"] == len(result["items"])
    assert summary["llm_written"] == len(result["items"])
    assert summary["template_written"] == 0 and summary["fallback"] == 0


def test_agent_nebius_calls_are_sealed_in_evidence():
    result = _neb_agent(_ok_runner).run(KYOTO)
    llm_entries = [e for e in result["evidence"]["entries"]
                   if e["kind"] == "llm_call"]
    # Alternatives are explained (and sealed) too — see test_alternatives.
    assert len(llm_entries) == len(result["items"]) + \
        len(result.get("alternatives", []))
    assert all(e["call"]["purpose"] == "explain" for e in llm_entries)
    assert all(e["call"]["item"] for e in llm_entries)
    assert verify_chain(result["evidence"]) is True


def test_nebius_empty_content_falls_back_and_records_it():
    """Live-observed failure mode (2026-10-10): Nemotron spent the whole
    token budget reasoning (finish_reason=length), so the connector
    returned status 200 with content=null. That must be a recorded
    fallback, never an empty or invented why line."""
    def runner(argv):
        return 0, json.dumps({"status": 200, "content": None,
                              "finish_reason": "length"})
    ex = NebiusExplainer(runner=runner)
    text, meta = ex.explain(
        ["Spirited Away"],
        {"entity": {"name": "Gion, Kyoto", "domain": "place"},
         "affinity": 0.88, "shared_tags": ["quiet"]})
    assert "Gion, Kyoto" in text  # template wording
    assert meta["source"] == "template" and meta["fallback_from"] == "nebius"
    assert meta["fallback_reason"] == "RuntimeError"


def test_nebius_request_budget_fits_reasoning_plus_answer():
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        return 0, '{"status": 200, "content": "A why line.", "usage": {}}'

    NebiusExplainer(runner=runner).explain(
        ["Spirited Away"],
        {"entity": {"name": "Gion, Kyoto", "domain": "place"},
         "affinity": 0.88, "shared_tags": []})
    budget = int(seen["argv"][seen["argv"].index("--max-tokens") + 1])
    assert budget >= 1000  # 300 was live-proven to starve the answer


def test_agent_nebius_failure_falls_back_and_provenance_shows_it():
    result = _neb_agent(_fail_runner).run(KYOTO)
    assert result["checker"]["passed"] and result["items"]
    for item in result["items"]:
        ex = item["explanation"]
        assert ex["source"] == "template"
        assert ex["fallback_from"] == "nebius"
        assert ex["fallback_reason"]  # recorded, never hidden
        assert item["entity"]["name"] in item["why"]  # template wording
    summary = result["explainer"]
    assert summary["source"] == "nebius"  # the configured explainer
    assert summary["llm_written"] == 0
    assert summary["fallback"] == len(result["items"])
    assert summary["template_written"] == 0
    assert verify_chain(result["evidence"]) is True


def test_agent_template_default_provenance():
    result = TastePilotAgent().run(KYOTO)
    assert result["explainer"]["source"] == "template"
    assert result["explainer"]["model"] is None
    for item in result["items"]:
        assert item["explanation"] == {"source": "template"}
    assert result["explainer"]["template_written"] == len(result["items"])
    # pure-template runs seal no llm_call entries (unchanged behaviour)
    assert not [e for e in result["evidence"]["entries"]
                if e["kind"] == "llm_call"]


def test_nebius_explainer_subprocess_branch_uses_stored_timeout(monkeypatch):
    """Regression: __init__ accepted `timeout` but never stored it, so the
    real (runner=None) subprocess branch raised AttributeError and every
    live Nebius call silently fell back to the template. Fake-runner
    tests cannot see this — patch subprocess.run itself."""
    import tastepilot.llm as llm_mod

    seen = {}

    class Proc:
        returncode = 0
        stdout = json.dumps({"status": 200, "content": "Subprocess why line.",
                             "usage": {"total_tokens": 5}})

    def fake_run(argv, capture_output, text, timeout):
        seen["timeout"] = timeout
        seen["argv"] = argv
        return Proc()

    monkeypatch.setattr(llm_mod.subprocess, "run", fake_run)
    ex = NebiusExplainer(timeout=42)
    assert ex.timeout == 42
    text_out, meta = ex.explain(
        ["Spirited Away"],
        {"entity": {"name": "Gion, Kyoto", "domain": "place"},
         "affinity": 0.88, "shared_tags": ["quiet"]})
    assert text_out == "Subprocess why line."
    assert meta["source"] == "nebius"
    assert seen["timeout"] == 42


def test_cli_agent_for_selects_explainer():
    assert isinstance(_agent_for("mock", "nebius").explainer, NebiusExplainer)
    assert isinstance(_agent_for("mock", "template").explainer,
                      TemplateExplainer)


def test_cli_serve_help_offers_llm_flag():
    proc = subprocess.run(
        [sys.executable, "-m", "tastepilot", "serve", "--help"],
        cwd=APP_DIR, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "--llm" in proc.stdout and "nebius" in proc.stdout


@pytest.fixture()
def neb_server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(_neb_agent(_ok_runner)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _get_json(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def test_server_health_reports_nebius_explainer(neb_server_url):
    health = _get_json(neb_server_url + "/api/health")
    assert health["ok"] is True and health["mode"] == "mock"
    assert health["explainer"] == {"source": "nebius", "model": DEFAULT_MODEL}


def test_server_plan_surfaces_explanation_provenance(neb_server_url):
    req = urllib.request.Request(
        neb_server_url + "/api/plan", method="POST",
        data=json.dumps({"text": KYOTO}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        plan = json.loads(resp.read())
    assert plan["explainer"]["llm_written"] == len(plan["items"])
    assert all(i["explanation"]["source"] == "nebius" for i in plan["items"])
    # grouped timeline items carry the same provenance the cards render
    grouped = [i for g in plan["groups"] for i in g["items"]]
    assert all(i["explanation"]["model"] == DEFAULT_MODEL for i in grouped)


def test_ui_page_renders_explanation_provenance(neb_server_url):
    with urllib.request.urlopen(neb_server_url + "/") as resp:
        html = resp.read().decode()
    assert "item.explanation" in html
    assert "why by" in html
    assert "template fallback" in html
    assert "data.explainer" in html
