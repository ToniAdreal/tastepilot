from tastepilot.agent import TastePilotAgent
from tastepilot.evidence import verify_chain


def test_weekend_kyoto_end_to_end():
    result = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue")
    assert result["scenario"] == "weekend"
    assert result["location"] == "Kyoto"
    domains = {i["domain"] for i in result["items"]}
    assert len(domains) >= 3, domains
    assert all(i["evidence_ref"].startswith("mock:insights:") for i in result["items"])
    assert all(i["why"] for i in result["items"])
    assert verify_chain(result["evidence"])


def test_gift_scenario_uses_gift_domains():
    result = TastePilotAgent().run(
        "I need a birthday gift shortlist for someone who loves Amelie")
    assert result["scenario"] == "gift"
    assert {i["domain"] for i in result["items"]} <= {"brand", "book", "music", "film"}
    assert result["checker"]["passed"] is True


def test_date_scenario_in_nyc():
    result = TastePilotAgent().run(
        "a first date in New York, we both love Kind of Blue")
    assert result["scenario"] == "date"
    assert result["items"]
    assert result["checker"]["passed"] is True


def test_no_named_seeds_falls_back_to_defaults():
    result = TastePilotAgent().run("plan something nice for this weekend")
    assert result["seeds"], "default seeds should resolve"
    assert result["items"]


def test_deterministic_output():
    a = TastePilotAgent().run("a slow weekend in Kyoto, I love Spirited Away")
    b = TastePilotAgent().run("a slow weekend in Kyoto, I love Spirited Away")
    assert [i["entity"]["id"] for i in a["items"]] == \
           [i["entity"]["id"] for i in b["items"]]
