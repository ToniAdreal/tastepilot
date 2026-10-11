"""Guided branch suggestions: the agent proposes the swap branches
worth exploring, ranked deterministically by affinity delta and
taste coverage, as specs explore_branches accepts unchanged.
Suggestions propose only — the base is verified first, never
mutated, never re-sealed; a tampered plan is refused."""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.server import make_handler
from tastepilot.swap import (explore_branches, suggest_branches,
                             validate_branches)

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def test_candidates_ranked_by_score_delta_and_coverage():
    result = suggest_branches(_kyoto())
    cands = result["candidates"]
    # 2 place primaries x museum + 2 food primaries x kaiseki.
    assert len(cands) == 4
    pairs = [(c["primary"], c["alternative"]) for c in cands]
    # Score = alt + delta + 0.05/new tag: Arashiyama->Museum 0.73,
    # izakaya->kaiseki 0.69, Gion->Museum 0.67, ramen->kaiseki 0.63.
    assert pairs == [
        ("Arashiyama Bamboo Grove", "Kyoto National Museum"),
        ("A lantern-lit izakaya in Pontocho Alley",
         "A seasonal kaiseki tasting menu"),
        ("Gion, Kyoto", "Kyoto National Museum"),
        ("A quiet ramen counter in Gion",
         "A seasonal kaiseki tasting menu"),
    ]
    top = cands[0]
    assert top["affinity_delta"] == pytest.approx(-0.07)
    assert top["alternative_affinity"] == pytest.approx(0.75)
    assert top["new_tags"] == ["art"]
    assert top["coverage_gain"] == 1
    assert top["score"] == pytest.approx(0.73)
    assert result["base"]["passed"] is True


def test_suggestions_include_singles_and_guided_tour():
    result = suggest_branches(_kyoto())
    sugs = result["suggestions"]
    # limit 5: the single budget is limit-1 = 4 (one slot reserved
    # for the tour); all 4 candidates fit, plus the tour = 5.
    assert len(sugs) == 5
    tour = sugs[-1]
    assert tour["name"] == "Guided tour: best swap in each domain"
    assert len(tour["swaps"]) == 2
    domains = {s["domain"] for s in
               [c for c in result["candidates"]]}
    assert domains == {"place", "food"}
    assert tour["affinity_delta"] == pytest.approx(-0.15)
    assert sorted(tour["new_tags"]) == ["art", "seasonal"]
    # Every suggestion is a valid branch spec, names unique/<=60.
    validate_branches(result["branches"])
    names = [b["name"] for b in result["branches"]]
    assert len(set(names)) == len(names)
    assert all(len(n) <= 60 for n in names)
    assert [b["name"] for b in result["branches"]] == \
        [s["name"] for s in sugs]


def test_suggested_branches_all_apply_via_explore():
    plan = _kyoto()
    result = suggest_branches(plan)
    explored = explore_branches(plan, result["branches"])
    assert all(b["ok"] and b["passed"] for b in explored["branches"])
    tour = explored["branches"][-1]
    assert tour["swap_count"] == 2
    assert "Kyoto National Museum" in tour["items"]
    assert "A seasonal kaiseki tasting menu" in tour["items"]


def test_suggest_is_deterministic_and_never_mutates():
    plan = _kyoto()
    snapshot = copy.deepcopy(plan)
    first = suggest_branches(plan)
    second = suggest_branches(plan)
    assert first == second
    assert plan == snapshot
    # No resealing happened: the evidence record is untouched.
    assert plan["evidence"] == snapshot["evidence"]


def test_suggest_limit_respected():
    plan = _kyoto()
    assert len(suggest_branches(plan, limit=1)["suggestions"]) == 1
    assert len(suggest_branches(plan, limit=2)["suggestions"]) == 2
    # With limit 2 the tour still gets its reserved slot.
    names = [s["name"] for s in suggest_branches(plan, limit=2)["suggestions"]]
    assert names[-1] == "Guided tour: best swap in each domain"
    for bad in (0, 7, -1, "5", 2.5, True, None):
        with pytest.raises(ValueError, match="bad_limit"):
            suggest_branches(plan, limit=bad)


