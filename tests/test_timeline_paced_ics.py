"""Branch timelines in paced iCalendar exports (run 54): the
branch timeline as a calendar at day granularity — one paced
event series per branch, the timeline counterpart of the
single-plan paced calendar (run 53).

Doctrine under test: pacing is recomputed per column from that
plan's schedule + primary items via pacing.plan_itinerary — a
stored ``itinerary`` field is never read back; every series'
verdict is recomputed from that branch plan's own sealed record
— a tampered branch's days read NOT VERIFIED, never PASS; days
the pacing leaves empty produce no event; a timeline with no
dated day holding recommendations in any column raises
no_dated_legs instead of inventing dates; rendering is
deterministic with paced UIDs namespaced apart from both the
leg-level timeline UIDs and the single-plan paced UIDs.
"""
import copy
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.export import (TIMELINE_FORMATS, export_timeline,
                               media_for, render_paced_ics,
                               render_timeline_ics,
                               render_timeline_paced_ics)
from tastepilot.pacing import plan_itinerary
from tastepilot.server import make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


def _base(text=DATED):
    return TastePilotAgent().run(text)


def _branches(base):
    return [{"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]


def _unfold(content):
    return content.replace("\r\n ", "")


def _paced_days(plan):
    itin = plan_itinerary(plan)
    return [(leg["location"], day) for leg in itin["legs"]
            for day in leg["days"]
            if day["date"] is not None and day["items"]]


def _expected_events(base, branches):
    plans = [base] + [b["plan"] for b in branches]
    return sum(len(_paced_days(p)) for p in plans)


def test_paced_series_per_branch_per_day():
    base = _base()
    branches = _branches(base)
    content, all_passed = export_timeline(base, branches, "ics-days")
    assert all_passed is True
    assert content.startswith("BEGIN:VCALENDAR")
    assert content.count("BEGIN:VEVENT") == _expected_events(
        base, branches)
    assert content.count("BEGIN:VEVENT") > 4  # more than leg-level
    flat = _unfold(content)
    for location, day in _paced_days(base):
        assert (f"TastePilot [As planned]: {location} · {day['date']}"
                in flat)
    for location, day in _paced_days(branches[0]["plan"]):
        assert (f"TastePilot [Museum]: {location} · {day['date']}"
                in flat)
    assert "Branch: Museum (PASS" in flat
    assert "NOT VERIFIED" not in flat
    # The branch's paced days carry the swapped-in museum; the
    # base column's days carry Gion instead.
    assert ALT in flat and "Gion" in flat


def test_each_event_carries_only_its_days_items():
    base = _base()
    branches = _branches(base)
    content = render_timeline_paced_ics(base, branches)
    events = _unfold(content).split("BEGIN:VEVENT")[1:]
    days = _paced_days(base) + _paced_days(branches[0]["plan"])
    assert len(events) == len(days)
    for (location, day), block in zip(days, events):
        assert day["date"] in block
        for ref in day["items"]:
            assert ref["name"].split(",")[0] in block or \
                ref["name"] in block
    # A name paced only on a later base day is absent from the
    # first base event.
    first_names = {i["name"] for i in days[0][1]["items"]}
    base_days = _paced_days(base)
    later_names = {i["name"] for _, d in base_days[1:]
                   for i in d["items"]}
    for name in later_names - first_names:
        assert name not in events[0]


def test_paced_timeline_tampered_branch_not_verified():
    base = _base()
    branches = _branches(base)
    branches[0]["plan"]["items"][0]["affinity"] = 0.01
    content, all_passed = export_timeline(base, branches, "ics-days")
    assert all_passed is False
    flat = _unfold(content)
    assert "Branch: Museum (FAIL" in flat
    assert "NOT VERIFIED" in flat
    assert "Branch: As planned (PASS" in flat


def test_paced_timeline_dateless_refused():
    base = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away")
    branches = [{"name": "Museum",
                 "plan": swap_plan(base, PRIMARY, ALT)}]
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_timeline(base, branches, "ics-days")
    with pytest.raises(ValueError, match="no_dated_legs"):
        render_timeline_paced_ics(base, branches)


def test_paced_timeline_deterministic_and_uids_apart():
    base = _base()
    branches = _branches(base)
    first, _ = export_timeline(base, branches, "ics-days")
    second, _ = export_timeline(copy.deepcopy(base),
                                copy.deepcopy(branches), "ics-days")
    assert first == second
    uids = re.findall(r"UID:([0-9a-f]+)@tastepilot-timeline", first)
    assert len(uids) == len(set(uids)) == _expected_events(
        base, branches)
    # Regenerating from scratch keeps byte-identical output.
    base2 = _base()
    third, _ = export_timeline(base2, _branches(base2), "ics-days")
    assert first == third
    # Namespaced apart from the leg-level timeline calendar and
    # from the single-plan paced calendar.
    leg_uids = set(re.findall(r"UID:([0-9a-f]+)@tastepilot-timeline",
                              render_timeline_ics(base, branches)))
    assert not (set(uids) & leg_uids)
    single_uids = set(re.findall(r"UID:([0-9a-f]+)@tastepilot",
                                 render_paced_ics(base)))
    assert not (set(uids) & single_uids)


def test_paced_timeline_rfc_shape_and_format_registered():
    base = _base()
    content, _ = export_timeline(base, _branches(base), "ics-days")
    assert content.endswith("\r\n")
    for line in content.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert "ics-days" in TIMELINE_FORMATS
    assert media_for("ics-days") == ("text/calendar; charset=utf-8",
                                     "ics")
    with pytest.raises(ValueError, match="unsupported"):
        export_timeline(base, _branches(base), "html")


def test_stored_itinerary_field_never_read_back():
    base = _base()
    base["itinerary"] = {"legs": [{"location": "Kyoto", "days": [
        {"date": "1999-01-01", "items": [
            {"name": "evil.example item", "domain": "place",
             "location": "Kyoto", "affinity": 1.0}]}]}]}
    content = render_timeline_paced_ics(base, _branches(base))
    assert "evil.example" not in content
    assert "19990101" not in content


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


def test_paced_timeline_endpoint_over_http():
    server, port = _serve(TastePilotAgent())
    try:
        status, _h, body = _post_raw(port, "/api/plan",
                                     {"text": DATED})
        assert status == 200
        plan = json.loads(body)
        swapped = swap_plan(plan, PRIMARY, ALT)
        payload = {"base": plan,
                   "branches": [{"name": "Museum", "plan": swapped}]}
        status, headers, body = _post_raw(
            port, "/api/export-timeline?format=ics-days", payload)
        assert status == 200
        assert "tastepilot-timeline.ics" in \
            headers.get("Content-Disposition", "")
        assert "text/calendar" in headers.get("Content-Type", "")
        assert headers.get("Cache-Control") == "no-store"
        assert body.count("BEGIN:VEVENT") == _expected_events(
            plan, payload["branches"])
        # Tampered branch: still 200, its days NOT VERIFIED.
        swapped["items"][0]["affinity"] = 0.01
        status, _h, body = _post_raw(
            port, "/api/export-timeline?format=ics-days", payload)
        assert status == 200 and \
            "NOT VERIFIED" in body.replace("\r\n ", "")
        # Dateless timeline -> categorised 400.
        status, _h, body = _post_raw(port, "/api/plan", {
            "text": "a slow weekend in Kyoto, I love Spirited Away"})
        plan2 = json.loads(body)
        payload2 = {"base": plan2, "branches": [
            {"name": "Museum",
             "plan": swap_plan(plan2, PRIMARY, ALT)}]}
        status, _h, body = _post_raw(
            port, "/api/export-timeline?format=ics-days", payload2)
        assert status == 400 and "no_dated_legs" in body
        # Unknown format -> 400.
        status, _h, _b = _post_raw(
            port, "/api/export-timeline?format=html", payload)
        assert status == 400
    finally:
        server.shutdown()


def test_paced_timeline_cli_round_trip(tmp_path, capsys):
    base = _base()
    branches = _branches(base)
    base_path = tmp_path / "base.json"
    branch_path = tmp_path / "museum.json"
    out_path = tmp_path / "timeline-days.ics"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    branch_path.write_text(json.dumps(branches[0]["plan"]),
                           encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics-days", "--out", str(out_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "timeline=True" in out and "verified=True" in out
    content = out_path.read_text(encoding="utf-8")
    assert content.count("BEGIN:VEVENT") == _expected_events(
        base, branches)
    # Tampered branch -> exit 1, still written, NOT VERIFIED inside.
    bad = copy.deepcopy(branches[0]["plan"])
    bad["items"][0]["affinity"] = 0.01
    branch_path.write_text(json.dumps(bad), encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics-days", "--out", str(out_path)])
    assert code == 1
    assert "NOT VERIFIED" in out_path.read_text(
        encoding="utf-8").replace("\r\n ", "")
    # Dateless base -> caller error exit 2, no traceback.
    plain = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away")
    base_path.write_text(json.dumps(plain), encoding="utf-8")
    branch_path.write_text(json.dumps(
        swap_plan(plain, PRIMARY, ALT)), encoding="utf-8")
    code = cli.main(["export", "--plan", str(base_path),
                     "--branch", f"Museum={branch_path}",
                     "--format", "ics-days"])
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_paced_timeline_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Download timeline (iCalendar by day)" in html
    assert "'ics-days'" in html
    readme = README.read_text(encoding="utf-8")
    assert "one paced" in readme and "ics-days" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "paced iCalendar" in submission
    assert "Paced timeline calendar probe" in \
        DEPLOY.read_text(encoding="utf-8")
