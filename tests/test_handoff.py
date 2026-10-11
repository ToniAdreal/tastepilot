"""Native handoffs: calendar / maps links recomputed from a plan.

Doctrine pinned here: a dated leg gets a Google Calendar template
link (exclusive end date); a dateless leg gets NO calendar link
(never invent dates) but still gets a maps link; links are
recomputed from schedule + items, so a stored ``handoffs`` field
is never trusted; rendering is deterministic and URL-capped."""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

from tastepilot.agent import TastePilotAgent
from tastepilot.export import export_plan
from tastepilot.handoff import leg_calendar_url, maps_url, plan_handoffs
from tastepilot.llm import TemplateExplainer
from tastepilot.qloo import MockQlooClient

APP = Path(__file__).resolve().parents[1]
KYOTO = json.loads((APP / "samples" / "plan_weekend_kyoto.json")
                   .read_text(encoding="utf-8"))


def _agent():
    return TastePilotAgent(MockQlooClient(), TemplateExplainer())


def _dated_plan():
    return _agent().run(
        "a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
        "I love Spirited Away and Amelie")


def test_maps_url_encodes_query():
    url = maps_url("Gion & Kiyomizu, Kyoto")
    assert url.startswith("https://www.google.com/maps/search/?api=1&query=")
    assert unquote_plus(urlparse(url).query.split("query=", 1)[1]) \
        == "Gion & Kiyomizu, Kyoto"


def test_calendar_url_all_day_exclusive_end():
    url = leg_calendar_url("Kyoto", "2026-11-03", "2026-11-05",
                           ["Gion"], request_text="weekend")
    qs = parse_qs(urlparse(url).query)
    assert qs["action"] == ["TEMPLATE"]
    assert qs["dates"] == ["20261103/20261106"]  # end + 1 day
    assert qs["text"] == ["TastePilot: Kyoto"]
    assert "Gion" in qs["details"][0]


def test_calendar_url_single_day_and_dateless():
    url = leg_calendar_url("Paris", "2026-11-06", None, [])
    assert parse_qs(urlparse(url).query)["dates"] == ["20261106/20261107"]
    assert leg_calendar_url("Paris", None, None, []) is None
    assert leg_calendar_url("Paris", "", None, []) is None


def test_calendar_url_is_capped():
    names = [f"Some Very Long Venue Name Number {i} In Kyoto Japan"
             for i in range(200)]
    url = leg_calendar_url("Kyoto", "2026-11-03", "2026-11-05", names,
                           request_text="x" * 100)
    assert len(url) <= 2000
    assert "more in the TastePilot plan" in unquote_plus(url)


def test_plan_handoffs_dated_multi_leg():
    plan = _dated_plan()
    handoffs = plan_handoffs(plan)
    assert [leg["location"] for leg in handoffs["legs"]] == ["Kyoto", "Paris"]
    for leg in handoffs["legs"]:
        assert leg["calendar_url"] and "calendar.google.com" in leg["calendar_url"]
        assert leg["maps_url"] and "google.com/maps" in leg["maps_url"]
    assert handoffs["items"], "expected per-item maps links"
    for item in handoffs["items"]:
        assert item["maps_url"].startswith(
            "https://www.google.com/maps/search/?api=1&query=")


def test_plan_handoffs_dateless_leg_has_maps_but_no_calendar():
    handoffs = plan_handoffs(KYOTO)
    assert len(handoffs["legs"]) == 1
    leg = handoffs["legs"][0]
    assert leg["location"] == "Kyoto"
    assert leg["calendar_url"] is None  # never invent dates
    assert leg["maps_url"]


def test_item_maps_query_uses_location():
    handoffs = plan_handoffs(KYOTO)
    by_name = {i["name"]: i["maps_url"] for i in handoffs["items"]}
    grove = by_name.get("Arashiyama Bamboo Grove")
    assert grove is not None
    assert unquote_plus(grove.split("query=", 1)[1]) \
        == "Arashiyama Bamboo Grove, Kyoto"
    gion = by_name.get("Gion, Kyoto")  # name already carries the city
    assert unquote_plus(gion.split("query=", 1)[1]) == "Gion, Kyoto"
    kyoto = by_name.get("Kyoto")  # destination: name == location
    assert unquote_plus(kyoto.split("query=", 1)[1]) == "Kyoto"


def test_handoffs_deterministic():
    plan = _dated_plan()
    assert plan_handoffs(plan) == plan_handoffs(plan)


def test_agent_plan_carries_recomputed_handoffs():
    plan = _dated_plan()
    assert plan["handoffs"] == plan_handoffs(plan)
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]


def test_exports_carry_handoffs():
    plan = _dated_plan()
    md, verified = export_plan(plan, "markdown")
    assert verified
    assert "## Handoffs" in md
    assert "Add the Kyoto leg to Google Calendar" in md
    assert "calendar.google.com/calendar/render" in md
    assert "google.com/maps/search" in md
    text, _ = export_plan(plan, "text")
    assert "Handoffs (your own calendar / maps):" in text
    assert "calendar.google.com/calendar/render" in text
    assert not re.search(r"\[.+\]\(http", text)  # plain text, raw URLs


def test_export_ignores_stored_handoffs_field():
    plan = _dated_plan()
    plan["handoffs"] = {"legs": [{"location": "Kyoto", "start": None,
                                   "end": None,
                                   "calendar_url": "https://evil.example/x",
                                   "maps_url": "https://evil.example/y"}],
                         "items": []}
    md, _ = export_plan(plan, "markdown")
    assert "evil.example" not in md
    assert "calendar.google.com/calendar/render" in md


def test_dateless_export_has_maps_handoff_but_no_calendar():
    md, _ = export_plan(KYOTO, "markdown")
    assert "Open Kyoto in Google Maps" in md
    assert "calendar.google.com" not in md


def test_cli_plan_prints_handoff_lines(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "tastepilot", "plan",
         "a trip: Kyoto Nov 3-5, I love Spirited Away"],
        cwd=APP, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "handoff: add Kyoto leg to Google Calendar:" in proc.stdout
    assert "handoff: open Kyoto in Google Maps:" in proc.stdout


def test_ui_carries_handoff_wiring():
    html = (APP / "tastepilot" / "static" / "index.html") \
        .read_text(encoding="utf-8")
    assert "mapslink" in html and "open in Maps" in html
    assert "leghandoff" in html
    assert "Add this leg to Google Calendar" in html
    assert "data.handoffs" in html


def test_docs_describe_handoffs():
    readme = (APP / "README.md").read_text(encoding="utf-8")
    assert "handoff.py" in readme and "Google Calendar" in readme
    submission = (APP / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "handoff" in submission.lower()
