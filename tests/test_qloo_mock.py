from tastepilot.qloo import MockQlooClient


def test_search_finds_seed_by_name():
    resp = MockQlooClient().search("Spirited Away")
    assert resp.source == "mock" and resp.endpoint == "search"
    assert resp.results[0].entity.id == "mov_spirited_away"


def test_search_is_deterministic():
    c = MockQlooClient()
    assert c.search("jazz").response_id == c.search("jazz").response_id


def test_insights_cross_domain_sorted_and_filtered():
    resp = MockQlooClient().insights(["mov_spirited_away"], "place", limit=5)
    assert resp.results, "expected cross-domain place results"
    assert all(r.entity.domain == "place" for r in resp.results)
    affinities = [r.affinity for r in resp.results]
    assert affinities == sorted(affinities, reverse=True)
    assert resp.results[0].entity.id == "plc_gion"


def test_insights_location_filter():
    resp = MockQlooClient().insights(["mov_amelie"], "food", location="Paris")
    assert resp.results and all(
        (r.entity.location in (None, "Paris")) for r in resp.results)


def test_insights_unknown_seed_is_empty_or_fallback_only():
    resp = MockQlooClient().insights(["no_such_id"], "film")
    assert resp.results == []


def test_insights_request_shape_matches_real_api():
    resp = MockQlooClient().insights(["a", "b"], "film")
    assert resp.request["filter.type"] == "urn:entity:movie"
    assert resp.request["signal.interests.entities"] == "a,b"


def test_place_and_food_responses_have_distinct_ids():
    # Regression: place and food share urn:entity:place in the request,
    # so identical request params must still seal as distinct responses.
    c = MockQlooClient()
    a = c.insights(["mov_spirited_away"], "place", limit=4)
    b = c.insights(["mov_spirited_away"], "food", limit=4)
    assert a.response_id != b.response_id
