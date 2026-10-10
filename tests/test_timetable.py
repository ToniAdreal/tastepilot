"""Timetable providers: scheduled inter-leg services from a file the
user supplies, behind the same feasibility interface as the offline
estimates (run 14). Deterministic, offline, no network: the file is
the whole input, scheduled pairs are judged on their scheduled
duration, uncovered pairs fall back to the offline estimate, and
every transfer records which source produced it.
"""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.parser import parse_request
from tastepilot.qloo import MockQlooClient
from tastepilot.timetable import (ChainedTimetableProvider,
                                  FileTimetableProvider,
                                  OfflineEstimateProvider,
                                  load_timetable, parse_timetable_bytes,
                                  provider_for)
from tastepilot.travel import assess_transfers

REF = date(2026, 10, 10)
ROOT = Path(__file__).resolve().parents[1]


def leg(location, start=None, end=None):
    return {"location": location, "start": start, "end": end,
            "nights": None, "dates": []}


KYOTO_PARIS_JSON = json.dumps({"services": [
    {"from": "Kyoto", "to": "Paris", "duration_hours": 13.5,
     "mode": "flight", "service": "Demo Air 101"},
    {"from": "Kyoto", "to": "Osaka", "duration_minutes": 75,
     "mode": "train", "service": "Demo Rail"},
]}).encode()


def test_parse_json_durations_modes_and_provenance():
    parsed = parse_timetable_bytes(KYOTO_PARIS_JSON, "my-services.json")
    assert parsed["source_file"] == "my-services.json"
    assert parsed["format"] == "timetable_json"
    by_pair = {(e["from"], e["to"]): e for e in parsed["entries"]}
    assert by_pair[("Kyoto", "Paris")]["travel_hours"] == 13.5
    assert by_pair[("Kyoto", "Paris")]["service"] == "Demo Air 101"
    # duration_minutes is converted to hours.
    assert by_pair[("Kyoto", "Osaka")]["travel_hours"] == 1.25
    assert by_pair[("Kyoto", "Osaka")]["mode"] == "train"


def test_parse_csv_and_bare_list_json():
    csv_bytes = (b"from,to,duration_hours,mode,service\n"
                 b"Paris,New York,7.5,flight,Demo Air 202\n")
    parsed = parse_timetable_bytes(csv_bytes, "services.csv")
    assert parsed["format"] == "timetable_csv"
    assert parsed["entries"][0]["travel_hours"] == 7.5
    bare = parse_timetable_bytes(
        json.dumps([{"from": "Tokyo", "to": "Seoul",
                     "duration_hours": 2.5}]).encode(), "x.json")
    assert bare["entries"][0]["mode"] == "scheduled"
    assert bare["entries"][0]["service"] is None


def test_parse_errors_are_categorised_and_never_echo_content():
    with pytest.raises(ValueError, match="bad_json"):
        parse_timetable_bytes(b"{not json SECRETROW", "x.json")
    with pytest.raises(ValueError, match="no_items"):
        parse_timetable_bytes(b'{"services": []}', "x.json")
    with pytest.raises(ValueError, match="no_items"):
        # A row with no usable duration is skipped, not guessed.
        parse_timetable_bytes(
            b'[{"from": "Kyoto", "to": "Paris"}]', "x.json")
    with pytest.raises(ValueError, match="unsupported"):
        parse_timetable_bytes(b"Kyoto to Paris somehow\n", "x.txt")
    with pytest.raises(ValueError, match="too_large"):
        parse_timetable_bytes(b" " * 1_048_577, "x.json")
    with pytest.raises(ValueError, match="undecodable"):
        parse_timetable_bytes(b"\xff\xfe\x00bad", "x.json")
    with pytest.raises(ValueError, match="unreadable"):
        load_timetable(ROOT / "no-such-timetable.json")
    # Invalid durations (zero / absurd) are dropped like missing ones.
    parsed = parse_timetable_bytes(json.dumps({"services": [
        {"from": "Kyoto", "to": "Paris", "duration_hours": 0},
        {"from": "Kyoto", "to": "Osaka", "duration_hours": 1.0},
    ]}).encode(), "x.json")
    assert [(e["from"], e["to"]) for e in parsed["entries"]] == [
        ("Kyoto", "Osaka")]


def test_file_provider_exact_reverse_and_miss():
    provider = FileTimetableProvider(
        [parse_timetable_bytes(KYOTO_PARIS_JSON, "s.json")])
    hit = provider.lookup("Kyoto", "Paris")
    assert hit["source"] == "timetable_file"
    assert hit["travel_hours"] == 13.5 and hit["service"] == "Demo Air 101"
    assert "reversed" not in hit
    rev = provider.lookup("Paris", "Kyoto")
    assert rev["travel_hours"] == 13.5 and rev["reversed"] is True
    assert provider.lookup("Kyoto", "Tokyo") is None
    # Case-insensitive on city names.
    assert provider.lookup("kyoto", "paris")["travel_hours"] == 13.5


def test_chained_provider_falls_back_to_offline_estimate():
    provider = provider_for(
        [parse_timetable_bytes(KYOTO_PARIS_JSON, "s.json")])
    assert isinstance(provider, ChainedTimetableProvider)
    scheduled = provider.lookup("Kyoto", "Paris")
    assert scheduled["source"] == "timetable_file"
    estimated = provider.lookup("Paris", "New York")
    assert estimated["source"] == "offline_estimate"
    assert estimated["travel_hours"] > 9  # the run-14 estimate
    plain = provider_for(None)
    assert isinstance(plain, OfflineEstimateProvider)
    assert plain.lookup("Kyoto", "Atlantis") is None


