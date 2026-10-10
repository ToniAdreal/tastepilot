"""Real-mode smoke check: search -> insights, shape validation, sealed
evidence. The smoke path itself must run zero-key against the mock
client and against a fake-transport real client, and its report must
never contain the API key."""
import json

import pytest

from tastepilot import cli
from tastepilot.evidence import verify_chain
from tastepilot.qloo import MockQlooClient, RealQlooClient
from tastepilot.smoke import run_smoke

SENTINEL_KEY = "sentinel-key-must-never-appear"


def transport_for(search_payload, insights_payload, status=200):
    def transport(method, url, headers):
        assert headers["X-Api-Key"] == SENTINEL_KEY
        if "/v2/insights" in url:
            return status, insights_payload
        return status, search_payload
    return transport


SEARCH_OK = {"results": [{"entity_id": "film-1", "name": "Spirited Away",
                          "type": "urn:entity:movie", "affinity": 0.9}]}
INSIGHTS_OK = {"results": [{"entity_id": "artist-1", "name": "Nujabes",
                            "type": "urn:entity:artist", "affinity": 0.91}]}


def test_smoke_mock_passes_and_chain_valid():
    report = run_smoke(MockQlooClient())
    assert report["passed"] is True
    assert report["source"] == "mock"
    assert [s["name"] for s in report["steps"]] == ["search", "insights"]
    assert all(s["ok"] for s in report["steps"])
    assert report["evidence"]["sealed_responses"] == 2
    assert report["evidence"]["chain_valid"] is True
    assert verify_chain(report["evidence_record"]) is True


def test_smoke_mock_shape_diagnostics():
    report = run_smoke(MockQlooClient())
    search_shape = report["steps"][0]["shape"]
    assert search_shape["result_count"] >= 1
    assert search_shape["ids_present"] and search_shape["names_present"]
    assert search_shape["affinity_min"] is not None
    insights_step = report["steps"][1]
    assert insights_step["seed_ids"]  # seeded from the search results
    assert insights_step["shape"]["domains"] == ["music"]


def test_smoke_real_fake_transport_passes_without_key_leak():
    client = RealQlooClient(SENTINEL_KEY,
                            transport=transport_for(SEARCH_OK, INSIGHTS_OK))
    report = run_smoke(client)
    assert report["passed"] is True
    assert report["source"] == "real"
    assert SENTINEL_KEY not in json.dumps(report)
    assert SENTINEL_KEY not in repr(report)


def test_smoke_real_http_error_fails_search_and_skips_insights():
    client = RealQlooClient(
        SENTINEL_KEY, transport=transport_for({}, {}, status=401))
    report = run_smoke(client)
    assert report["passed"] is False
    assert report["steps"][0]["reason"] == "api_error"
    assert report["steps"][1]["reason"] == "skipped_search_failed"
    # The failed run is still sealed, and its chain still verifies.
    assert report["evidence"]["sealed_responses"] == 0
    assert verify_chain(report["evidence_record"]) is True


def test_smoke_real_empty_search_results_fail():
    client = RealQlooClient(
        SENTINEL_KEY, transport=transport_for({"results": []}, INSIGHTS_OK))
    report = run_smoke(client)
    assert report["passed"] is False
    assert report["steps"][0]["reason"] == "empty_results"
    assert report["steps"][1]["reason"] == "skipped_search_failed"


def test_smoke_real_missing_entity_id_fails_shape():
    bad = {"results": [{"name": "", "type": "urn:entity:movie"}]}
    client = RealQlooClient(
        SENTINEL_KEY, transport=transport_for(bad, INSIGHTS_OK))
    report = run_smoke(client)
    assert report["passed"] is False
    assert report["steps"][0]["ok"] is False
    assert report["steps"][0]["reason"] in (
        "missing_entity_id", "missing_entity_name")


def test_smoke_real_empty_insights_fail():
    client = RealQlooClient(
        SENTINEL_KEY, transport=transport_for(SEARCH_OK, {"results": []}))
    report = run_smoke(client)
    assert report["passed"] is False
    assert report["steps"][0]["ok"] is True
    assert report["steps"][1]["reason"] == "empty_results"
    assert report["evidence"]["sealed_responses"] == 2


def test_cli_smoke_mock_exits_zero(capsys):
    assert cli.main(["smoke", "--mode", "mock"]) == 0
    out = capsys.readouterr().out
    assert "passed=True" in out and "chain_valid=True" in out


def test_cli_smoke_real_without_key_exits_two(monkeypatch):
    monkeypatch.delenv("QLOO_API_KEY", raising=False)
    monkeypatch.setattr("tastepilot.qloo.load_env_key",
                        lambda env_path=None: None)
    assert cli.main(["smoke", "--mode", "real"]) == 2


def test_cli_smoke_out_writes_report(tmp_path):
    out = tmp_path / "smoke.json"
    assert cli.main(["smoke", "--mode", "mock", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["passed"] is True
    assert verify_chain(data["evidence_record"]) is True


def test_cli_smoke_help_offers_domain_and_query():
    with pytest.raises(SystemExit) as exc:
        cli.main(["smoke", "--help"])
    assert exc.value.code == 0
