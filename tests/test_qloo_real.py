import pytest

from tastepilot.qloo import QlooError, RealQlooClient, load_env_key


def fake_transport_factory(payload):
    calls = []
    def transport(method, url, headers):
        calls.append((method, url, headers))
        return 200, payload
    return transport, calls


def test_real_client_sends_key_header_and_parses():
    payload = {"results": [{"entity_id": "x1", "name": "Test Place",
                            "type": "urn:entity:place", "affinity": 0.42}]}
    transport, calls = fake_transport_factory(payload)
    client = RealQlooClient("test-key-not-a-real-secret", transport=transport)
    resp = client.insights(["s1"], "place")
    assert calls[0][2]["X-Api-Key"] == "test-key-not-a-real-secret"
    assert "hackathon.api.qloo.com" in calls[0][1]
    assert resp.source == "real" and resp.results[0].affinity == 0.42
    assert "test-key-not-a-real-secret" not in repr(resp)


def test_real_client_requires_key():
    with pytest.raises(QlooError):
        RealQlooClient("")


def test_real_client_http_error_raises():
    client = RealQlooClient("k", transport=lambda m, u, h: (401, {}))
    with pytest.raises(QlooError):
        client.search("jazz")


def test_load_env_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("QLOO_API_KEY", raising=False)
    assert load_env_key(tmp_path / "nope.env") is None
