"""Branch timelines in iCalendar exports: the branch timeline
(run 34) as a calendar file — one event series per branch.

Doctrine under test: every series' verdict is recomputed from
that branch plan's own sealed record — a tampered branch's events
read NOT VERIFIED, never PASS; a timeline with no dated leg in
any column raises no_dated_legs instead of inventing dates;
rendering is deterministic with content-derived UIDs that are
unique per branch and stable across regenerations."""
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
from tastepilot.export import export_timeline, render_timeline_ics
from tastepilot.server import make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
SINGLE = ("a slow weekend in Kyoto Nov 3-5, "
          "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


def _base(text=DATED):
    return TastePilotAgent().run(text)


def _branches(base):
    return [{"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]


def _unfold(content):
    return content.replace("\r\n ", "")


def test_timeline_ics_one_series_per_branch():
    base = _base()
    content, all_passed = export_timeline(base, _branches(base), "ics")
    assert all_passed is True
    assert content.startswith("BEGIN:VCALENDAR")
    # 2 columns (base + Museum) x 2 dated legs (Kyoto, Paris).
    assert content.count("BEGIN:VEVENT") == 4
    flat = _unfold(content)
    assert "TastePilot [As planned]: Kyoto" in flat
    assert "TastePilot [Museum]: Kyoto" in flat
    assert "TastePilot [Museum]: Paris" in flat
    assert "Branch: Museum (PASS" in flat
    assert "NOT VERIFIED" not in flat
    # The branch's Kyoto events list the swapped-in museum, and its
    # base-column events list Gion instead (ICS-escaped as Gion\,).
    assert ALT in flat and "Gion" in flat


def test_timeline_ics_single_leg_dated():
    base = _base(SINGLE)
    content, all_passed = export_timeline(base, _branches(base), "ics")
    assert all_passed is True
    assert content.count("BEGIN:VEVENT") == 2
    assert "DTSTART;VALUE=DATE:20261103" in content
    assert "DTEND;VALUE=DATE:20261106" in content  # end Nov 5 + 1, exclusive


def test_timeline_ics_tampered_branch_not_verified():
    base = _base()
    branches = _branches(base)
    branches[0]["plan"]["items"][0]["affinity"] = 0.01
    content, all_passed = export_timeline(base, branches, "ics")
    assert all_passed is False
    flat = _unfold(content)
    assert "Branch: Museum (FAIL" in flat
    assert "NOT VERIFIED" in flat
    # The base column's own series still recomputes as PASS.
    assert "Branch: As planned (PASS" in flat


def test_timeline_ics_dateless_refused():
    base = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away")
    branches = [{"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_timeline(base, branches, "ics")
    with pytest.raises(ValueError, match="no_dated_legs"):
        render_timeline_ics(base, branches)


def test_timeline_ics_deterministic_and_uids_stable():
    base = _base()
    branches = _branches(base)
    first, _ = export_timeline(base, branches, "ics")
    second, _ = export_timeline(copy.deepcopy(base),
                                copy.deepcopy(branches), "ics")
    assert first == second
    uids = [ln for ln in first.split("\r\n") if ln.startswith("UID:")]
    assert len(uids) == 4 and len(set(uids)) == 4
    # Regenerating the same request from scratch (new evidence
    # record, new wall-clock) yields byte-identical ICS.
    base2 = _base()
    third, _ = export_timeline(base2, _branches(base2), "ics")
    assert first == third


def test_timeline_ics_rfc_shape():
    base = _base()
    content, _ = export_timeline(base, _branches(base), "ics")
    assert content.endswith("\r\n")
    for line in content.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


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


def test_timeline_ics_endpoint_over_http():
    server, port = _serve(TastePilotAgent())
    try:
        status, _h, body = _post_raw(port, "/api/plan", {"text": DATED})
        assert status == 200
        plan = json.loads(body)
        swapped = swap_plan(plan, PRIMARY, ALT)
        payload = {"base": plan,
                   "branches": [{"name": "Museum", "plan": swapped}]}
        status, headers, body = _post_raw(
            port, "/api/export-timeline?format=ics", payload)
        assert status == 200
        assert "tastepilot-timeline.ics" in \
            headers.get("Content-Disposition", "")
        assert "text/calendar" in headers.get("Content-Type", "")
        assert headers.get("Cache-Control") == "no-store"
        assert body.count("BEGIN:VEVENT") == 4
        # Tampered branch: still 200, its series NOT VERIFIED.
        swapped["items"][0]["affinity"] = 0.01
        status, _h, body = _post_raw(
            port, "/api/export-timeline?format=ics", payload)
        assert status == 200 and "NOT VERIFIED" in body.replace("\r\n ", "")
        # Dateless timeline -> categorised 400.
        status, _h, body = _post_raw(port, "/api/plan", {
            "text": "a slow weekend in Kyoto, I love Spirited Away"})
        plan2 = json.loads(body)
        payload2 = {"base": plan2, "branches": [
            {"name": "Museum", "plan": swap_plan(plan2, PRIMARY, ALT)}]}
        status, _h, body = _post_raw(
            port, "/api/export-timeline?format=ics", payload2)
        assert status == 400 and "no_dated_legs" in body
    finally:
        server.shutdown()


def test_timeline_ics_cli_round_trip(tmp_path, capsys):
    base = _base()
    branches = _branches(base)
    base_path = tmp_path / "base.json"
    branch_path = tmp_path / "museum.json"
    out_path = tmp_path / "timeline.ics"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    branch_path.write_text(json.dumps(branches[0]["plan"]),
                           encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics", "--out", str(out_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "timeline=True" in out and "verified=True" in out
    content = out_path.read_text(encoding="utf-8")
    assert content.count("BEGIN:VEVENT") == 4
    # Tampered branch -> exit 1, still written, NOT VERIFIED inside.
    bad = copy.deepcopy(branches[0]["plan"])
    bad["items"][0]["affinity"] = 0.01
    branch_path.write_text(json.dumps(bad), encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics", "--out", str(out_path)])
    assert code == 1
    assert "NOT VERIFIED" in out_path.read_text(
        encoding="utf-8").replace("\r\n ", "")
    # Dateless base -> caller error exit 2.
    plain = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away")
    base_path.write_text(json.dumps(plain), encoding="utf-8")
    branch_path.write_text(json.dumps(
        swap_plan(plain, PRIMARY, ALT)), encoding="utf-8")
    assert cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics"]) == 2


def test_timeline_ics_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Download timeline (iCalendar)" in html
    readme = README.read_text(encoding="utf-8")
    assert "one event series per branch" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "one event series per branch" in submission
    assert "live calendar subscriptions" in submission
    assert "Timeline ICS probe" in DEPLOY.read_text(encoding="utf-8")
