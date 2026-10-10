"""Real-client resilience: transient-failure retries, network-error
normalisation, and payload-envelope broadening.

The live Qloo API is a hackathon endpoint: when the key lands, the first
real run must survive a 429 or a dropped connection instead of crashing
the smoke check with a raw urllib exception, and must parse the payload
envelopes the API actually uses — not just the one flat shape the first
parser assumed. Everything here runs against fake transports with a
recording sleeper: no network, no real delays, no key material (the
sentinel key is asserted absent from every error surface).
"""
import urllib.error

import pytest

from tastepilot import cli
from tastepilot.qloo import QlooError, RealQlooClient
from tastepilot.smoke import run_smoke

SENTINEL_KEY = "sentinel-key-must-never-appear"
OK_PAYLOAD = {"results": [{"entity_id": "x1", "name": "Test Place",
                           "type": "urn:entity:place", "affinity": 0.42}]}


class Recorder:
    """Sleeper double: records requested backoff delays, never sleeps."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def scripted_transport(script):
    """Transport replaying (status, payload) steps or raising exceptions."""
    calls = []

    def transport(method, url, headers):
        calls.append((method, url, headers))
        step = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step

    return transport, calls


def make_client(script, **kwargs):
    transport, calls = scripted_transport(script)
    sleeper = Recorder()
    client = RealQlooClient(SENTINEL_KEY, transport=transport,
                            sleeper=sleeper, **kwargs)
    return client, calls, sleeper


# --- retries ---------------------------------------------------------------

def test_transient_429_retried_then_succeeds():
    client, calls, sleeper = make_client([(429, {}), (200, OK_PAYLOAD)])
    resp = client.search("jazz")
    assert resp.results[0].entity.id == "x1"
    assert len(calls) == 2
    assert sleeper.delays == [0.5]


def test_transient_503_exhausts_attempts_and_raises_qloo_error():
    client, calls, sleeper = make_client([(503, {})], max_attempts=3)
    with pytest.raises(QlooError) as err:
        client.search("jazz")
    assert "503" in str(err.value)
    assert len(calls) == 3
    assert sleeper.delays == [0.5, 1.0]


def test_auth_error_fails_immediately_without_retry():
    client, calls, sleeper = make_client([(401, {}), (200, OK_PAYLOAD)])
    with pytest.raises(QlooError):
        client.search("jazz")
    assert len(calls) == 1
    assert sleeper.delays == []


def test_network_error_retried_then_succeeds():
    boom = urllib.error.URLError("connection reset")
    client, calls, sleeper = make_client([boom, (200, OK_PAYLOAD)])
    resp = client.search("jazz")
    assert resp.results[0].affinity == 0.42
    assert len(calls) == 2
    assert sleeper.delays == [0.5]


def test_network_error_exhausted_raises_qloo_error_not_urlerror():
    boom = urllib.error.URLError("timed out")
    client, calls, _ = make_client([boom], max_attempts=2)
    with pytest.raises(QlooError) as err:
        client.insights(["s1"], "place")
    assert len(calls) == 2
    assert SENTINEL_KEY not in str(err.value)


def test_error_messages_never_contain_the_key():
    client, _, _ = make_client([(500, {})], max_attempts=1)
    with pytest.raises(QlooError) as err:
        client.search("jazz")
    assert SENTINEL_KEY not in str(err.value)
    assert SENTINEL_KEY not in repr(err.value)


def test_smoke_reports_api_error_after_retries_not_a_crash():
    boom = urllib.error.URLError("dns failure")
    client, _, _ = make_client([boom], max_attempts=2)
    report = run_smoke(client)
    assert report["passed"] is False
    assert report["steps"][0]["reason"] == "api_error"
    assert report["steps"][1]["reason"] == "skipped_search_failed"
    assert report["evidence"]["chain_valid"] is True


# --- payload envelopes -----------------------------------------------------

def parse(payload, hint=None):
    return RealQlooClient._parse_entities(payload, hint)


def test_results_object_wrapping_entities():
    payload = {"results": {"entities": [
        {"entity_id": "b1", "name": "Wrapped Book",
         "type": "urn:entity:book", "affinity": 0.7}]}}
    rows = parse(payload)
    assert [(r.entity.id, r.entity.domain, r.affinity) for r in rows] == [
        ("b1", "book", 0.7)]


def test_top_level_entities_and_data_envelopes():
    ent = {"entity_id": "e1", "name": "Flat", "type": "urn:entity:brand",
           "affinity": 0.3}
    assert parse({"entities": [ent]})[0].entity.domain == "brand"
    assert parse({"data": [ent]})[0].entity.id == "e1"


def test_nested_entity_object_with_top_level_affinity():
    payload = {"results": [
        {"entity": {"entity_id": "n1", "name": "Nested Artist",
                    "type": "urn:entity:artist"},
         "affinity": 0.88}]}
    rows = parse(payload)
    assert rows[0].entity.name == "Nested Artist"
    assert rows[0].entity.domain == "music"
    assert rows[0].affinity == 0.88


def test_properties_fallbacks_for_name_tags_location():
    payload = {"results": [
        {"entity_id": "p1", "type": "urn:entity:place", "affinity": 0.5,
         "properties": {"name": "Property Place", "tags": ["quiet"],
                        "location": "Kyoto"}}]}
    row = parse(payload)[0]
    assert row.entity.name == "Property Place"
    assert row.entity.tags == ("quiet",)
    assert row.entity.location == "Kyoto"


def test_affinity_coercion_never_crashes():
    payload = {"results": [
        {"entity_id": "a", "name": "Stringy", "affinity": "0.66"},
        {"entity_id": "b", "name": "ScoreFallback", "affinity": None,
         "score": "0.3"},
        {"entity_id": "c", "name": "Garbage", "affinity": "n/a"},
        {"entity_id": "d", "name": "Missing"},
    ]}
    affinities = [r.affinity for r in parse(payload)]
    assert affinities == [0.66, 0.3, 0.5, 0.5]


def test_non_dict_payload_and_rows_are_ignored():
    assert parse({"results": "not-a-list"}) == []
    assert parse({"results": ["junk", 42]}) == []
    assert parse(None) == []


# --- CLI contract ----------------------------------------------------------

class BoomClient:
    source = "real"

    def search(self, query, limit=10):
        raise QlooError("Qloo API HTTP 503 on /search")

    def insights(self, seed_ids, target_domain, limit=5, location=None):
        raise QlooError("Qloo API HTTP 503 on /v2/insights")


def test_cli_plan_api_failure_exits_two_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_client", lambda mode: BoomClient())
    code = cli.main(["plan", "a weekend in Kyoto", "--mode", "real"])
    assert code == 2
    captured = capsys.readouterr()
    assert "503" in captured.err
    assert "Traceback" not in captured.err + captured.out
