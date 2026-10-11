"""Deeper native integrations: platform maps links + OS share sheet.

Doctrine pinned here: every leg/item also gets an Apple Maps URL
and a ``geo:`` URI for the same recomputed query; the Web Share
payload (``handoffs.share``) is recomputed from schedule + items,
its verification line is recomputed from the sealed evidence (a
tampered plan shares as NOT VERIFIED), and the payload is bounded
with a "+N more" truncation note."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, unquote_plus, urlparse

from tastepilot.agent import TastePilotAgent
from tastepilot.export import export_plan
from tastepilot.handoff import (apple_maps_url, geo_url,
                                plan_handoffs, plan_share_payload)
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


def test_apple_maps_url_encodes_query():
    url = apple_maps_url("Gion & Kiyomizu, Kyoto")
    assert url.startswith("https://maps.apple.com/?q=")
    assert unquote_plus(urlparse(url).query.split("q=", 1)[1]) \
        == "Gion & Kiyomizu, Kyoto"


def test_geo_url_is_android_intent_form():
    url = geo_url("Gion & Kiyomizu, Kyoto")
    assert url.startswith("geo:0,0?q=")
    assert unquote(url.split("q=", 1)[1]) == "Gion & Kiyomizu, Kyoto"
    assert " " not in url  # fully percent-encoded


def test_plan_handoffs_carry_platform_links():
    handoffs = plan_handoffs(_dated_plan())
    for leg in handoffs["legs"]:
        assert leg["maps_apple_url"].startswith("https://maps.apple.com/?q=")
        assert leg["maps_geo_url"].startswith("geo:0,0?q=")
    for item in handoffs["items"]:
        assert item["maps_apple_url"].startswith("https://maps.apple.com/?q=")
        assert item["maps_geo_url"].startswith("geo:0,0?q=")
    # Same query across platforms for one item.
    item = handoffs["items"][0]
    google_q = unquote_plus(item["maps_url"].split("query=", 1)[1])
    apple_q = unquote_plus(item["maps_apple_url"].split("q=", 1)[1])
    geo_q = unquote(item["maps_geo_url"].split("q=", 1)[1])
    assert google_q == apple_q == geo_q


def test_share_payload_names_legs_and_items():
    payload = plan_share_payload(_dated_plan())
    assert payload["title"] == "TastePilot plan: Kyoto → Paris"
    assert "Kyoto (2026-11-03" in payload["text"]
    assert "Paris (2026-11-06" in payload["text"]
    assert "Verification: PASS" in payload["text"]


def test_share_payload_dateless_sample():
    payload = plan_share_payload(KYOTO)
    assert payload["title"] == "TastePilot plan: Kyoto"
    assert "Verification: PASS" in payload["text"]


def test_share_payload_tampered_plan_is_not_verified():
    plan = copy.deepcopy(KYOTO)
    plan["items"][0]["affinity"] = 0.01  # forge one affinity
    payload = plan_share_payload(plan)
    assert "NOT VERIFIED" in payload["text"]
    assert "Verification: PASS" not in payload["text"]


def test_share_payload_missing_evidence_is_not_verified():
    payload = plan_share_payload({"request": "x", "items": []})
    assert "NOT VERIFIED" in payload["text"]


def test_share_payload_ignores_stored_handoffs_field():
    plan = _dated_plan()
    plan["handoffs"] = {"share": {"title": "evil",
                                   "text": "https://evil.example"}}
    payload = plan_share_payload(plan)
    assert "evil" not in payload["title"]
    assert "evil.example" not in payload["text"]


def test_share_payload_is_bounded():
    plan = _dated_plan()
    plan["items"] = plan["items"] * 60  # absurdly large plan
    payload = plan_share_payload(plan)
    assert len(payload["text"]) <= 1000
    assert "more)" in payload["text"]
    assert "Verification:" in payload["text"]  # verdict survives the cap


def test_share_payload_deterministic():
    plan = _dated_plan()
    assert plan_share_payload(plan) == plan_share_payload(plan)
    assert plan_handoffs(plan)["share"] == plan_share_payload(plan)


def test_agent_plan_carries_share_payload():
    plan = _dated_plan()
    assert plan["handoffs"]["share"] == plan_share_payload(plan)
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]


def test_exports_carry_apple_maps_leg_link():
    md, verified = export_plan(_dated_plan(), "markdown")
    assert verified
    assert "Open Kyoto in Apple Maps" in md
    assert "maps.apple.com/?q=" in md
    text, _ = export_plan(_dated_plan(), "text")
    assert "maps.apple.com/?q=" in text


def test_cli_plan_prints_native_lines():
    proc = subprocess.run(
        [sys.executable, "-m", "tastepilot", "plan",
         "a trip: Kyoto Nov 3-5, I love Spirited Away"],
        cwd=APP, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "handoff: open Kyoto in Apple Maps:" in proc.stdout
    assert "share payload: TastePilot plan: Kyoto" in proc.stdout


def test_ui_carries_share_sheet_and_apple_wiring():
    html = (APP / "tastepilot" / "static" / "index.html") \
        .read_text(encoding="utf-8")
    assert "ossharebtn" in html and "navigator.share" in html
    assert "navigator.clipboard" in html  # fallback when no share sheet
    assert "handoffs.share" in html or "handoffs && lastPlan.handoffs" in html \
        or ".handoffs.share" in html
    assert "Open in Apple Maps" in html
    assert "maps_apple_url" in html


def test_docs_describe_native_integrations():
    readme = (APP / "README.md").read_text(encoding="utf-8")
    assert "maps.apple.com" in readme or "Apple Maps" in readme
    assert "share" in readme.lower()
    submission = (APP / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "share sheet" in submission.lower()
