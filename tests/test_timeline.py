"""Visual branch timeline: recommendation slots grouped by leg,
one column per branch, every cell kept/added/removed/re-scored/
absent — and every column's verdict recomputed from its own sealed
record, so a tampered branch is a FAIL column in the grid."""
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
from tastepilot.swap import swap_plan
from tastepilot.timeline import branch_timeline

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def _swapped():
    return swap_plan(_kyoto(), PRIMARY, ALT)


def _cell(result, name, column=1):
    for leg in result["legs"]:
        for row in leg["rows"]:
            if row["name"] == name:
                return row["cells"][column]
    raise AssertionError(f"no row for {name}")


def test_timeline_base_and_swapped_branch():
    base, swapped = _kyoto(), _swapped()
    result = branch_timeline(base, [{"name": "Museum", "plan": swapped}])
    assert [c["name"] for c in result["columns"]] == ["As planned", "Museum"]
    assert all(c["passed"] for c in result["columns"])
    assert result["summary"]["all_passed"] is True
    assert _cell(result, PRIMARY, 0)["status"] == "kept"
    assert _cell(result, PRIMARY, 1)["status"] == "removed"
    assert _cell(result, ALT, 0)["status"] == "absent"
    assert _cell(result, ALT, 1)["status"] == "added"
    kept = _cell(result, "Kyoto", 1)
    assert kept["status"] == "kept" and kept["affinity"] == 0.90
    assert result["columns"][1]["swap_count"] == 1


def test_timeline_legs_group_rows_in_trip_order():
    plan = TastePilotAgent().run(
        "a trip from Kyoto to Paris, I love Spirited Away and Amelie")
    result = branch_timeline(plan, [{"name": "Same", "plan": plan}])
    assert [leg["location"] for leg in result["legs"]] == ["Kyoto", "Paris"]
    assert all(leg["rows"] for leg in result["legs"])
    assert result["columns"][1]["summary"]["identical"] is True


def test_timeline_rescored_cell_reports_delta():
    base = _kyoto()
    other = copy.deepcopy(base)
    item = next(i for i in other["items"] if i["entity"]["name"] == "Kyoto")
    item["affinity"] = 0.5  # tampered: verdict must FAIL, cell re-scored
    result = branch_timeline(base, [{"name": "Edited", "plan": other}])
    cell = _cell(result, "Kyoto", 1)
    assert cell["status"] == "rescored"
    assert cell["delta"] == pytest.approx(-0.40)
    assert result["columns"][1]["passed"] is False
    assert result["summary"]["all_passed"] is False


def test_timeline_tampered_branch_is_fail_column_not_error():
    base, swapped = _kyoto(), _swapped()
    swapped["items"][0]["affinity"] = 0.01
    result = branch_timeline(base, [{"name": "Bad", "plan": swapped}])
    assert result["columns"][0]["passed"] is True
    assert result["columns"][1]["passed"] is False


def test_timeline_deterministic_and_inputs_unmutated():
    base, swapped = _kyoto(), _swapped()
    base_copy, swapped_copy = copy.deepcopy(base), copy.deepcopy(swapped)
    branches = [{"name": "Museum", "plan": swapped}]
    first = branch_timeline(base, branches)
    second = branch_timeline(base, branches)
    assert first == second
    assert base == base_copy and swapped == swapped_copy


@pytest.mark.parametrize("branches", [
    [], "x", [{}], [{"name": "", "plan": {}}], [{"name": "A"}],
    [{"name": "A", "plan": {}}, {"name": "A", "plan": {}}],
    [{"name": "A", "plan": {}}] * 7,
])
def test_timeline_branch_shape_gate(branches):
    with pytest.raises(ValueError):
        branch_timeline(_kyoto(), branches)


def test_timeline_bad_base_plan():
    with pytest.raises(ValueError, match="bad_plan"):
        branch_timeline({"items": "x"}, [{"name": "A", "plan": _kyoto()}])


def _serve(agent):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_timeline_endpoint_over_http():
    server, port = _serve(TastePilotAgent())
    try:
        status, plan = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        swapped = swap_plan(plan, PRIMARY, ALT)
        status, data = _post(port, "/api/timeline", {
            "base": plan, "branches": [{"name": "Museum", "plan": swapped}]})
        assert status == 200
        assert data["summary"]["all_passed"] is True
        assert _cell(data, ALT, 1)["status"] == "added"
        swapped["items"][0]["affinity"] = 0.01
        status, data = _post(port, "/api/timeline", {
            "base": plan, "branches": [{"name": "Bad", "plan": swapped}]})
        assert status == 200 and data["columns"][1]["passed"] is False
        status, data = _post(port, "/api/timeline", {"base": plan})
        assert status == 400
        status, data = _post(port, "/api/timeline", {
            "base": plan, "branches": [{"name": "X", "plan": {"items": "y"}}]})
        assert status == 400
    finally:
        server.shutdown()


def test_timeline_cli_round_trip(tmp_path, capsys):
    base, swapped = _kyoto(), _swapped()
    base_path = tmp_path / "base.json"
    branch_path = tmp_path / "museum.json"
    out_path = tmp_path / "timeline.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    branch_path.write_text(json.dumps(swapped), encoding="utf-8")
    code = cli.main(["timeline", "--base", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--out", str(out_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "timeline:" in out and "all_passed=True" in out
    assert json.loads(out_path.read_text(encoding="utf-8"))["summary"]["rows"] > 0
    swapped["items"][0]["affinity"] = 0.01
    branch_path.write_text(json.dumps(swapped), encoding="utf-8")
    assert cli.main(["timeline", "--base", str(base_path),
                     "--branch", f"Museum={branch_path}"]) == 1
    assert cli.main(["timeline", "--base", str(tmp_path / "nope.json"),
                     "--branch", f"Museum={branch_path}"]) == 2
    assert cli.main(["timeline", "--base", str(base_path),
                     "--branch", "noequals"]) == 2


def test_timeline_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "/api/timeline" in html and "View branch timeline" in html
    assert "timelinebtn" in html and "table.timeline" in html
    assert "timeline.py" in README.read_text(encoding="utf-8")
    assert "branch_timeline" in README.read_text(encoding="utf-8") or \
        "branch timeline" in README.read_text(encoding="utf-8")
    assert "visual" in SUBMISSION.read_text(encoding="utf-8").lower()
    assert "Timeline probe" in DEPLOY.read_text(encoding="utf-8")
