"""Branching swap timelines: one base plan forked into named swap
paths, each applied independently, re-sealed, and compared by
verdicts recomputed from its own record. A failing branch is a
per-branch result beside the successful ones; the base plan is
never mutated; a branch plan is an ordinary swapped plan that
every existing surface (verify, further swaps, undo) accepts."""
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
from tastepilot.evidence import verify_chain
from tastepilot.server import make_handler
from tastepilot.swap import (apply_swap_path, explore_branches,
                             swap_history, swap_plan, undo_last_swap,
                             validate_branches)

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
PRIMARY = "Gion, Kyoto"  # place primary
ALT = "Kyoto National Museum"  # place alternative
FOOD_ALT = "A seasonal kaiseki tasting menu"  # food alternative

SPEC = [
    {"name": "As planned", "swaps": []},
    {"name": "Museum instead of Gion",
     "swaps": [{"primary": PRIMARY, "alternative": ALT}]},
    {"name": "Both swaps",
     "swaps": [{"primary": PRIMARY, "alternative": ALT},
               {"primary": "A quiet ramen counter in Gion",
                "alternative": FOOD_ALT}]},
    {"name": "Impossible",
     "swaps": [{"primary": PRIMARY, "alternative": FOOD_ALT}]},
]


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def test_validate_branches_shape_gate():
    validate_branches(SPEC)  # no raise
    for bad, category in [
            ("nope", "bad_branches"), ([], "bad_branches"),
            ([{"name": "x", "swaps": []}] * 7, "too_many_branches"),
            (["nope"], "bad_branch"),
            ([{"swaps": []}], "bad_branch_name"),
            ([{"name": "  ", "swaps": []}], "bad_branch_name"),
            ([{"name": "x" * 61, "swaps": []}], "bad_branch_name"),
            ([{"name": "a", "swaps": []}, {"name": "a", "swaps": []}],
             "duplicate_branch_name"),
            ([{"name": "a", "swaps": {}}], "bad_branch_swaps"),
            ([{"name": "a", "swaps": [{"primary": "x"}]}],
             "bad_branch_step"),
            ([{"name": "a", "swaps": [{"primary": 3, "alternative": "y"}]}],
             "bad_branch_step")]:
        with pytest.raises(ValueError, match=category):
            validate_branches(bad)
    with pytest.raises(ValueError, match="too_many_swaps"):
        validate_branches([{"name": "a", "swaps": [
            {"primary": "x", "alternative": "y"}] * 11}])


def test_explore_branches_compares_independent_forks():
    base = _kyoto()
    snapshot = copy.deepcopy(base)
    result = explore_branches(base, SPEC)
    assert base == snapshot  # the base plan is never mutated
    assert result["base"]["passed"] is True
    assert result["base"]["swap_count"] == 0
    branches = {b["name"]: b for b in result["branches"]}
    as_planned = branches["As planned"]
    assert as_planned["ok"] and as_planned["passed"]
    assert as_planned["items"] == result["base"]["items"]
    assert as_planned["plan"]["branch"] == {
        "name": "As planned", "swaps_applied": 0, "base_swap_count": 0}
    museum = branches["Museum instead of Gion"]
    assert museum["ok"] and museum["passed"] and museum["chain_valid"]
    assert museum["swap_count"] == 1
    assert ALT in museum["items"] and PRIMARY not in museum["items"]
    both = branches["Both swaps"]
    assert both["ok"] and both["swap_count"] == 2
    assert ALT in both["items"] and FOOD_ALT in both["items"]
    # The failing branch (a place primary for a food alternative is
    # a domain mismatch) is a result, not an aborted exploration.
    impossible = branches["Impossible"]
    assert impossible["ok"] is False
    assert impossible["error"] == "domain_mismatch"
    assert "plan" not in impossible


def test_branch_plans_are_independent_of_each_other():
    result = explore_branches(_kyoto(), SPEC)
    branches = {b["name"]: b for b in result["branches"] if b["ok"]}
    museum = branches["Museum instead of Gion"]["plan"]
    both = branches["Both swaps"]["plan"]
    # Mutating one branch's plan changes nothing about another's
    # recomputed verdict.
    museum["items"][0]["affinity"] = 0.01
    again = explore_branches(_kyoto(), SPEC)
    assert again["branches"][2]["passed"] is True
    assert verify_chain(both["evidence"])


def test_branch_plan_is_an_ordinary_swapped_plan():
    result = explore_branches(_kyoto(), SPEC)
    museum = next(b["plan"] for b in result["branches"]
                  if b["name"] == "Museum instead of Gion")
    # Its audit trail is the sealed history; undo works on it.
    assert len(swap_history(museum)) == 1
    undone = undo_last_swap(museum)
    assert PRIMARY in [i["entity"]["name"] for i in undone["items"]]
    # And it can be swapped further through the normal path.
    further = swap_plan(museum, "A quiet ramen counter in Gion", FOOD_ALT)
    assert len(swap_history(further)) == 2


