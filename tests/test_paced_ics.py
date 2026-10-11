"""Paced iCalendar exports (run 53): one event per paced day.

Doctrine under test: the ``ics-days`` calendar is recomputed from
the plan's schedule + primary items via pacing.plan_itinerary —
a stored ``itinerary`` field is never read back; every event
carries the recomputed verdict banner; days the pacing leaves
empty and undated legs produce no event; a plan with no dated day
fails categorically instead of inventing dates; paced UIDs are
stable and namespaced apart from the leg-level calendar's.
"""
import copy
import json
import re
import subprocess
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.export import (FORMATS, export_plan, media_for,
                               render_ics, render_paced_ics)
from tastepilot.pacing import plan_itinerary

APP = Path(__file__).resolve().parent.parent
SAMPLE = APP / "samples" / "plan_weekend_kyoto.json"
REF = date(2026, 10, 10)


def _dated_plan():
    return TastePilotAgent().run(
        "a trip: Kyoto Nov 3-5, then Paris Nov 6-8, I love Spirited Away",
        reference_date=REF)


def _unfold(content):
    return content.replace("\r\n ", "")


def _events(content):
    return _unfold(content).split("BEGIN:VEVENT")[1:]


def _paced_days(plan):
    itin = plan_itinerary(plan)
    return [(leg["location"], day) for leg in itin["legs"]
            for day in leg["days"]
            if day["date"] is not None and day["items"]]


def test_one_event_per_paced_day_with_items():
    plan = _dated_plan()
    content, verified = export_plan(plan, "ics-days")
    assert verified is True
    days = _paced_days(plan)
    assert content.count("BEGIN:VEVENT") == len(days)
    for location, day in days:
        assert (f"SUMMARY:TastePilot: {location} · {day['date']}"
                in _unfold(content))
        start = date.fromisoformat(day["date"])
        assert (f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}"
                in content)
        assert (f"DTEND;VALUE=DATE:"
                f"{(start + timedelta(days=1)).strftime('%Y%m%d')}"
                in content)
    assert "Verification: PASS" in _unfold(content).replace("\\,", ",")
    for line in content.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_each_event_carries_only_its_days_items():
    plan = _dated_plan()
    days = _paced_days(plan)
    assert len(days) >= 2
    events = _events(render_paced_ics(plan))
    assert len(events) == len(days)
    for (location, day), block in zip(days, events):
        for ref in day["items"]:
            assert ref["name"].split(",")[0] in block or \
                ref["name"] in block
    first_names = {i["name"] for i in days[0][1]["items"]}
    later_names = {i["name"] for _, d in days[1:] for i in d["items"]}
    for name in later_names - first_names:
        assert name not in events[0]


def test_paced_uids_stable_unique_and_apart_from_leg_uids():
    plan = _dated_plan()
    content = render_paced_ics(plan)
    uids = re.findall(r"UID:([0-9a-f]+)@tastepilot", content)
    assert len(uids) == len(set(uids)) == len(_paced_days(plan))
    assert render_paced_ics(_dated_plan()) == content
    leg_uids = set(re.findall(r"UID:([0-9a-f]+)@tastepilot",
                              render_ics(plan)))
    assert not (set(uids) & leg_uids)


def test_empty_days_get_no_event():
    plan = {"request": "one stop", "location": "Kyoto",
            "schedule": [{"location": "Kyoto", "start": "2026-11-03",
                          "end": "2026-11-05"}],
            "items": [{"entity": {"id": "e1", "name": "Gion, Kyoto"},
                       "domain": "place", "location": "Kyoto",
                       "affinity": 0.9}],
            "evidence": {"entries": [], "verdict": None}}
    content = render_paced_ics(plan)
    assert content.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:20261103" in content


def test_dateless_plan_is_categorised_error():
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="no_dated_legs"):
        render_paced_ics(sample)
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_plan(sample, "ics-days")


def test_tampered_plan_days_carry_not_verified():
    plan = _dated_plan()
    plan["items"][0]["affinity"] += 0.05
    content, verified = export_plan(plan, "ics-days")
    assert verified is False
    assert "NOT VERIFIED" in _unfold(content)


def test_stored_itinerary_field_never_read_back():
    plan = _dated_plan()
    plan["itinerary"] = {"legs": [{"location": "Kyoto", "days": [
        {"date": "1999-01-01", "items": [
            {"name": "evil.example item", "domain": "place",
             "location": "Kyoto", "affinity": 1.0}]}]}]}
    content = render_paced_ics(plan)
    assert "evil.example" not in content
    assert "19990101" not in content


def test_alternatives_are_never_paced_into_events():
    plan = _dated_plan()
    names = {i["name"] for _, d in _paced_days(plan) for i in d["items"]}
    assert names == {i["entity"]["name"] for i in plan["items"]}


def test_format_registered_with_calendar_media():
    assert "ics-days" in FORMATS
    assert media_for("ics-days") == ("text/calendar; charset=utf-8",
                                      "ics")
    # The leg-level format is unchanged.
    assert _dated_plan() and render_ics(_dated_plan()).count(
        "BEGIN:VEVENT") == 2


def _cli(*argv):
    return subprocess.run([sys.executable, "-m", "tastepilot", *argv],
                          cwd=APP, capture_output=True, text=True,
                          timeout=60)


def test_cli_paced_export_and_dateless_error(tmp_path):
    dated = tmp_path / "dated.json"
    plan = _dated_plan()
    dated.write_text(json.dumps(plan), encoding="utf-8")
    run = _cli("export", "--plan", str(dated), "--format", "ics-days")
    assert run.returncode == 0, run.stderr
    assert run.stdout.count("BEGIN:VEVENT") == len(_paced_days(plan))
    run = _cli("export", "--plan", str(SAMPLE), "--format", "ics-days")
    assert run.returncode == 2 and "no_dated_legs" in run.stderr
    assert "Traceback" not in run.stderr


def test_server_paced_export_and_ui_option():
    from http.server import ThreadingHTTPServer
    import threading
    from tastepilot.server import make_handler
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(TastePilotAgent()))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/export?format=ics-days",
            data=json.dumps(_dated_plan()).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            assert resp.headers.get_content_type() == "text/calendar"
            body = resp.read().decode()
        assert body.count("BEGIN:VEVENT") == len(
            _paced_days(_dated_plan()))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            html = resp.read().decode()
        assert 'value="ics-days"' in html
    finally:
        server.shutdown()


def test_docs_describe_paced_ics():
    readme = (APP / "README.md").read_text()
    submission = (APP / "SUBMISSION.md").read_text()
    assert "ics-days" in readme
    assert "ics-days" in submission