def test_suggest_no_alternatives_yields_empty():
    plan = _kyoto()
    plan["alternatives"] = []
    result = suggest_branches(plan)
    assert result["candidates"] == []
    assert result["suggestions"] == []
    assert result["branches"] == []
    assert result["base"]["passed"] is True


def test_suggest_refuses_tampered_and_broken_plans():
    tampered = _kyoto()
    tampered["items"][0]["affinity"] = 0.01
    with pytest.raises(ValueError, match="plan_not_verified"):
        suggest_branches(tampered)
    broken = _kyoto()
    broken["evidence"]["entries"][1]["response"]["results"][0][
        "affinity"] = 0.01
    with pytest.raises(ValueError, match="evidence_chain_invalid"):
        suggest_branches(broken)
    with pytest.raises(ValueError, match="missing_evidence"):
        suggest_branches({"items": [], "alternatives": []})
    with pytest.raises(ValueError, match="bad_plan"):
        suggest_branches("nope")


def test_cli_suggest_round_trip_and_spec_feeds_branches(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    out_path = tmp_path / "suggestions.json"
    rc = cli.main(["suggest", "--plan", str(plan_path),
                   "--out", str(out_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "candidates=4" in out and "suggestions=5" in out
    assert "suggestion #1" in out and "Guided tour" in out
    spec = json.loads(out_path.read_text(encoding="utf-8"))
    validate_branches(spec["branches"])
    # The written file is a branches spec unchanged.
    rc = cli.main(["branches", "--plan", str(plan_path),
                   "--spec", str(out_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "Guided tour" in out


def test_cli_suggest_failures(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    tampered = _kyoto()
    tampered["items"][0]["affinity"] = 0.01
    plan_path.write_text(json.dumps(tampered), encoding="utf-8")
    rc = cli.main(["suggest", "--plan", str(plan_path)])
    err = capsys.readouterr().err
    assert rc == 1 and "plan_not_verified" in err
    assert "Traceback" not in err
    plan_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    rc = cli.main(["suggest", "--plan", str(plan_path), "--limit", "9"])
    err = capsys.readouterr().err
    assert rc == 2 and "bad_limit" in err
    rc = cli.main(["suggest", "--plan", str(tmp_path / "missing.json")])
    assert rc == 2


def _serve(agent):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def test_server_suggest_endpoint():
    server, port = _serve(TastePilotAgent())
    try:
        status, plan, _ = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        status, result, headers = _post(port, "/api/suggest",
                                        {"plan": plan})
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        assert len(result["candidates"]) == 4
        assert result["suggestions"][0]["name"].startswith(
            "Kyoto National Museum instead of")
        # Suggested branches explore through the standard endpoint.
        status, explored, _ = _post(port, "/api/branches", {
            "plan": plan, "branches": result["branches"]})
        assert status == 200
        assert all(b["ok"] for b in explored["branches"])
        # Limit + refusal shapes.
        status, limited, _ = _post(port, "/api/suggest",
                                   {"plan": plan, "limit": 2})
        assert status == 200 and len(limited["suggestions"]) == 2
        status, body, _ = _post(port, "/api/suggest",
                                {"plan": plan, "limit": 99})
        assert status == 400 and "bad_limit" in body["error"]
        status, body, _ = _post(port, "/api/suggest", {})
        assert status == 400 and "plan is required" in body["error"]
        tampered = copy.deepcopy(plan)
        tampered["items"][0]["affinity"] = 0.01
        status, body, _ = _post(port, "/api/suggest", {"plan": tampered})
        assert status == 400 and "plan_not_verified" in body["error"]
    finally:
        server.shutdown()


def test_ui_and_docs_describe_guided_suggestions():
    page = INDEX.read_text()
    assert "/api/suggest" in page and "suggestbtn" in page
    assert "Suggest branches" in page and "Guided suggestions" in page
    assert "exploreguidedbtn" in page and "doSuggest" in page
    readme = README.read_text()
    assert "suggest_branches" in readme
    submission = SUBMISSION.read_text()
    assert "Guided branch suggestions" not in submission
    assert "affinity delta" in submission
    assert "Suggest probe" in DEPLOY.read_text()
