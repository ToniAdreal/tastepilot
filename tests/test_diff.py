"""Plan comparison: two plans diffed item by item, with BOTH sides'
verdicts recomputed from their own sealed records — a stored checker
field is never read on either side. The branch explorer's lines gain
the server-recomputed "+added −removed ~rescored" summary, so what a
swap path changed is stated, not eyeballed from two item lists."""
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
from tastepilot.diff import diff_plans
from tastepilot.server import make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
PRIMARY = "Gion, Kyoto"  # place primary
ALT = "Kyoto National Museum"  # place alternative


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def _swapped():
    return swap_plan(_kyoto(), PRIMARY, ALT)


def test_identical_plans_diff_clean():
    plan = _kyoto()
    result = diff_plans(plan, copy.deepcopy(plan))
    assert result["summary"]["identical"] is True
    assert result["added"] == [] and result["removed"] == []
    assert result["affinity_changes"] == []
    assert result["base"]["passed"] is True
    assert result["other"]["passed"] is True
    assert result["base"]["chain_valid"] is True
    assert result["base"]["swap_count"] == 0
    assert result["summary"]["kept"] == len(plan["items"])


def test_swap_diff_names_added_and_removed():
    base, swapped = _kyoto(), _swapped()
    result = diff_plans(base, swapped)
    assert [i["name"] for i in result["added"]] == [ALT]
    assert [i["name"] for i in result["removed"]] == [PRIMARY]
    assert result["summary"]["identical"] is False
    assert result["base"]["swap_count"] == 0
    assert result["other"]["swap_count"] == 1
    # The swapped-out primary is now an alternative and vice versa.
    assert [i["name"] for i in result["alternatives_added"]] == [PRIMARY]
    assert [i["name"] for i in result["alternatives_removed"]] == [ALT]
    assert result["other"]["passed"] is True


def test_affinity_change_on_a_kept_slot_is_reported():
    base = _kyoto()
    other = copy.deepcopy(base)
    # Presentation-only edit: the item stays in place with a moved
    # affinity — the diff reports the change AND the other side now
    # fails recomputation (the sealed response disagrees).
    other["items"][0]["affinity"] = 0.42
    result = diff_plans(base, other)
    assert len(result["affinity_changes"]) == 1
    change = result["affinity_changes"][0]
    assert change["name"] == base["items"][0]["entity"]["name"]
    assert change["base_affinity"] == base["items"][0]["affinity"]
    assert change["other_affinity"] == 0.42
    assert change["delta"] == pytest.approx(0.42 - change["base_affinity"])
    assert result["base"]["passed"] is True
    assert result["other"]["passed"] is False
    assert any(v["reason"] == "affinity_mismatch"
               for v in result["other"]["violations"])


def test_stored_checker_field_is_never_trusted():
    base = _kyoto()
    lying = copy.deepcopy(base)
    lying["checker"] = {"passed": True, "chain_valid": True}
    lying["items"][0]["affinity"] = 0.01  # breaks the sealed trace
    result = diff_plans(base, lying)
    assert result["other"]["passed"] is False


def test_missing_evidence_side_reports_not_verified():
    base = _kyoto()
    bare = {"items": copy.deepcopy(base["items"]),
            "alternatives": copy.deepcopy(base["alternatives"])}
    result = diff_plans(base, bare)
    assert result["other"]["passed"] is False
    assert result["other"]["chain_valid"] is False
    assert result["other"]["swap_count"] is None
    assert result["summary"]["identical"] is True  # same items


def test_same_entity_in_another_leg_is_remove_plus_add():
    base = _kyoto()
    other = copy.deepcopy(base)
    item = other["items"][1]
    item["location"] = "Osaka"
    item["entity"]["location"] = "Osaka"
    result = diff_plans(base, other)
    assert [i["name"] for i in result["removed"]] == \
        [base["items"][1]["entity"]["name"]]
    assert [i["name"] for i in result["added"]] == \
        [base["items"][1]["entity"]["name"]]
    assert result["added"][0]["location"] == "Osaka"