def test_scheduled_duration_changes_the_verdict():
    # Same-day Kyoto -> Paris: the 15.3 h offline estimate is
    # infeasible in a 12 h window, but a 13.5 h scheduled service is
    # still infeasible while an 11 h one would be tight — here the
    # file's 13.5 h keeps infeasible, and Osaka's short scheduled
    # train flips nothing. Use an 11 h service to show the flip:
    parsed = parse_timetable_bytes(json.dumps({"services": [
        {"from": "Kyoto", "to": "Paris", "duration_hours": 11.0,
         "mode": "flight"}]}).encode(), "s.json")
    provider = provider_for([parsed])
    transfers, warnings = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Paris", "2026-11-05", "2026-11-07")], provider=provider)
    t = transfers[0]
    assert t["source"] == "timetable_file" and t["travel_hours"] == 11.0
    assert t["status"] == "tight"  # 11 h in a 12 h window
    assert [w["code"] for w in warnings] == ["transfer_tight"]
    assert "scheduled" in t["detail"]


def test_uncovered_pair_in_same_plan_uses_estimate_and_records_source():
    parsed = parse_timetable_bytes(KYOTO_PARIS_JSON, "s.json")
    provider = provider_for([parsed])
    transfers, _ = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Paris", "2026-11-06", "2026-11-08"),
         leg("New York", "2026-11-09", "2026-11-10")],
        provider=provider)
    assert [t["source"] for t in transfers] == [
        "timetable_file", "offline_estimate"]
    assert transfers[0]["status"] == "ok"  # 13.5 h in a 24 h window


def test_scheduled_pair_without_dates_is_unknown_but_carries_hours():
    parsed = parse_timetable_bytes(KYOTO_PARIS_JSON, "s.json")
    transfers, warnings = assess_transfers(
        [leg("Kyoto"), leg("Paris", "2026-11-06")],
        provider=provider_for([parsed]))
    assert transfers[0]["status"] == "unknown" and warnings == []
    assert transfers[0]["travel_hours"] == 13.5


def test_default_assess_records_offline_source():
    transfers, _ = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Paris", "2026-11-06", "2026-11-08")])
    assert transfers[0]["source"] == "offline_estimate"
    assert transfers[0]["service"] is None


def test_agent_plan_carries_timetable_provenance_and_verdict():
    agent = TastePilotAgent(MockQlooClient())
    tt = parse_timetable_bytes(json.dumps({"services": [
        {"from": "Kyoto", "to": "Paris", "duration_hours": 11.0,
         "mode": "flight", "service": "Demo Air 101"}]}).encode(),
        "my-services.json")
    plan = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 5, "
                     "I love Amelie", reference_date=REF, timetables=[tt])
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    assert plan["timetable"] == [{"source_file": "my-services.json",
                                  "format": "timetable_json",
                                  "entries": 1}]
    assert plan["transfers"][0]["source"] == "timetable_file"
    assert plan["transfers"][0]["status"] == "tight"
    assert [w["code"] for w in plan["travel_warnings"]] == [
        "transfer_tight"]
    # No timetable -> empty provenance, offline sources throughout.
    plain = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
                      "I love Spirited Away", reference_date=REF)
    assert plain["timetable"] == []
    assert plain["transfers"][0]["source"] == "offline_estimate"


def test_timetable_durations_never_reach_qloo_calls():
    agent = TastePilotAgent(MockQlooClient())
    tt = parse_timetable_bytes(KYOTO_PARIS_JSON, "s.json")
    plan = agent.run("Kyoto Nov 3-5, then Paris Nov 6, I love Amelie",
                     reference_date=REF, timetables=[tt])
    for entry in plan["evidence"]["entries"]:
        if entry["kind"] == "qloo_call":
            assert "Demo Air 101" not in str(entry)
            assert "travel_hours" not in str(entry)


def test_cli_timetable_file_end_to_end(tmp_path, capsys):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": [
        {"from": "Kyoto", "to": "Paris", "duration_hours": 11.0,
         "mode": "flight"}]}), encoding="utf-8")
    code = cli.main(["plan", "Kyoto Nov 3-5, then Paris Nov 5, "
                             "I love Amelie",
                     "--timetable-file", str(path)])
    out = capsys.readouterr().out
    assert code == 0  # a tight transfer never blocks the plan
    assert "timetable: services.json (timetable_json)" in out
    assert "1 matched" in out
    assert "source=timetable_file" in out and "status=tight" in out


def test_cli_bad_or_missing_timetable_file_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    code = cli.main(["plan", "a weekend in Kyoto",
                     "--timetable-file", str(bad)])
    err = capsys.readouterr().err
    assert code == 2 and "timetable failed: bad_json" in err
    assert "Traceback" not in err
    code = cli.main(["plan", "a weekend in Kyoto",
                     "--timetable-file", str(tmp_path / "gone.json")])
    err = capsys.readouterr().err
    assert code == 2 and "unreadable" in err


def test_ui_and_docs_carry_timetable_wiring():
    html = (ROOT / "tastepilot" / "static" / "index.html").read_text()
    assert "timetable" in html.lower()
    assert "Timetable:" in html and "timetable_file" in html
    readme = (ROOT / "README.md").read_text()
    assert re.search(r"timetable", readme, re.IGNORECASE)
    assert "timetable.py" in readme
    submission = (ROOT / "SUBMISSION.md").read_text()
    assert re.search(r"timetable file", submission, re.IGNORECASE)
