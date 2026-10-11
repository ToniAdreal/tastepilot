"""Negative preferences (exclusions): a short clause behind a cue
word ("no jazz", "avoid ramen and wine", "allergic to shellfish")
is captured deterministically and applied by the agent as a filter
AFTER the evidence checker — an excluded item is evidence-backed
but unwanted, never a rejected hallucination. Exclusions are never
sent to Qloo: no Qloo call shape changes, and an excluded item keeps
its passing verification report."""
import json
import re
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.export import render_markdown, render_text
from tastepilot.parser import exclusion_match, extract_exclusions, parse_request
from tastepilot.server import make_handler

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away"


def test_cue_forms_and_clause_splitting():
    assert extract_exclusions(KYOTO + ", but no jazz") == ["jazz"]
    assert extract_exclusions(
        "a first date in New York — avoid ramen and wine please"
    ) == ["ramen", "wine"]
    assert extract_exclusions("no books, no jazz") == ["books", "jazz"]
    assert extract_exclusions(
        "dinner in Kyoto, allergic to shellfish, skip kaiseki"
    ) == ["shellfish", "kaiseki"]
    assert extract_exclusions(
        "a weekend in Kyoto without museums") == ["museums"]


def test_prose_containing_cue_words_is_not_an_exclusion():
    assert extract_exclusions(KYOTO + " and Kind of Blue") == []
    # "don't want to miss X" wants X.
    assert extract_exclusions(
        "a weekend in Kyoto, I don't want to miss the jazz bars") == []
    # A long clause after a cue word is prose, not a preference list.
    assert extract_exclusions(
        "never been to Kyoto, I love Spirited Away") == []
    # An unmatched short phrase is recorded but removes nothing.
    assert extract_exclusions(
        "a night with no plans, I love Amelie") == ["plans"]


def test_cue_inside_a_labelled_seed_list_is_ignored():
    text = "Watchlist: No Country for Old Men, Spirited Away. " + KYOTO
    assert extract_exclusions(text) == []


def test_exclusion_match_rules():
    item = {"domain": "food",
            "entity": {"name": "A seasonal kaiseki tasting menu",
                       "tags": ["kyoto", "japanese", "seasonal"]}}
    assert exclusion_match(item, ["kaiseki"]) == "kaiseki"      # name word
    assert exclusion_match(item, ["seasonal"]) == "seasonal"    # tag
    assert exclusion_match(item, ["food"]) == "food"            # domain
    assert exclusion_match(item, ["foods"]) == "foods"          # singular
    assert exclusion_match(item, ["shellfish"]) is None
    book = {"domain": "book",
            "entity": {"name": "The Little Prince",
                       "tags": ["french", "whimsical"]}}
    assert exclusion_match(book, ["books"]) == "books"
    assert exclusion_match(book, ["the little prince"]) == \
        "the little prince"                                     # full name


def test_agent_excludes_by_tag_and_keeps_verification():
    plan = TastePilotAgent().run(KYOTO + ", but no jazz")
    names = [i["entity"]["name"] for i in plan["items"]]
    assert "Nujabes" not in names
    assert plan["exclusions"] == ["jazz"]
    assert plan["excluded"] == [{
        "name": "Nujabes", "domain": "music", "location": "Kyoto",
        "matched": "jazz", "reason": "excluded_by_request",
        "verification": "ok"}]
    # Excluded is not rejected, and the plan still passes cleanly.
    assert plan["rejected"] == [] and plan["rejected_details"] == []
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    # The excluded item is gone from groups/legs/handoffs too.
    grouped = [i["entity"]["name"] for g in plan["groups"]
               for i in g["items"]]
    assert "Nujabes" not in grouped
    handed = [i["name"] for i in plan["handoffs"]["items"]]
    assert "Nujabes" not in handed


def test_agent_excludes_by_domain_and_by_name_word():
    gift = TastePilotAgent().run(
        "I need a birthday gift shortlist for someone who loves "
        "Amelie, no books")
    assert all(i["domain"] != "book" for i in gift["items"])
    assert [e["name"] for e in gift["excluded"]] == ["The Little Prince"]
    plan = TastePilotAgent().run(KYOTO + ", skip the ramen counter")
    assert any(e["matched"] == "the ramen counter" or
               e["name"] == "A quiet ramen counter in Gion"
               for e in plan["excluded"])


def test_unmatched_exclusion_removes_nothing():
    base = TastePilotAgent().run(KYOTO)
    plan = TastePilotAgent().run(KYOTO + ", no plans")
    assert plan["exclusions"] == ["plans"]
    assert plan["excluded"] == []
    assert [i["entity"]["name"] for i in plan["items"]] == \
        [i["entity"]["name"] for i in base["items"]]


def test_no_exclusion_fields_are_empty_and_deterministic():
    p1 = TastePilotAgent().run(KYOTO)
    p2 = TastePilotAgent().run(KYOTO)
    assert p1["exclusions"] == [] and p1["excluded"] == []
    assert json.dumps(p1["items"], sort_keys=True) == \
        json.dumps(p2["items"], sort_keys=True)


def test_exclusions_never_reach_qloo_calls():
    plan = TastePilotAgent().run(KYOTO + ", but no jazz")
    sealed = json.dumps(plan["evidence"])
    qloo_entries = [e for e in plan["evidence"]["entries"]
                    if e.get("kind") == "qloo_call"]
    assert qloo_entries
    for entry in qloo_entries:
        assert "exclusion" not in json.dumps(entry).lower()
    assert sealed  # the request text itself is sealed, as always


def test_parser_shape_and_parse_request_integration():
    parsed = parse_request(KYOTO + ", but no jazz and no wine")
    assert parsed.exclusions == ["jazz", "wine"]


def test_exports_name_excluded_items_and_provenance():
    plan = TastePilotAgent().run(KYOTO + ", but no jazz")
    md = render_markdown(plan)
    assert "## Excluded by your request" in md
    assert "Nujabes" in md and "matched exclusion 'jazz'" in md
    assert "Exclusions: jazz" in md
    text = render_text(plan)
    assert "Excluded by your request:" in text and "Nujabes" in text
    clean = render_markdown(TastePilotAgent().run(KYOTO))
    assert "Excluded by your request" not in clean


def test_cli_reports_exclusions(capsys):
    rc = cli.main(["plan", KYOTO + ", but no jazz", "--mode", "mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "excluded=1" in out
    assert re.search(r"excluded: Nujabes \(music\) — matched "
                     r"exclusion 'jazz'", out)


def test_server_plan_carries_exclusions_and_ui_wiring():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    port = server.server_address[1]
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = json.dumps(
            {"text": KYOTO + ", but no jazz"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/plan", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            plan = json.loads(resp.read())
        assert plan["exclusions"] == ["jazz"]
        assert [e["name"] for e in plan["excluded"]] == ["Nujabes"]
        assert plan["checker"]["passed"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            page = resp.read().decode()
        assert "Exclusions:" in page and "Excluded by your request" in page
    finally:
        server.shutdown()


def test_docs_describe_exclusions():
    assert "no jazz" in README.read_text()
    assert "excluded" in SUBMISSION.read_text().lower()
    assert "Exclusions" in INDEX.read_text()
