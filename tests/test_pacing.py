"""Day-by-day pacing (run 52): items distributed over dated legs."""
import copy
import json
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.pacing import plan_itinerary, _distribute, _leg_days
from tastepilot.export import render_markdown, render_text

APP = Path(__file__).resolve().parent.parent
REF = date(2026, 10, 10)


def _dated_plan():
    return TastePilotAgent().run(
        "a trip: Kyoto Nov 3-5, then Paris Nov 6-8, I love Spirited Away",
        reference_date=REF)


def test_leg_days_range_and_guards():
    assert _leg_days("2026-11-03", "2026-11-05") == [
        "2026-11-03", "2026-11-04", "2026-11-05"]
    assert _leg_days("2026-11-03", None) == ["2026-11-03"]
    assert _leg_days(None, "2026-11-05") is None
    assert _leg_days("2026-11-05", "2026-11-03") is None
    assert _leg_days("not-a-date", None) is None


def test_distribute_even_and_remainder_first():
    assert [len(c) for c in _distribute(list(range(7)), 3)] == [3, 2, 2]
    assert [len(c) for c in _distribute(list(range(6)), 3)] == [2, 2, 2]
    assert [len(c) for c in _distribute([], 2)] == [0, 0]
    flat = [x for c in _distribute(list(range(7)), 3) for x in c]
    assert flat == list(range(7))


def test_dated_multi_leg_pacing_covers_every_item_once():
    plan = _dated_plan()
    itin = plan_itinerary(plan)
    assert itin["dated_days"] == 6
    assert itin["total_items"] == len(plan["items"])
    seen = []
    for leg in itin["legs"]:
        assert [d["date"] for d in leg["days"]] == sorted(
            d["date"] for d in leg["days"])
        for day in leg["days"]:
            seen += [i["name"] for i in day["items"]]
    assert sorted(seen) == sorted(
        i["entity"]["name"] for i in plan["items"])
    # Consecutive chunks: no item repeats, order preserved per leg.
    kyoto = itin["legs"][0]
    kyoto_names = [i["name"] for d in kyoto["days"] for i in d["items"]]
    expected = [i["entity"]["name"] for i in plan["items"]
                if i["location"] == "Kyoto"]
    assert kyoto_names == expected


def test_undated_leg_is_unscheduled_bucket():
    plan = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue")
    itin = plan_itinerary(plan)
    assert itin["dated_days"] == 0
    assert len(itin["legs"]) == 1
    assert len(itin["legs"][0]["days"]) == 1
    assert itin["legs"][0]["days"][0]["date"] is None
    assert len(itin["legs"][0]["days"][0]["items"]) == len(plan["items"])


def test_end_before_start_never_invents_days():
    plan = {"location": "Kyoto",
            "schedule": [{"location": "Kyoto", "start": "2026-11-05",
                          "end": "2026-11-03"}],
            "items": [{"entity": {"name": "Gion, Kyoto"}, "domain": "place",
                       "location": "Kyoto", "affinity": 0.9}]}
    itin = plan_itinerary(plan)
    assert itin["dated_days"] == 0
    assert itin["legs"][0]["days"][0]["date"] is None


def test_agent_field_equals_recomputation_and_checker_untouched():
    plan = _dated_plan()
    assert plan["itinerary"] == plan_itinerary(plan)
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    # Alternatives are swap options, never paced.
    paced = {i["name"] for leg in plan["itinerary"]["legs"]
             for d in leg["days"] for i in d["items"]}
    assert paced == {i["entity"]["name"] for i in plan["items"]}


def test_determinism_and_no_mutation():
    plan = _dated_plan()
    snapshot = copy.deepcopy(plan)
    a, b = plan_itinerary(plan), plan_itinerary(plan)
    assert a == b and plan == snapshot


def test_stored_itinerary_field_never_read_back():
    plan = _dated_plan()
    plan["itinerary"] = {"legs": [{"location": "Kyoto", "days": [
        {"date": "1999-01-01", "items": [
            {"name": "evil.example item", "domain": "place",
             "location": "Kyoto", "affinity": 1.0}]}]}]}
    assert "evil.example" not in render_markdown(plan)
    assert "evil.example" not in render_text(plan)
    assert "1999-01-01" not in render_markdown(plan)


def test_exports_carry_day_by_day_section():
    plan = _dated_plan()
    md = render_markdown(plan)
    assert "## Day-by-day itinerary" in md
    assert "Kyoto · 2026-11-03:" in md
    assert "Paris · 2026-11-08:" in md or "Paris · 2026-11-07:" in md
    txt = render_text(plan)
    assert "Day-by-day itinerary:" in txt
    assert "Kyoto · 2026-11-03:" in txt


def test_undated_export_labels_unscheduled():
    plan = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue")
    assert "unscheduled" in render_markdown(plan)


def test_bad_plan_shapes():
    assert plan_itinerary(None) == {"legs": [], "dated_days": 0,
                                     "total_items": 0}
    assert plan_itinerary({}) == {"legs": [], "dated_days": 0,
                                  "total_items": 0}
    assert plan_itinerary({"items": "nope"})["total_items"] == 0


def test_cli_itinerary_lines(capsys):
    from tastepilot import cli
    rc = cli.main(["plan", "--mode", "mock",
                   "a trip: Kyoto Nov 3-5, I love Spirited Away"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "itinerary: Kyoto · 2026-11-03:" in out


def test_server_plan_carries_itinerary_and_ui_wiring():
    from tastepilot.server import make_handler
    from http.server import ThreadingHTTPServer
    import threading
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(TastePilotAgent()))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/plan",
            data=json.dumps({"text": "a trip: Kyoto Nov 3-5, "
                                     "I love Spirited Away"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            plan = json.loads(resp.read())
        assert plan["itinerary"]["dated_days"] == 3
        assert plan["itinerary"]["total_items"] == len(plan["items"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            html = resp.read().decode()
        assert "Day-by-day itinerary" in html
        assert "itineraryday" in html
    finally:
        server.shutdown()


def test_docs_describe_pacing():
    readme = (APP / "README.md").read_text()
    submission = (APP / "SUBMISSION.md").read_text()
    assert "pacing.py" in readme
    assert "Day-by-day" in readme or "day-by-day" in readme
    assert "day-by-day" in submission.lower()
