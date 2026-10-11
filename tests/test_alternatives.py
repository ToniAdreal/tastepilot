"""Evidence-backed alternatives: each insights call asks Qloo for
more results than the plan shows (ITEMS_PER_DOMAIN + 2); the overflow
used to be discarded. Now it is offered as swap options — held to
exactly the primary standard: cited to the same sealed insights
response, checker-verified against the final record before being
kept, filtered by the user's exclusions, and covered by every
verification surface (CLI verify, server verify, exports, share
payloads), so tampering with an alternative fails verification just
like tampering with a primary item."""
import copy
import json
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tastepilot import cli, fixtures
from tastepilot.agent import TastePilotAgent
from tastepilot.checker import plan_recommendations
from tastepilot.export import export_plan, render_markdown, render_text
from tastepilot.handoff import plan_handoffs
from tastepilot.models import QlooResponse, ScoredEntity
from tastepilot.qloo import response_id_for
from tastepilot.server import make_handler

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"


class OverflowClient:
    """Stub Qloo client: one seed, and a film insights call returning
    four results (two primaries + two overflow) — other domains one."""

    source = "mock"

    def search(self, query, limit=10):
        ent = fixtures.BY_ID["mov_spirited_away"]
        results = [ScoredEntity(ent, 1.0)]
        request = {"query": query, "limit": limit}
        return QlooResponse(
            response_id_for("search", request, "mock", results),
            "search", request, results, "mock", len(results))

    def insights(self, seed_ids, target_domain, limit=5, location=None):
        request = {"filter.type": target_domain,
                   "signal.interests.entities": ",".join(seed_ids),
                   "take": limit, "location": location}
        if target_domain == "film":
            scored = [ScoredEntity(fixtures.BY_ID[eid], aff) for eid, aff in
                      [("mov_amelie", 0.9), ("mov_parasite", 0.8),
                       ("mov_grand_budapest", 0.7), ("mov_blade_runner", 0.6)]]
        else:
            scored = [ScoredEntity(fixtures.BY_ID["art_nujabes"], 0.5)] \
                if target_domain == "music" else []
        return QlooResponse(
            response_id_for("insights", request, "mock", scored),
            "insights", request, scored, "mock", len(scored))


def test_kyoto_plan_surfaces_verified_alternatives():
    plan = TastePilotAgent().run(KYOTO)
    alts = plan["alternatives"]
    assert [(a["entity"]["name"], a["domain"], a["affinity"])
            for a in alts] == [
        ("Kyoto National Museum", "place", 0.75),
        ("A seasonal kaiseki tasting menu", "food", 0.72)]
    for alt in alts:
        assert alt["verification"]["admissible"] is True
        assert alt["verification"]["reason"] == "ok"
        assert alt["verification"]["evidence"]["affinity_in_response"] \
            == alt["affinity"]
    assert plan["checker"]["alternatives"] == \
        [a["verification"] for a in alts]
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]


def test_alternatives_are_not_primary_items():
    plan = TastePilotAgent().run(KYOTO)
    primary = [i["entity"]["name"] for i in plan["items"]]
    assert len(primary) == 7  # primaries unchanged by the new field
    for alt in plan["alternatives"]:
        assert alt["entity"]["name"] not in primary
    grouped = [i["entity"]["name"] for g in plan["groups"]
               for i in g["items"]]
    handed = [i["name"] for i in plan["handoffs"]["items"]]
    for alt in plan["alternatives"]:
        assert alt["entity"]["name"] not in grouped
        assert alt["entity"]["name"] not in handed


def test_stub_client_overflow_becomes_alternatives():
    plan = TastePilotAgent(qloo_client=OverflowClient()).run(
        "a generic plan, I love Spirited Away")
    film_alts = [a for a in plan["alternatives"] if a["domain"] == "film"]
    assert [(a["entity"]["name"], a["affinity"]) for a in film_alts] == [
        ("The Grand Budapest Hotel", 0.7), ("Blade Runner", 0.6)]
    film_primary = [i["entity"]["name"] for i in plan["items"]
                    if i["domain"] == "film"]
    assert film_primary == ["Amelie", "Parasite"]
    # Every alternative cites the very response that produced it.
    refs = {a["evidence_ref"] for a in film_alts}
    assert len(refs) == 1
    assert plan["checker"]["passed"]


