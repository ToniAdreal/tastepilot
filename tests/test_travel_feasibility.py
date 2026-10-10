"""Inter-leg travel-time feasibility: offline door-to-door transfer
estimates checked against the date-aware schedule's windows.

Deterministic throughout: fixed coordinates, a fixed reference date
(2026-10-10), and no network. Warnings are surfaced, never repaired,
and never block a plan — the same doctrine as date_warnings.
"""
import re
from datetime import date
from pathlib import Path

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.parser import parse_request
from tastepilot.qloo import MockQlooClient
from tastepilot.travel import (CITY_COORDS, assess_transfers,
                               estimate_transfer, haversine_km)

REF = date(2026, 10, 10)
ROOT = Path(__file__).resolve().parents[1]


def leg(location, start=None, end=None):
    return {"location": location, "start": start, "end": end,
            "nights": None, "dates": []}


def test_haversine_known_distances():
    assert 35 < haversine_km("Kyoto", "Osaka") < 55
    assert 9_400 < haversine_km("Kyoto", "Paris") < 9_800
    assert haversine_km("Kyoto", "Paris") == haversine_km("Paris", "Kyoto")
    assert haversine_km("Kyoto", "Atlantis") is None


def test_every_parser_city_has_coordinates():
    from tastepilot.parser import CITIES, CITY_CANON
    for city in CITIES:
        assert CITY_CANON.get(city, city) in CITY_COORDS


def test_estimate_modes_same_short_long_unknown():
    same = estimate_transfer("Kyoto", "Kyoto")
    assert same["mode"] == "same_city" and same["travel_hours"] == 0.0
    short = estimate_transfer("Kyoto", "Osaka")
    assert short["mode"] == "surface" and short["travel_hours"] < 2
    long = estimate_transfer("Kyoto", "Paris")
    assert long["mode"] == "flight" and 14 < long["travel_hours"] < 17
    unknown = estimate_transfer("Kyoto", "Atlantis")
    assert unknown["mode"] == "unknown" and unknown["travel_hours"] is None


def test_next_day_long_haul_is_ok():
    transfers, warnings = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Paris", "2026-11-06", "2026-11-08")])
    assert len(transfers) == 1
    t = transfers[0]
    assert (t["from"], t["to"]) == ("Kyoto", "Paris")
    assert t["gap_days"] == 1 and t["window_hours"] == 24
    assert t["status"] == "ok" and warnings == []


def test_same_day_long_haul_is_infeasible_and_warns():
    transfers, warnings = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Paris", "2026-11-05", "2026-11-07")])
    t = transfers[0]
    assert t["gap_days"] == 0 and t["window_hours"] == 12
    assert t["status"] == "infeasible"
    assert [w["code"] for w in warnings] == ["transfer_infeasible"]
    assert warnings[0]["location"] == "Paris"


def test_same_day_transatlantic_is_tight_and_warns():
    # Paris -> New York is ~10.5 h door-to-door: fits a 12 h same-day
    # window, but uses more than three quarters of it.
    transfers, warnings = assess_transfers(
        [leg("Paris", "2026-11-03", "2026-11-05"),
         leg("New York", "2026-11-05", "2026-11-07")])
    assert transfers[0]["status"] == "tight"
    assert [w["code"] for w in warnings] == ["transfer_tight"]


def test_same_day_short_hop_is_ok():
    transfers, warnings = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-03"),
         leg("Osaka", "2026-11-03", "2026-11-04")])
    assert transfers[0]["mode"] == "surface"
    assert transfers[0]["status"] == "ok" and warnings == []


def test_missing_dates_and_unknown_city_are_unknown_no_warning():
    transfers, warnings = assess_transfers(
        [leg("Kyoto"), leg("Paris", "2026-11-06")])
    assert transfers[0]["status"] == "unknown" and warnings == []
    transfers, warnings = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Atlantis", "2026-11-06", "2026-11-08")])
    assert transfers[0]["status"] == "unknown" and warnings == []


def test_single_leg_and_same_city_pairs_have_no_transfers():
    assert assess_transfers([leg("Kyoto", "2026-11-03")]) == ([], [])
    assert assess_transfers(
        [leg("Kyoto", "2026-11-03"), leg("Kyoto", "2026-11-05")]) == ([], [])


def test_start_before_previous_end_is_infeasible():
    transfers, _ = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-06"),
         leg("Osaka", "2026-11-05", "2026-11-07")])
    assert transfers[0]["window_hours"] == 0
    assert transfers[0]["status"] == "infeasible"


def test_parser_carries_transfers_and_warnings():
    p = parse_request("Kyoto Nov 3-5, then Paris Nov 6-8",
                      reference_date=REF)
    assert len(p.transfers) == 1 and p.transfers[0]["status"] == "ok"
    assert p.travel_warnings == []
    p = parse_request("Kyoto Nov 3-5, then Paris Nov 5", reference_date=REF)
    assert p.transfers[0]["status"] == "infeasible"
    assert [w["code"] for w in p.travel_warnings] == ["transfer_infeasible"]
    # A dateless multi-city request still reports its transfers,
    # judged unknown rather than silently dropped.
    p = parse_request("a trip from Kyoto to Paris", reference_date=REF)
    assert len(p.transfers) == 1 and p.transfers[0]["status"] == "unknown"


def test_agent_plan_carries_travel_fields_and_never_blocks():
    agent = TastePilotAgent(MockQlooClient())
    plan = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 5, "
                     "I love Amelie", reference_date=REF)
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    assert plan["transfers"][0]["status"] == "infeasible"
    assert [w["code"] for w in plan["travel_warnings"]] == [
        "transfer_infeasible"]
    clean = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
                      "I love Spirited Away", reference_date=REF)
    assert clean["transfers"][0]["status"] == "ok"
    assert clean["travel_warnings"] == []


def test_transfer_estimates_never_reach_qloo_calls():
    """Like dates, transfers are composition constraints only: no
    sealed Qloo request may carry an estimate."""
    agent = TastePilotAgent(MockQlooClient())
    plan = agent.run("Kyoto Nov 3-5, then Paris Nov 5, I love Amelie",
                     reference_date=REF)
    for entry in plan["evidence"]["entries"]:
        if entry["kind"] == "qloo_call":
            assert "travel_hours" not in str(entry)


def test_cli_prints_transfer_lines(capsys):
    code = cli.main(["plan", "Kyoto Nov 3-5, then Paris Nov 5, "
                             "I love Amelie"])
    out = capsys.readouterr().out
    assert code == 0  # an infeasible transfer never blocks the plan
    assert "transfer:" in out and "status=infeasible" in out
    assert "travel warning [transfer_infeasible]" in out


def test_ui_and_docs_carry_travel_wiring():
    html = (ROOT / "tastepilot" / "static" / "index.html").read_text()
    assert "transfers" in html and "travel_warnings" in html
    readme = (ROOT / "README.md").read_text()
    assert re.search(r"travel-time feasibility", readme, re.IGNORECASE)
    submission = (ROOT / "SUBMISSION.md").read_text()
    assert re.search(r"travel-time feasibility", submission, re.IGNORECASE)
