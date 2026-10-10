"""Richer seed capture: pasted labelled lists (watchlist / playlist /
reading list) contribute every item as a seed — catalogue titles under
their canonical name, unknown titles verbatim for Qloo search — with
the list provenance carried into the plan and shown in the UI."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.evidence import verify_chain
from tastepilot.parser import extract_seed_lists, parse_request
from tastepilot.qloo import MockQlooClient
from tastepilot.server import make_handler


def test_labelled_lists_extract_items_with_provenance():
    p = parse_request(
        "Plan a weekend in Kyoto. Watchlist: Spirited Away, An Obscure Reel. "
        "Playlist: Kind of Blue; Bon Iver. Reading list: Norwegian Wood")
    assert p.seed_lists == [
        {"label": "watchlist", "items": ["Spirited Away", "An Obscure Reel"]},
        {"label": "playlist", "items": ["Kind of Blue", "Bon Iver"]},
        {"label": "reading list", "items": ["Norwegian Wood"]},
    ]
    # Unknown titles are captured verbatim; catalogue titles land under
    # their canonical catalogue name, exactly once.
    assert "An Obscure Reel" in p.seed_names
    assert "Kind of Blue (Miles Davis)" in p.seed_names
    assert p.seed_names.count("Spirited Away") == 1


def test_bulleted_list_and_prose_termination():
    text = ("Watchlist:\n- Spirited Away\n- Parasite\n"
            "Now plan a quiet evening around these.")
    lists = extract_seed_lists(text)
    assert lists == [{"label": "watchlist",
                      "items": ["Spirited Away", "Parasite"]}]


def test_label_line_list_ends_with_the_sentence():
    p = parse_request("Watchlist: Spirited Away. Plan a weekend in Kyoto please")
    assert p.seed_lists == [{"label": "watchlist", "items": ["Spirited Away"]}]
    assert "Plan a weekend in Kyoto please" not in p.seed_names


def test_and_is_never_a_delimiter():
    p = parse_request("Reading list: Klara and the Sun, Neuromancer")
    assert p.seed_lists == [
        {"label": "reading list", "items": ["Klara and the Sun", "Neuromancer"]}]
    assert "Klara and the Sun" in p.seed_names


def test_no_labels_means_no_lists_and_unchanged_parsing():
    p = parse_request("a first date in NYC, I love Kind of Blue")
    assert p.seed_lists == []
    assert p.scenario == "date" and p.location == "New York"
    assert "Kind of Blue (Miles Davis)" in p.seed_names


def test_location_named_in_a_list_is_not_a_seed():
    p = parse_request("Watchlist: Kyoto, Spirited Away")
    assert p.location == "Kyoto"
    assert "Kyoto" not in p.seed_names
    assert "Spirited Away" in p.seed_names


def test_agent_plan_carries_seed_lists_and_still_verifies():
    result = TastePilotAgent().run(
        "a slow weekend in Kyoto. Watchlist: Spirited Away. "
        "Playlist: Kind of Blue, Bon Iver")
    assert result["seed_lists"] == [
        {"label": "watchlist", "items": ["Spirited Away"]},
        {"label": "playlist", "items": ["Kind of Blue", "Bon Iver"]},
    ]
    assert "Spirited Away" in result["seeds"]
    assert "Kind of Blue (Miles Davis)" in result["seeds"]
    assert result["items"] and result["checker"]["passed"] is True
    assert verify_chain(result["evidence"])


def test_unknown_list_title_is_searched_verbatim():
    queries = []

    class RecordingClient(MockQlooClient):
        def search(self, query, limit=10):
            queries.append(query)
            return super().search(query, limit=limit)

    result = TastePilotAgent(qloo_client=RecordingClient()).run(
        "a weekend in Kyoto. Watchlist: An Obscure Reel, Spirited Away")
    assert "An Obscure Reel" in queries
    assert result["seed_lists"][0]["items"][0] == "An Obscure Reel"


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_ui_status_line_reports_seed_lists(server_url):
    with urllib.request.urlopen(server_url + "/") as resp:
        page = resp.read().decode()
    assert "seed_lists" in page and "Lists:" in page
    req = urllib.request.Request(
        server_url + "/api/plan", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"text": "a weekend in Kyoto. "
                                 "Watchlist: Spirited Away"}).encode())
    with urllib.request.urlopen(req) as resp:
        plan = json.loads(resp.read())
    assert plan["seed_lists"] == [
        {"label": "watchlist", "items": ["Spirited Away"]}]
