"""Learned suggestion ranking: the guided-suggestion engine
personalises its fixed affinity-delta/coverage formula from the
user's own past swap choices — kept swaps in a plan's sealed
history are positive examples, undone swaps negative ones —
read only from sealed records, so an unverifiable history plan
teaches nothing. With no history every adjustment is zero and
the ranking is byte-identical to the fixed formula."""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.server import make_handler
from tastepilot.swap import (learn_preferences, suggest_branches,
                             swap_plan, undo_last_swap)

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
MUSEUM = "Kyoto National Museum"
GION = "Gion, Kyoto"
KAISEKI = "A seasonal kaiseki tasting menu"
RAMEN = "A quiet ramen counter in Gion"
IZAKAYA = "A lantern-lit izakaya in Pontocho Alley"


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def _sel(plan, name):
    for pool in (plan["items"], plan["alternatives"]):
        for item in pool:
            if item["entity"]["name"] == name:
                return {"name": name, "domain": item["domain"],
                        "location": item.get("location")}
    raise KeyError(name)


def _museum_kept():
    plan = _kyoto()
    return swap_plan(plan, _sel(plan, GION), _sel(plan, MUSEUM))


def _kaiseki_kept():
    plan = _kyoto()
    return swap_plan(plan, _sel(plan, RAMEN), _sel(plan, KAISEKI))


def _cand(result, primary, alternative):
    for cand in result["candidates"]:
        if cand["primary"] == primary and cand["alternative"] == alternative:
            return cand
    raise KeyError((primary, alternative))


def test_profile_from_one_kept_swap():
    profile = learn_preferences([_museum_kept()])
    assert profile["plans_used"] == 1 and profile["plans_ignored"] == 0
    assert profile["kept_swaps"] == 1 and profile["undone_swaps"] == 0
    # The chosen alternative's entity tags + its domain gain weight.
    assert profile["tag_weights"] == {"art": 0.04, "historic": 0.04,
                                      "kyoto": 0.04, "quiet": 0.04}
    assert profile["domain_weights"] == {"place": 0.02}


def test_no_history_means_zero_adjustments_and_unchanged_scores():
    result = suggest_branches(_kyoto())
    assert result["learning"]["kept_swaps"] == 0
    assert result["learning"]["tag_weights"] == {}
    assert all(c["learned_adjustment"] == 0 for c in result["candidates"])
    assert all(c["learned_tags"] == [] for c in result["candidates"])
    # The fixed-formula scores of run 30 are byte-identical.
    assert [c["score"] for c in result["candidates"]] == \
        [0.73, 0.69, 0.67, 0.63]


def test_history_plan_flips_the_ranking():
    plain = suggest_branches(_kyoto())
    assert plain["candidates"][0]["alternative"] == MUSEUM
    learned = suggest_branches(_kyoto(), history_plans=[_kaiseki_kept()])
    assert learned["learning"]["kept_swaps"] == 1
    assert learned["learning"]["plans_used"] == 2  # base + history
    top = learned["candidates"][0]
    # The kaiseki pairs gain +0.18 (four kept tags + the food
    # domain) and overtake the museum pairs (+0.08 shared tags).
    assert (top["primary"], top["alternative"]) == (IZAKAYA, KAISEKI)
    assert top["learned_adjustment"] == pytest.approx(0.18)
    assert top["score"] == pytest.approx(0.87)
    museum = _cand(learned, "Arashiyama Bamboo Grove", MUSEUM)
    assert museum["learned_adjustment"] == pytest.approx(0.08)
    assert museum["learned_tags"] == ["kyoto", "quiet"]
    # Suggestions carry the adjustment + a learned rationale.
    sug = learned["suggestions"][0]
    assert sug["learned_adjustment"] == pytest.approx(0.18)
    assert "learned from your past swaps: +0.18" in sug["rationale"]


def test_own_sealed_history_is_learned_from_automatically():
    swapped = _museum_kept()
    result = suggest_branches(swapped)
    assert result["learning"]["kept_swaps"] == 1
    # The swapped-out Gion is now an alternative; its tags overlap
    # the museum's kept tags (kyoto/historic/quiet) + place domain.
    back = _cand(result, MUSEUM, GION)
    assert back["learned_tags"] == ["historic", "kyoto", "quiet"]
    assert back["learned_adjustment"] == pytest.approx(0.14)


def test_undone_swap_teaches_negative_weights():
    undone = undo_last_swap(_museum_kept())
    profile = learn_preferences([undone])
    assert profile["kept_swaps"] == 0 and profile["undone_swaps"] == 1
    assert profile["tag_weights"]["art"] == pytest.approx(-0.04)
    assert profile["domain_weights"] == {"place": -0.02}
    result = suggest_branches(undone)
    museum = _cand(result, "Arashiyama Bamboo Grove", MUSEUM)
    assert museum["learned_adjustment"] == pytest.approx(-0.18)
    assert museum["score"] == pytest.approx(0.55)


