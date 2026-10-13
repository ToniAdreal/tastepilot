"""Live tag shape: the real Qloo API returns tag objects, not strings.

Found by the 2026-10-13 real-mode evidence run — the first end-to-end
run against the live API after the key landed. Mock entities carry
string tags (the internal ``Entity.tags`` contract), but live rows
carry ``{"id", "name", "type"}`` tag dicts, and:

- ``parser.exclusion_match`` did ``t.lower()`` on each tag →
  AttributeError, so EVERY real-mode request with an exclusion
  ("…but no jazz") crashed with a traceback (exit 1);
- ``swap._tags_of`` did ``set(entity tags)`` → TypeError (unhashable
  dict), so guided suggestions / learned ranking would have crashed
  on any real-mode plan next.

The fix normalises at the boundary and hardens the consumers:
``models.tag_names`` extracts display names from either shape,
``RealQlooClient._parse_entities`` stores names (restoring the
``Entity.tags: tuple[str, ...]`` contract), and ``exclusion_match`` /
``_tags_of`` read through the same helper so a foreign plan JSON
carrying dict tags cannot crash them either. The live payload also
carries no affinity/score at all (only a global ``popularity``),
which the parser's documented 0.5 fallback already covers — pinned
here so the fallback stays a deliberate, visible behaviour.
"""
from tastepilot import fixtures
from tastepilot.agent import TastePilotAgent
from tastepilot.models import (Entity, QlooResponse, ScoredEntity,
                               tag_name, tag_names)
from tastepilot.parser import exclusion_match
from tastepilot.qloo import RealQlooClient, response_id_for
from tastepilot.swap import _tags_of

SENTINEL_KEY = "sentinel-key-must-never-appear"

# One live-shaped insights payload: results-object envelope, entity
# rows with tag objects, a popularity score and NO affinity/score.
LIVE_INSIGHTS = {
    "success": True,
    "results": {"entities": [
        {"name": "Blue Note Kyoto", "entity_id": "live1",
         "type": "urn:entity", "subtype": "urn:entity:place",
         "popularity": 0.87,
         "tags": [{"id": "urn:tag:genre:place:jazz_club",
                   "name": "Jazz Club", "type": "urn:tag:genre:place"},
                  {"id": "urn:tag:keyword:live_music",
                   "name": "Live Music", "type": "urn:tag:keyword"}]},
        {"name": "Quiet Garden", "entity_id": "live2",
         "type": "urn:entity", "subtype": "urn:entity:place",
         "popularity": 0.42, "tags": []},
    ]},
}


def _client(payload):
    def transport(method, url, headers):
        return 200, payload
    return RealQlooClient(SENTINEL_KEY, transport=transport,
                          sleeper=lambda s: None)


def test_tag_name_shapes():
    assert tag_name("jazz") == "jazz"
    assert tag_name({"name": "Jazz", "id": "x"}) == "Jazz"
    assert tag_name({"id": "urn:tag:jazz"}) == "urn:tag:jazz"
    assert tag_name({"tag_id": "urn:tag:blues"}) == "urn:tag:blues"
    assert tag_name({"name": ""}) is None
    assert tag_name(42) is None
    assert tag_name(None) is None
    assert tag_names([{"name": "A"}, "b", 42, {"name": "C"}]) == ["A", "b", "C"]
    assert tag_names(None) == []


def test_real_client_normalises_live_tag_objects():
    resp = _client(LIVE_INSIGHTS).insights(["seed1"], "place", limit=5)
    first, second = resp.results
    assert first.entity.tags == ("Jazz Club", "Live Music")
    assert all(isinstance(t, str) for t in first.entity.tags)
    assert second.entity.tags == ()
    # No affinity/score in the live payload -> the documented neutral
    # fallback, never the entity's global popularity relabelled.
    assert first.affinity == 0.5 and second.affinity == 0.5


def test_exclusion_match_tolerates_dict_tags():
    item = {"domain": "place",
            "entity": {"name": "Blue Note Kyoto",
                       "tags": [{"name": "Jazz Club"}, {"name": "Live Music"}]}}
    assert exclusion_match(item, ["jazz"]) == "jazz"
    assert exclusion_match(item, ["opera"]) is None


def test_tags_of_tolerates_dict_tags():
    item = {"entity": {"tags": [{"name": "Jazz Club"}, "quiet"]},
            "shared_tags": [{"name": "Shared One"}]}
    assert _tags_of(item) == {"Jazz Club", "quiet", "Shared One"}


class DictTagClient:
    """Stub client whose entities carry live-shaped dict tags,
    bypassing the real client's normalisation — the consumers must
    still not crash (a foreign plan can carry this shape)."""

    source = "mock"

    def search(self, query, limit=10):
        ent = fixtures.BY_ID["mov_spirited_away"]
        results = [ScoredEntity(ent, 1.0)]
        request = {"query": query, "limit": limit}
        return QlooResponse(
            response_id_for("search", request, "mock", results),
            "search", request, results, "mock", len(results))

    def insights(self, seed_ids, target_domain, limit=5, location=None):
        request = {"filter.type": target_domain,
                   "signal.interests.entities": ",".join(seed_ids),
                   "take": limit, "location": location}
        if target_domain == "place":
            jazz = Entity("live_jazz", "Blue Note Kyoto", "place",
                          tags=({"id": "urn:tag:genre:jazz",
                                 "name": "Jazz Club",
                                 "type": "urn:tag:genre"},))
            garden = Entity("live_garden", "Quiet Garden", "place",
                            tags=({"id": "urn:tag:mood:quiet",
                                   "name": "Quiet",
                                   "type": "urn:tag:mood"},))
            scored = [ScoredEntity(jazz, 0.9), ScoredEntity(garden, 0.8)]
        elif target_domain == "music":
            scored = [ScoredEntity(fixtures.BY_ID["art_nujabes"], 0.5)]
        else:
            scored = []
        return QlooResponse(
            response_id_for("insights", request, "mock", scored),
            "insights", request, scored, "mock", len(scored))


def test_agent_exclusion_over_dict_tag_entities():
    plan = TastePilotAgent(qloo_client=DictTagClient()).run(
        "a slow weekend in Kyoto, I love Spirited Away, but no jazz")
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    excluded = plan["excluded"]
    # The dict-tagged jazz place is excluded via its tag's word, and
    # the fixture jazz artist (string tag) goes with it — both were
    # admissible, so both keep their passing verification.
    assert [e["name"] for e in excluded] == ["Blue Note Kyoto", "Nujabes"]
    assert all(e["matched"] == "jazz" for e in excluded)
    assert all(e["verification"] == "ok" for e in excluded)
    assert all(i["entity"]["name"] != "Blue Note Kyoto"
               for i in plan["items"])
