"""Branch timelines in take-away exports: the visual branch
timeline (run 34) rendered into Markdown / plain-text exports.

Doctrine under test: an export's banner and every column verdict
are recomputed from each plan's own sealed record — a tampered
branch exports as a FAIL column under a NOT VERIFIED banner, never
clean; rendering is deterministic; iCalendar is refused
categorically for a timeline; malformed input is a categorised
ValueError / 400, never a traceback."""
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
from tastepilot.export import export_timeline, render_timeline_markdown, \
    render_timeline_text
from tastepilot.server import make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


def _base():
    return TastePilotAgent().run(KYOTO)


def _branches(base=None):
    base = base or _base()
    return [{"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]


def test_timeline_markdown_export_pass_grid():
    base = _base()
    content, all_passed = export_timeline(base, _branches(base), "markdown")
    assert all_passed is True
    assert content.startswith("# TastePilot branch timeline")
    assert "Verification: PASS" in content
    assert "NOT VERIFIED" not in content
    assert "| Recommendation | Domain | As planned | Museum |" in content
    assert "Leg: Kyoto" in content
    assert ALT in content and "added" in content
    assert PRIMARY in content and "removed" in content
    assert "kept (0.90)" in content  # Kyoto destination, base column


def test_timeline_text_export_no_markdown():
    base = _base()
    content, all_passed = export_timeline(base, _branches(base), "text")
    assert all_passed is True
    assert "Verification: PASS" in content
    assert "**" not in content and "| " not in content.splitlines()[5]
    assert "Museum: added" in content
    assert "As planned" in content


def test_timeline_tampered_branch_exports_not_verified():
    base = _base()
    branches = _branches(base)
    branches[0]["plan"]["items"][0]["affinity"] = 0.01
    for fmt in ("markdown", "text"):
        content, all_passed = export_timeline(base, branches, fmt)
        assert all_passed is False
        assert "NOT VERIFIED" in content
        assert "Museum" in content and "FAIL" in content


def test_timeline_rescored_cell_shows_delta():
    base = _base()
    other = copy.deepcopy(base)
    item = next(i for i in other["items"]
                if i["entity"]["name"] == "Kyoto")
    item["affinity"] = 0.5
    content, all_passed = export_timeline(
        base, [{"name": "Edited", "plan": other}], "markdown")
    assert all_passed is False
    assert "rescored (0.50, -0.40)" in content


def test_timeline_export_deterministic():
    base = _base()
    branches = _branches(base)
    for fmt in ("markdown", "text"):
        first, _ = export_timeline(base, branches, fmt)
        second, _ = export_timeline(copy.deepcopy(base),
                                    copy.deepcopy(branches), fmt)
        assert first == second


def test_timeline_export_ics_refused():
    base = _base()
    with pytest.raises(ValueError, match="unsupported"):
        export_timeline(base, _branches(base), "ics")
    with pytest.raises(ValueError, match="unsupported"):
        export_timeline(base, _branches(base), "html")


def test_timeline_export_shape_gate():
    base = _base()
    with pytest.raises(ValueError):
        export_timeline(base, [], "markdown")
    with pytest.raises(ValueError):
        render_timeline_markdown({"items": "x"}, _branches(base))
    with pytest.raises(ValueError):
        render_timeline_text(base, [{"name": "A", "plan": {}},
                                    {"name": "A", "plan": {}}])


def _serve(agent):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post_raw(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode()


def test_timeline_export_endpoint_over_http():
    server, port = _serve(TastePilotAgent())
    try:
        status, _h, body = _post_raw(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        plan = json.loads(body)
        swapped = swap_plan(plan, PRIMARY, ALT)
        payload = {"base": plan,
                   "branches": [{"name": "Museum", "plan": swapped}]}
        status, headers, body = _post_raw(
            port, "/api/export-timeline?format=markdown", payload)
        assert status == 200
        assert "tastepilot-timeline.md" in \
            headers.get("Content-Disposition", "")
        assert headers.get("Cache-Control") == "no-store"
        assert "Verification: PASS" in body and "added" in body
        status, _h, body = _post_raw(
            port, "/api/export-timeline?format=text", payload)
        assert status == 200 and "Museum: added" in body
        # Tampered branch: still a 200 download, FAIL column.
        swapped["items"][0]["affinity"] = 0.01
        status, _h, body = _post_raw(
            port, "/api/export-timeline?format=markdown", payload)
        assert status == 200 and "NOT VERIFIED" in body
        # ics and malformed payloads are categorised 400s.
        status, _h, _b = _post_raw(
            port, "/api/export-timeline?format=ics", payload)
        assert status == 400
        status, _h, _b = _post_raw(port, "/api/export-timeline",
                                   {"base": plan})
        assert status == 400
    finally:
        server.shutdown()


def test_timeline_export_cli_round_trip(tmp_path, capsys):
    base = _base()
    branches = _branches(base)
    base_path = tmp_path / "base.json"
    branch_path = tmp_path / "museum.json"
    out_path = tmp_path / "timeline.md"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    branch_path.write_text(json.dumps(branches[0]["plan"]),
                           encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "markdown", "--out", str(out_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "timeline=True" in out and "verified=True" in out
    content = out_path.read_text(encoding="utf-8")
    assert "Verification: PASS" in content and ALT in content
    # Single-plan export is unchanged when no --branch is given.
    code = cli.main(["export", "--plan", str(base_path),
                     "--format", "markdown", "--out", str(out_path)])
    assert code == 0
    assert "branch timeline" not in \
        out_path.read_text(encoding="utf-8").splitlines()[0]
    # Tampered branch -> exit 1, still written with NOT VERIFIED.
    bad = copy.deepcopy(branches[0]["plan"])
    bad["items"][0]["affinity"] = 0.01
    branch_path.write_text(json.dumps(bad), encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "text", "--out", str(out_path)])
    assert code == 1
    assert "NOT VERIFIED" in out_path.read_text(encoding="utf-8")
    # ics with branches and a bad spec are caller errors (exit 2).
    assert cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics"]) == 2
    assert cli.main(["export", "--plan", str(base_path),
                     "--branch", "noequals"]) == 2


def test_timeline_export_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "/api/export-timeline" in html
    assert "Download timeline (Markdown)" in html
    assert "timelineexportbtn" in html
    readme = README.read_text(encoding="utf-8")
    assert "export_timeline" in readme or "branch-timeline export" in readme
    assert "Timeline export probe" in DEPLOY.read_text(encoding="utf-8")
    assert "take-away exports" in SUBMISSION.read_text(
        encoding="utf-8").lower()