def test_explore_branches_from_a_swapped_base():
    base = swap_plan(_kyoto(), PRIMARY, ALT)
    result = explore_branches(base, [
        {"name": "As planned", "swaps": []},
        {"name": "Add kaiseki",
         "swaps": [{"primary": "A quiet ramen counter in Gion",
                    "alternative": FOOD_ALT}]}])
    assert result["base"]["swap_count"] == 1
    branch = result["branches"][1]
    assert branch["ok"] and branch["swap_count"] == 2
    assert branch["plan"]["branch"]["base_swap_count"] == 1


def test_apply_swap_path_and_empty_path_proves_the_base():
    plan = _kyoto()
    same = apply_swap_path(plan, [])
    assert same == plan and same is not plan
    tampered = _kyoto()
    tampered["items"][0]["affinity"] = 0.01
    with pytest.raises(ValueError, match="plan_not_verified"):
        apply_swap_path(tampered, [])
    with pytest.raises(ValueError, match="domain_mismatch"):
        apply_swap_path(plan, [{"primary": PRIMARY,
                                "alternative": FOOD_ALT}])


def test_explore_branches_on_a_tampered_base_fails_every_branch():
    tampered = _kyoto()
    tampered["items"][0]["affinity"] = 0.01
    result = explore_branches(tampered, SPEC[:2])
    assert result["base"]["passed"] is False
    assert all(not b["ok"] for b in result["branches"])
    assert {b["error"] for b in result["branches"]} == \
        {"plan_not_verified"}


def test_cli_branches_round_trip(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    spec_path = tmp_path / "branches.json"
    spec_path.write_text(json.dumps({"branches": SPEC}),
                         encoding="utf-8")
    out_dir = tmp_path / "branches"
    rc = cli.main(["branches", "--plan", str(plan_path),
                   "--spec", str(spec_path), "--out-dir", str(out_dir)])
    out = capsys.readouterr().out
    # One branch refuses (domain mismatch) -> verdict exit 1.
    assert rc == 1 and "base:" in out and "passed=True" in out
    assert "Museum instead of Gion" in out and "swaps=1" in out
    assert "Impossible" in out and "domain_mismatch" in out
    museum_file = out_dir / "Museum-instead-of-Gion.json"
    assert museum_file.exists()
    assert cli.main(["verify", "--record", str(museum_file)]) == 0
    written = json.loads(museum_file.read_text(encoding="utf-8"))
    assert written["branch"]["name"] == "Museum instead of Gion"


def test_cli_branches_all_ok_and_failures(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    spec_path = tmp_path / "branches.json"
    spec_path.write_text(json.dumps(SPEC[:2]), encoding="utf-8")
    rc = cli.main(["branches", "--plan", str(plan_path),
                   "--spec", str(spec_path)])
    assert rc == 0
    spec_path.write_text(json.dumps([{"name": "a", "swaps": []},
                                     {"name": "a", "swaps": []}]),
                         encoding="utf-8")
    rc = cli.main(["branches", "--plan", str(plan_path),
                   "--spec", str(spec_path)])
    err = capsys.readouterr().err
    assert rc == 2 and "duplicate_branch_name" in err
    assert "Traceback" not in err
    rc = cli.main(["branches", "--plan", str(plan_path),
                   "--spec", str(tmp_path / "missing.json")])
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


def test_server_branches_endpoint():
    server, port = _serve(TastePilotAgent())
    try:
        status, plan, _ = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        status, result, headers = _post(port, "/api/branches", {
            "plan": plan, "branches": SPEC})
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        branches = {b["name"]: b for b in result["branches"]}
        assert branches["Both swaps"]["swap_count"] == 2
        assert branches["Impossible"]["ok"] is False
        # A branch plan verifies through the standard endpoint.
        status, report, _ = _post(
            port, "/api/verify", branches["Both swaps"]["plan"])
        assert status == 200 and report["passed"]
        assert report["swap_count"] == 2
        # Spec-shape problems and missing fields are 400s.
        status, body, _ = _post(port, "/api/branches", {
            "plan": plan, "branches": [{"name": "a", "swaps": []},
                                       {"name": "a", "swaps": []}]})
        assert status == 400 and "duplicate_branch_name" in body["error"]
        status, body, _ = _post(port, "/api/branches", {"plan": plan})
        assert status == 400 and "branches are required" in body["error"]
        status, body, _ = _post(port, "/api/branches", {"branches": SPEC})
        assert status == 400
        tampered = copy.deepcopy(plan)
        tampered["items"][0]["affinity"] = 0.01
        status, result, _ = _post(port, "/api/branches", {
            "plan": tampered, "branches": SPEC[:1]})
        assert status == 200 and result["branches"][0]["ok"] is False
        assert result["branches"][0]["error"] == "plan_not_verified"
    finally:
        server.shutdown()


def test_ui_and_docs_describe_branching():
    page = INDEX.read_text()
    assert "/api/branches" in page and "branchbtn" in page
    assert "Explore branches from alternatives" in page
    assert "loadbranchbtn" in page and "Branch explorer" in page
    readme = README.read_text()
    assert "explore_branches" in readme
    submission = SUBMISSION.read_text()
    assert "branch" in submission.lower()
    assert "Branching swap timelines" not in submission
    assert "Branch probe" in DEPLOY.read_text()
