from tastepilot.agent import TastePilotAgent
from tastepilot.checker import check_plan


def _plan():
    return TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue")


def test_agent_plan_passes_checker():
    result = _plan()
    assert result["checker"]["passed"] is True
    assert result["checker"]["chain_valid"] is True
    assert result["items"], "plan must not be empty"


def test_hallucinated_item_is_rejected():
    result = _plan()
    fake = {"entity": {"id": "invented", "name": "Invented Venue",
                       "domain": "place"},
            "affinity": 0.99, "evidence_ref": "mock:insights:deadbeefdeadbeef"}
    report = check_plan(result["items"] + [fake], result["evidence"])
    assert report["passed"] is False
    assert any(v["item"] == "Invented Venue" for v in report["violations"])


def test_forged_affinity_is_flagged():
    result = _plan()
    item = dict(result["items"][0])
    item["affinity"] = 0.01
    report = check_plan([item], result["evidence"])
    assert report["passed"] is False
    assert any(v["reason"] == "affinity_mismatch" for v in report["violations"])


def test_search_ref_is_not_valid_recommendation_evidence():
    result = _plan()
    item = dict(result["items"][0])
    search_ref = next(e["response"]["response_id"]
                      for e in result["evidence"]["entries"]
                      if e["kind"] == "qloo_call"
                      and e["response"]["endpoint"] == "search")
    item["evidence_ref"] = search_ref
    report = check_plan([item], result["evidence"])
    assert any(v["reason"] in ("evidence_ref_not_insights", "entity_not_in_qloo_response")
               for v in report["violations"])
