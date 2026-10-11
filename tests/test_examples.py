"""Curated demo examples: the catalogue the demo leads with is one
source of truth (examples.py) served by GET /api/examples, the web
UI's example buttons, and the `examples` CLI subcommand — and every
example is run end-to-end here, so the demo script cannot rot."""
import datetime
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.cli import main as cli_main
from tastepilot.examples import EXAMPLES, get_example, list_examples
from tastepilot.parser import exclusion_match
from tastepilot.server import make_handler

APP = Path(__file__).resolve().parent.parent
REF = datetime.date(2026, 10, 11)


def _run(example):
    ref = REF
    if "reference_date" in example:
        ref = datetime.date.fromisoformat(example["reference_date"])
    return TastePilotAgent().run(example["request"], reference_date=ref)


def test_catalogue_shape():
    examples = list_examples()
    assert len(examples) >= 6
    ids = [e["id"] for e in examples]
    assert len(set(ids)) == len(ids)
    for e in examples:
        assert e["title"] and e["request"]
        assert len(e["request"]) <= 2_000  # the agent's own text cap
        assert isinstance(e["demonstrates"], list) and e["demonstrates"]
        assert all(isinstance(d, str) and d for d in e["demonstrates"])
        if "reference_date" in e:
            datetime.date.fromisoformat(e["reference_date"])


def test_list_examples_returns_copies():
    examples = list_examples()
    examples[0]["title"] = "mutated"
    examples[0]["demonstrates"].append("mutated")
    assert list_examples()[0]["title"] == EXAMPLES[0]["title"]
    assert "mutated" not in list_examples()[0]["demonstrates"]


def test_get_example_round_trip_and_unknown():
    assert get_example(EXAMPLES[0]["id"])["id"] == EXAMPLES[0]["id"]
    assert get_example("no-such-example") is None


@pytest.mark.parametrize("example", list_examples(),
                         ids=[e["id"] for e in list_examples()])
def test_every_example_runs_end_to_end(example):
    plan = _run(example)
    assert len(plan["items"]) >= 4
    assert plan["checker"]["passed"] is True
    assert plan["checker"]["chain_valid"] is True
    assert plan["rejected"] == []


def test_seed_lists_example_captures_all_three_lists():
    plan = _run(get_example("seed-lists"))
    assert [s["label"] for s in plan["seed_lists"]] == \
        ["watchlist", "playlist", "reading list"]


def test_no_jazz_example_excludes_but_stays_evidence_backed():
    plan = _run(get_example("no-jazz"))
    assert plan["exclusions"] == ["jazz"]
    assert plan["excluded"], "the jazz-adjacent item should be excluded"
    for excluded in plan["excluded"]:
        assert excluded["verification"] == "ok"
    for item in plan["items"]:
        assert exclusion_match(item, plan["exclusions"]) is None


def test_multi_city_example_has_two_populated_legs():
    plan = _run(get_example("multi-city"))
    assert [leg["location"] for leg in plan["legs"]] == ["Kyoto", "Paris"]
    for leg in plan["legs"]:
        assert sum(len(g["items"]) for g in leg["groups"]) >= 4


def test_dated_example_schedule_transfers_and_pacing():
    plan = _run(get_example("dated-multi-city"))
    schedule = {s["location"]: s for s in plan["schedule"]}
    assert schedule["Kyoto"]["start"] == "2026-11-03"
    assert schedule["Kyoto"]["end"] == "2026-11-05"
    assert schedule["Paris"]["start"] == "2026-11-06"
    assert schedule["Paris"]["end"] == "2026-11-08"
    assert [(t["from"], t["to"], t["status"])
            for t in plan["transfers"]] == [("Kyoto", "Paris", "ok")]
    assert plan["date_warnings"] == []
    days = [d for leg in plan["itinerary"]["legs"] for d in leg["days"]]
    assert days and all(d["items"] for d in days)


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url):
    with urllib.request.urlopen(url) as res:
        return res.status, res.headers, json.loads(res.read())


def test_examples_endpoint_serves_the_catalogue(server_url):
    status, headers, body = _get(server_url + "/api/examples")
    assert status == 200
    assert body == {"examples": list_examples()}
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_examples_endpoint_is_a_get_and_never_rate_limited(server_url):
    # GETs are never limited: far more reads than the POST quota pass.
    for _ in range(35):
        status, _, _ = _get(server_url + "/api/examples")
        assert status == 200


def test_cli_examples_lists_the_catalogue(capsys):
    assert cli_main(["examples"]) == 0
    out = capsys.readouterr().out
    for e in list_examples():
        assert e["id"] in out
        assert e["request"] in out


def test_ui_builds_example_buttons_from_the_endpoint():
    html = (APP / "tastepilot" / "static" / "index.html").read_text()
    assert 'id="examples"' in html
    assert 'id="examplebtns"' in html
    assert "fetch('/api/examples')" in html
    assert "requestSubmit()" in html


def test_docs_wire_the_examples():
    readme = (APP / "README.md").read_text()
    assert "examples.py" in readme and "GET /api/examples" in readme
    submission = (APP / "SUBMISSION.md").read_text()
    assert "examples.py" in submission
    deploy = (APP / "DEPLOY.md").read_text()
    assert "46. Examples probe" in deploy
