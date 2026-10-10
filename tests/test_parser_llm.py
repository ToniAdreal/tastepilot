from tastepilot.llm import NebiusExplainer, TemplateExplainer
from tastepilot.parser import parse_request

ITEM = {"entity": {"name": "Gion, Kyoto", "domain": "place"},
        "affinity": 0.88, "shared_tags": ["japanese", "quiet"]}


def test_parser_scenarios_and_location():
    p = parse_request("a first date in NYC with jazz")
    assert p.scenario == "date" and p.location == "New York"
    p2 = parse_request("a birthday gift for a Murakami fan")
    assert p2.scenario == "gift"


def test_parser_finds_named_seeds():
    p = parse_request("I love Spirited Away and Norwegian Wood")
    assert "Spirited Away" in p.seed_names
    assert "Norwegian Wood" in p.seed_names


def test_template_explainer_is_deterministic_and_grounded():
    ex = TemplateExplainer()
    a, meta = ex.explain(["Spirited Away"], ITEM)
    b, _ = ex.explain(["Spirited Away"], ITEM)
    assert a == b and "0.88" in a and "Gion, Kyoto" in a
    assert meta["source"] == "template"


def test_nebius_explainer_uses_cli_output():
    def runner(argv):
        assert "nebius_tf.py" in " ".join(argv)
        return 0, '{"status": 200, "content": "A quiet match for your taste.", "usage": {"total_tokens": 12}}'
    ex = NebiusExplainer(runner=runner)
    text, meta = ex.explain(["Spirited Away"], ITEM)
    assert text.startswith("A quiet match")
    assert meta["source"] == "nebius" and meta["usage"]["total_tokens"] == 12


def test_nebius_explainer_falls_back_and_records_it():
    def runner(argv):
        return 1, '{"status": 500, "error": "boom"}'
    ex = NebiusExplainer(runner=runner)
    text, meta = ex.explain(["Spirited Away"], ITEM)
    assert "Gion, Kyoto" in text  # template wording
    assert meta["source"] == "template" and meta["fallback_from"] == "nebius"
