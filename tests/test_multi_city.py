"""Multi-city trip composition: every city a request mentions is a
leg, in mention order. Each leg is planned with the same resolved
seeds (per-leg taste continuity) but its own location-filtered Qloo
insights calls, sealed separately — so a leg's items cite that leg's
responses, and the same location-agnostic entity may appear once per
leg with a distinct evidence ref each time."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.evidence import verify_chain
from tastepilot.parser import extract_locations, parse_request
from tastepilot.qloo import MockQlooClient
from tastepilot.server import make_handler

MULTI = "a slow weekend trip from Kyoto to Paris, I love Spirited Away and Amelie"


def test_locations_in_mention_order():
    p = parse_request(MULTI)
    assert p.locations == ["Kyoto", "Paris"]
    assert p.location == "Kyoto"  # first leg, for single-location callers
    assert p.scenario == "weekend"


def test_mention_order_is_not_catalogue_order():
    p = parse_request("a trip to Paris and then Kyoto, I love Amelie")
    assert p.locations == ["Paris", "Kyoto"]
    assert p.location == "Paris"


def test_aliases_dedupe_to_one_leg():
    assert extract_locations("a date in NYC, then more of New York") == ["New York"]
    assert extract_locations("New York, NYC, New York") == ["New York"]


def test_single_and_missing_city_behaviour_unchanged():
    p = parse_request("a slow weekend in Kyoto, I love Spirited Away")
    assert p.locations == ["Kyoto"] and p.location == "Kyoto"
    p = parse_request("plan something nice, I love Spirited Away")
    assert p.locations == [] and p.location is None


def test_free_text_location_fallback_is_a_single_leg():
    p = parse_request("a weekend in Lisbon, I love Spirited Away")
    assert p.locations == ["Lisbon"] and p.location == "Lisbon"


def test_no_leg_city_becomes_a_seed():
    p = parse_request(MULTI)
    assert "Kyoto" not in p.seed_names and "Paris" not in p.seed_names


def test_multi_city_plan_has_both_legs_and_verifies():
    plan = TastePilotAgent().run(MULTI)
    assert plan["locations"] == ["Kyoto", "Paris"]
    assert [leg["location"] for leg in plan["legs"]] == ["Kyoto", "Paris"]
    names = {leg["location"]: {i["entity"]["name"]
                                for g in leg["groups"] for i in g["items"]}
             for leg in plan["legs"]}
    assert {"Kyoto", "Gion, Kyoto"} <= names["Kyoto"]
    assert {"Paris", "Montmartre, Paris"} <= names["Paris"]
    # Location-bound items never leak across legs.
    assert "Montmartre, Paris" not in names["Kyoto"]
    assert "Gion, Kyoto" not in names["Paris"]
    assert all(i["location"] in ("Kyoto", "Paris") for i in plan["items"])
    assert plan["checker"]["passed"] is True
    assert plan["rejected"] == []
    assert verify_chain(plan["evidence"])


def test_same_entity_per_leg_cites_distinct_evidence():
    plan = TastePilotAgent().run(MULTI)
    nujabes = [i for i in plan["items"] if i["entity"]["name"] == "Nujabes"]
    assert len(nujabes) == 2  # location-agnostic: one per leg
    assert {i["location"] for i in nujabes} == {"Kyoto", "Paris"}
    assert nujabes[0]["evidence_ref"] != nujabes[1]["evidence_ref"]
    for item in nujabes:
        assert item["verification"]["admissible"] is True
        assert item["verification"]["reason"] == "ok"
        assert item["verification"]["evidence"]["response_id"] == item["evidence_ref"]
    assert len(plan["checker"]["items"]) == len(plan["items"])


def test_legs_share_the_same_resolved_seeds():
    calls = []

    class RecordingClient(MockQlooClient):
        def insights(self, seed_ids, target_domain, limit=5, location=None):
            calls.append((tuple(seed_ids), target_domain, location))
            return super().insights(seed_ids, target_domain,
                                    limit=limit, location=location)

    plan = TastePilotAgent(qloo_client=RecordingClient()).run(MULTI)
    assert {loc for _, _, loc in calls} == {"Kyoto", "Paris"}
    assert len({seeds for seeds, _, _ in calls}) == 1  # taste continuity
    assert plan["seeds"]  # resolved once, reported on the plan


def test_single_city_plan_has_exactly_one_leg():
    plan = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue")
    assert [leg["location"] for leg in plan["legs"]] == ["Kyoto"]
    assert all(i["location"] == "Kyoto" for i in plan["items"])
    # The flat domain groups remain, covering the same items.
    grouped = [i["entity"]["name"] for g in plan["groups"] for i in g["items"]]
    assert sorted(grouped) == sorted(i["entity"]["name"] for i in plan["items"])


def test_multi_city_plan_is_deterministic():
    a = TastePilotAgent().run(MULTI)
    b = TastePilotAgent().run(MULTI)
    key = lambda p: [(i["entity"]["name"], i["domain"], i["location"],
                      i["affinity"], i["evidence_ref"]) for i in p["items"]]
    assert key(a) == key(b)


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_server_plan_and_ui_carry_legs(server_url):
    req = urllib.request.Request(
        server_url + "/api/plan", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"text": MULTI}).encode())
    with urllib.request.urlopen(req) as resp:
        plan = json.loads(resp.read())
    assert [leg["location"] for leg in plan["legs"]] == ["Kyoto", "Paris"]
    assert plan["checker"]["passed"] is True
    with urllib.request.urlopen(server_url + "/") as resp:
        page = resp.read().decode()
    assert "data.legs" in page and "Legs:" in page
