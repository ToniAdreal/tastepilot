"""Date-aware legs: per-leg dates, ordering, and pacing constraints.

All parsing is deterministic against an explicit reference date
(2026-10-10, a Saturday) — the agent defaults the reference to today,
tests always pass one.
"""
import re
from datetime import date
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.parser import extract_dates, parse_request
from tastepilot.qloo import MockQlooClient

REF = date(2026, 10, 10)  # Saturday
ROOT = Path(__file__).resolve().parents[1]


def legs(text):
    p = parse_request(text, reference_date=REF)
    return p, {d["location"]: d for d in p.leg_dates}


def test_explicit_forms_iso_month_name_and_day_first():
    _, by_loc = legs("Kyoto on 2026-11-03")
    assert by_loc["Kyoto"]["start"] == "2026-11-03"
    _, by_loc = legs("Kyoto on November 3rd, 2026")
    assert by_loc["Kyoto"]["start"] == "2026-11-03"
    _, by_loc = legs("Kyoto on 3 November 2026")
    assert by_loc["Kyoto"]["start"] == "2026-11-03"


def test_yearless_date_rolls_forward_past_reference():
    _, by_loc = legs("Kyoto on Nov 3")          # later this year
    assert by_loc["Kyoto"]["start"] == "2026-11-03"
    _, by_loc = legs("Kyoto on Oct 3")          # already passed -> next year
    assert by_loc["Kyoto"]["start"] == "2027-10-03"
    _, by_loc = legs("Kyoto in May 2027")       # a bare month+year is not a date
    assert by_loc["Kyoto"]["start"] is None


def test_ranges_from_to_dash_and_same_month_shorthand():
    _, by_loc = legs("Kyoto from Nov 3 to Nov 5")
    assert (by_loc["Kyoto"]["start"], by_loc["Kyoto"]["end"],
            by_loc["Kyoto"]["nights"]) == ("2026-11-03", "2026-11-05", 2)
    _, by_loc = legs("Kyoto Nov 3 \u2013 Nov 5")
    assert by_loc["Kyoto"]["end"] == "2026-11-05"
    _, by_loc = legs("Kyoto Nov 3-5")
    assert (by_loc["Kyoto"]["start"], by_loc["Kyoto"]["end"]) == \
        ("2026-11-03", "2026-11-05")


def test_duration_nights_and_days():
    _, by_loc = legs("Kyoto 2026-11-03 for 3 nights")
    assert by_loc["Kyoto"]["end"] == "2026-11-06"
    assert by_loc["Kyoto"]["nights"] == 3
    _, by_loc = legs("Kyoto 2026-11-03 for 3 days")
    assert by_loc["Kyoto"]["end"] == "2026-11-05"


def test_relative_dates_against_reference():
    _, by_loc = legs("Kyoto today, Paris tomorrow")
    assert by_loc["Kyoto"]["start"] == "2026-10-10"
    assert by_loc["Paris"]["start"] == "2026-10-11"
    _, by_loc = legs("Kyoto in 5 days")
    assert by_loc["Kyoto"]["start"] == "2026-10-15"
    # REF is a Saturday: this Friday = Oct 16, next Friday = Oct 23.
    _, by_loc = legs("Kyoto on Friday")
    assert by_loc["Kyoto"]["start"] == "2026-10-16"
    _, by_loc = legs("Kyoto next Friday")
    assert by_loc["Kyoto"]["start"] == "2026-10-23"


def test_bare_weekday_and_scenario_words_are_not_dates():
    p, by_loc = legs("a first date in New York, watch the film Friday")
    assert by_loc["New York"]["start"] is None
    assert p.date_warnings == []
    assert extract_dates("a first date, no calendar", REF) == []


def test_per_leg_assignment_and_date_before_any_city():
    _, by_loc = legs("Nov 1 start: Kyoto Nov 3-5, then Paris Nov 6-8")
    assert (by_loc["Kyoto"]["start"], by_loc["Kyoto"]["end"]) == \
        ("2026-11-01", "2026-11-05")  # the pre-city date joins leg 1
    assert (by_loc["Paris"]["start"], by_loc["Paris"]["end"]) == \
        ("2026-11-06", "2026-11-08")
    # A later re-mention re-assigns: the date after "Kyoto" (mentioned
    # again after Paris) belongs to Kyoto, not Paris.
    _, by_loc = legs("Kyoto and Paris, back to Kyoto on Nov 9")
    assert by_loc["Kyoto"]["start"] == "2026-11-09"
    assert by_loc["Paris"]["start"] is None


def test_warnings_out_of_order_overlap_end_before_start():
    p, _ = legs("Kyoto Nov 10, then Paris Nov 5")
    assert [w["code"] for w in p.date_warnings] == ["out_of_order"]
    p, _ = legs("Kyoto Nov 3 to Nov 6, Paris Nov 5")
    assert [w["code"] for w in p.date_warnings] == ["overlap"]
    p, _ = legs("Kyoto from Nov 8 to Nov 3")
    assert p.date_warnings[0]["code"] == "end_before_start"
    # A clean chronological trip warns about nothing.
    p, _ = legs("Kyoto Nov 3-5, then Paris Nov 6-8")
    assert p.date_warnings == []


def test_no_date_request_unchanged_shape():
    p, by_loc = legs("a slow weekend in Kyoto, I love Spirited Away")
    assert by_loc["Kyoto"] == {"location": "Kyoto", "start": None,
                               "end": None, "nights": None, "dates": []}
    assert p.date_warnings == []


def test_agent_plan_carries_schedule_legs_and_warnings():
    agent = TastePilotAgent(MockQlooClient())
    plan = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
                     "I love Spirited Away", reference_date=REF)
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    sched = {s["location"]: s for s in plan["schedule"]}
    assert sched["Kyoto"]["nights"] == 2 and sched["Paris"]["start"] == "2026-11-06"
    leg_view = {leg["location"]: leg for leg in plan["legs"]}
    assert leg_view["Kyoto"]["start"] == "2026-11-03"
    assert leg_view["Paris"]["end"] == "2026-11-08"
    assert plan["date_warnings"] == []
    warned = agent.run("Kyoto Nov 10, then Paris Nov 5, I love Amelie",
                       reference_date=REF)
    assert [w["code"] for w in warned["date_warnings"]] == ["out_of_order"]
    assert warned["checker"]["passed"]  # warnings never block the plan


def test_dates_never_reach_qloo_calls():
    """Dates are composition constraints: no sealed Qloo request may
    carry a date — the evidence invariant stays exactly as before."""
    agent = TastePilotAgent(MockQlooClient())
    plan = agent.run("Kyoto Nov 3-5, then Paris Nov 6-8, I love Amelie",
                     reference_date=REF)
    for entry in plan["evidence"]["entries"]:
        payload = str(entry)
        assert "2026-11" not in payload or entry["kind"] != "qloo_call", entry


def test_ui_carries_date_wiring():
    html = (ROOT / "tastepilot" / "static" / "index.html").read_text()
    assert "date_warnings" in html and "night" in html
    assert "legLabel" in html


def test_readme_documents_date_rules():
    readme = (ROOT / "README.md").read_text()
    assert re.search(r"date-aware", readme, re.IGNORECASE)