def test_no_overflow_means_no_alternatives():
    plan = TastePilotAgent().run(
        "a first date in New York, we both love Kind of Blue")
    assert plan["alternatives"] == []
    assert plan["checker"]["alternatives"] == []
    assert plan["checker"]["passed"]


def test_exclusions_filter_alternatives_too():
    plan = TastePilotAgent().run(KYOTO + ", but no museums")
    names = [a["entity"]["name"] for a in plan["alternatives"]]
    assert names == ["A seasonal kaiseki tasting menu"]
    # The museum was an alternative, not a primary: it is not in the
    # excluded list either — it is simply never offered.
    assert plan["excluded"] == []


def test_alternatives_deterministic():
    p1 = TastePilotAgent().run(KYOTO)
    p2 = TastePilotAgent().run(KYOTO)
    assert json.dumps(p1["alternatives"], sort_keys=True) == \
        json.dumps(p2["alternatives"], sort_keys=True)


def test_plan_recommendations_combines_items_and_alternatives():
    plan = TastePilotAgent().run(KYOTO)
    combined = plan_recommendations(plan)
    assert len(combined) == len(plan["items"]) + len(plan["alternatives"])
    assert plan_recommendations({"items": [1]}) == [1]


def test_tampered_alternative_fails_every_surface(tmp_path):
    plan = TastePilotAgent().run(KYOTO)
    plan["alternatives"][0]["affinity"] = 0.01
    # CLI verify
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    assert cli.main(["verify", "--record", str(path)]) == 1
    # Export banner
    content, verified = export_plan(plan, "markdown")
    assert verified is False and "NOT VERIFIED" in content
    assert "affinity_mismatch" in content
    # Share payload verdict line
    payload = plan_handoffs(plan)["share"]
    assert "NOT VERIFIED" in payload["text"]


def test_exports_list_alternatives():
    plan = TastePilotAgent().run(KYOTO)
    md = render_markdown(plan)
    assert "## Alternatives (evidence-backed swap options)" in md
    assert "Kyoto National Museum" in md and "checker: ok" in md
    text = render_text(plan)
    assert "Alternatives (evidence-backed swap options):" in text
    assert "A seasonal kaiseki tasting menu" in text
    clean = render_markdown(TastePilotAgent().run(
        "a first date in New York, we both love Kind of Blue"))
    assert "Alternatives" not in clean


def test_cli_reports_alternatives(capsys):
    rc = cli.main(["plan", KYOTO, "--mode", "mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alternatives=2" in out
    assert re.search(r"alternative: \[place\] Kyoto National Museum "
                     r"\(affinity 0\.75\) — checker: ok", out)


def test_server_plan_and_verify_cover_alternatives():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"text": KYOTO}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/plan", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            plan = json.loads(resp.read())
        assert len(plan["alternatives"]) == 2
        # Verify-by-API recomputes alternatives as well.
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/verify",
            data=json.dumps(plan).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            report = json.loads(resp.read())
        assert report["passed"] and report["alternative_count"] == 2
        assert all(r["admissible"] for r in report["alternatives"])
        tampered = copy.deepcopy(plan)
        tampered["alternatives"][0]["affinity"] = 0.01
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/verify",
            data=json.dumps(tampered).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            report = json.loads(resp.read())
        assert report["passed"] is False
        assert any(v["reason"] == "affinity_mismatch"
                   for v in report["violations"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            page = resp.read().decode()
        assert "Alternatives (evidence-backed swap options)" in page
        assert "alternative, checker:" in page
    finally:
        server.shutdown()


def test_docs_describe_alternatives():
    assert "alternatives" in README.read_text().lower()
    assert "swap option" in SUBMISSION.read_text()
    assert "Alternatives" in INDEX.read_text()
