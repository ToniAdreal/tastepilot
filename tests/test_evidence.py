from tastepilot.evidence import EvidenceLog, verify_chain
from tastepilot.qloo import MockQlooClient


def _record():
    log = EvidenceLog("a weekend in Kyoto", "mock")
    resp = MockQlooClient().insights(["mov_spirited_away"], "place")
    log.add_qloo_call(resp.to_dict())
    return log.seal("PASS"), resp


def test_chain_verifies():
    record, _ = _record()
    assert verify_chain(record) is True


def test_tampered_affinity_breaks_chain():
    record, _ = _record()
    record["entries"][1]["response"]["results"][0]["affinity"] = 0.01
    assert verify_chain(record) is False


def test_tampered_verdict_breaks_chain():
    record, _ = _record()
    record["verdict"] = "FAIL"
    assert verify_chain(record) is False