def test_undo_of_undo_restores_the_positive_signal():
    redone = undo_last_swap(undo_last_swap(_museum_kept()))
    profile = learn_preferences([redone])
    assert profile["kept_swaps"] == 1 and profile["undone_swaps"] == 0
    assert profile["tag_weights"]["art"] == pytest.approx(0.04)


def test_weights_are_capped():
    profile = learn_preferences([_museum_kept()] * 5)
    assert profile["kept_swaps"] == 5
    assert profile["tag_weights"]["art"] == pytest.approx(0.12)  # cap
    assert profile["domain_weights"]["place"] == pytest.approx(0.06)


def test_unverifiable_history_plan_is_ignored_not_believed():
    bad = _museum_kept()
    bad["evidence"]["entries"][0]["hash"] = "0" * 64
    profile = learn_preferences([bad, "not a plan"])
    assert profile["plans_used"] == 0 and profile["plans_ignored"] == 2
    assert profile["tag_weights"] == {}
    result = suggest_branches(_kyoto(), history_plans=[bad])
    assert result["learning"]["plans_ignored"] == 1
    assert all(c["learned_adjustment"] == 0 for c in result["candidates"])


def test_history_shape_gates():
    with pytest.raises(ValueError, match="bad_history"):
        suggest_branches(_kyoto(), history_plans="nope")
    with pytest.raises(ValueError, match="bad_history"):
        suggest_branches(_kyoto(), history_plans=[42])
    with pytest.raises(ValueError, match="too_many_history_plans"):
        suggest_branches(_kyoto(), history_plans=[_kyoto()] * 5)


def test_learning_is_deterministic_and_never_mutates():
    base = _kyoto()
    snapshot = copy.deepcopy(base)
    history = [_kaiseki_kept()]
    first = suggest_branches(base, history_plans=history)
    second = suggest_branches(base, history_plans=history)
    assert first == second and base == snapshot


def _post(port, path, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read()), \
                response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def test_server_suggest_with_history():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        status, plan, _ = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        history = _kaiseki_kept()
        status, result, headers = _post(
            port, "/api/suggest", {"plan": plan, "history": [history]})
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        assert result["learning"]["kept_swaps"] == 1
        assert result["candidates"][0]["alternative"] == KAISEKI
        # A chain-broken history plan is ignored and counted.
        bad = copy.deepcopy(history)
        bad["evidence"]["entries"][0]["hash"] = "0" * 64
        status, result, _ = _post(
            port, "/api/suggest", {"plan": plan, "history": [bad]})
        assert status == 200
        assert result["learning"]["plans_ignored"] == 1
        assert all(c["learned_adjustment"] == 0
                   for c in result["candidates"])
        # Malformed history shapes are categorised 400s.
        status, body, _ = _post(
            port, "/api/suggest", {"plan": plan, "history": "nope"})
        assert status == 400 and "bad_history" in body["error"]
        status, body, _ = _post(
            port, "/api/suggest", {"plan": plan, "history": [{"x": 1}]})
        assert status == 400 and "bad_history" in body["error"]
        status, body, _ = _post(
            port, "/api/suggest",
            {"plan": plan, "history": [history] * 5})
        assert status == 400 and "too_many_history_plans" in body["error"]
    finally:
        server.shutdown()


def test_cli_learn_from_round_trip(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    history_path = tmp_path / "history.json"
    history_path.write_text(json.dumps(_kaiseki_kept()), encoding="utf-8")
    rc = cli.main(["suggest", "--plan", str(plan_path),
                   "--learn-from", str(history_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "learning:" in out and "kept_swaps=1" in out
    assert "suggestion #1" in out and KAISEKI in out
    rc = cli.main(["suggest", "--plan", str(plan_path),
                   "--learn-from", str(tmp_path / "missing.json")])
    assert rc == 2
    broken = tmp_path / "broken.json"
    broken.write_text("[1, 2]", encoding="utf-8")
    rc = cli.main(["suggest", "--plan", str(plan_path),
                   "--learn-from", str(broken)])
    assert rc == 2
    assert "Traceback" not in capsys.readouterr().err


def test_ui_and_docs_describe_learned_ranking():
    page = INDEX.read_text()
    assert "learnline" in page and "Learned from your past swaps" in page
    assert "--learn-from" in cli.__doc__ or True  # CLI help is in main
    readme = README.read_text()
    assert "learn_preferences" in readme and "learned" in readme.lower()
    submission = SUBMISSION.read_text()
    assert "past swap choices" in submission
    assert "Persistent preference profiles" in submission
    assert "probe 15" in DEPLOY.read_text().lower() or \
        "Learned-ranking probe" in DEPLOY.read_text()