def test_schedule_change_is_reported():
    base = _kyoto()
    other = copy.deepcopy(base)
    other["schedule"] = [{"location": "Kyoto", "start": "2026-11-03",
                          "end": "2026-11-05", "nights": 2, "dates": []}]
    result = diff_plans(base, other)
    assert result["schedule_changes"] == [{
        "location": "Kyoto",
        "base": {"start": None, "end": None},
        "other": {"start": "2026-11-03", "end": "2026-11-05"}}]
    assert result["summary"]["identical"] is False


def test_diff_is_deterministic_and_never_mutates():
    base, swapped = _kyoto(), _swapped()
    base_copy, swapped_copy = copy.deepcopy(base), copy.deepcopy(swapped)
    first = diff_plans(base, swapped)
    assert diff_plans(base, swapped) == first
    assert base == base_copy and swapped == swapped_copy


@pytest.mark.parametrize("base,other", [
    ("nope", {}), ({}, "nope"), ({"items": "nope"}, {}),
    ({}, {"alternatives": [42]}),
])
def test_malformed_plans_raise_bad_plan(base, other):
    with pytest.raises(ValueError, match="bad_plan"):
        diff_plans(base, other)


def test_cli_diff_round_trip_and_exit_codes(tmp_path, capsys):
    base_path = tmp_path / "base.json"
    swapped_path = tmp_path / "swapped.json"
    base_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    swapped_path.write_text(json.dumps(_swapped()), encoding="utf-8")
    out_path = tmp_path / "diff.json"
    rc = cli.main(["diff", "--base", str(base_path),
                   "--other", str(swapped_path), "--out", str(out_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "added=1 removed=1" in out
    assert f"+ [place] {ALT}" in out and f"- [place] {PRIMARY}" in out
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["summary"]["added"] == 1
    # Identical plans: exit 0.
    assert cli.main(["diff", "--base", str(base_path),
                     "--other", str(base_path)]) == 0
    capsys.readouterr()
    # A tampered side is a verdict (exit 1), not a crash.
    tampered = _kyoto()
    tampered["items"][0]["affinity"] = 0.01
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    rc = cli.main(["diff", "--base", str(base_path),
                   "--other", str(tampered_path)])
    out = capsys.readouterr()
    assert rc == 1 and "Traceback" not in out.out + out.err
    # Missing files and malformed plans are caller errors (exit 2).
    assert cli.main(["diff", "--base", str(tmp_path / "nope.json"),
                     "--other", str(base_path)]) == 2
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps({"items": "nope"}), encoding="utf-8")
    rc = cli.main(["diff", "--base", str(bad_path),
                   "--other", str(base_path)])
    err = capsys.readouterr().err
    assert rc == 2 and "bad_plan" in err and "Traceback" not in err


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


def test_server_diff_endpoint():
    server, port = _serve(TastePilotAgent())
    try:
        status, plan, _ = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        status, swapped, _ = _post(port, "/api/swap", {
            "plan": plan, "primary": PRIMARY, "alternative": ALT})
        assert status == 200
        status, result, headers = _post(
            port, "/api/diff", {"base": plan, "other": swapped})
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        assert [i["name"] for i in result["added"]] == [ALT]
        assert result["base"]["passed"] is True
        assert result["other"]["swap_count"] == 1
        # A tampered side diffs as NOT VERIFIED, still HTTP 200.
        tampered = copy.deepcopy(swapped)
        tampered["items"][0]["affinity"] = 0.01
        status, result, _ = _post(
            port, "/api/diff", {"base": plan, "other": tampered})
        assert status == 200 and result["other"]["passed"] is False
        # Shape gates: missing side / malformed side -> 400.
        status, body, _ = _post(port, "/api/diff", {"base": plan})
        assert status == 400
        status, body, _ = _post(
            port, "/api/diff", {"base": plan, "other": {"items": "x"}})
        assert status == 400 and "Traceback" not in json.dumps(body)
    finally:
        server.shutdown()


def test_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "/api/diff" in html and "vs base:" in html
    assert "diff.py" in README.read_text(encoding="utf-8")
    assert "compar" in SUBMISSION.read_text(encoding="utf-8").lower()
    assert "/api/diff" in DEPLOY.read_text(encoding="utf-8")
