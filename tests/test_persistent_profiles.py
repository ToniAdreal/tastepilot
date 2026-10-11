"""Persistent preference profiles: the learned ranking's profile
carried across plans and sessions as an owner-token artefact.

A profile is writable ONLY from sealed evidence — ProfileStore
derives it with learn_preferences from supplied plans and merges
it under the learner's caps — keyed by the owner token's hash
(the token itself is never stored), optionally file-persistent
across restarts, and folded into suggest_branches as a stored
profile whose adjustments are reported like any other learning.
"""
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
from tastepilot.profiles import (MAX_PROFILE_HISTORY_PLANS, ProfileStore,
                                 owner_token_hash as profile_token_hash)
from tastepilot.server import OWNER_TOKEN_HEADER, make_handler
from tastepilot.swap import (learn_preferences, merge_profiles,
                             suggest_branches, swap_plan,
                             validate_preference_profile)

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"
GITIGNORE = APP_ROOT / ".gitignore"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
OWNER = "profile-owner-token-0123456789abcdef"
OTHER = "profile-other-token-0123456789abcdef"
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


# --- profile arithmetic ---------------------------------------------------


def test_validate_profile_normalises_and_clamps():
    profile = validate_preference_profile({
        "tag_weights": {"art": 0.5, "quiet": 0.0, "kyoto": -0.04},
        "domain_weights": {"place": 0.02},
        "kept_swaps": 3, "undone_swaps": 1,
        "plans_used": 2, "plans_ignored": 0})
    assert profile["tag_weights"] == {"art": 0.12, "kyoto": -0.04}
    assert profile["domain_weights"] == {"place": 0.02}
    assert profile["kept_swaps"] == 3
    for bad, code in ((None, "bad_profile"),
                      ({"tag_weights": {"art": "lots"}},
                       "bad_profile_weights"),
                      ({"tag_weights": {1: 0.04}}, "bad_profile_weights"),
                      ({"kept_swaps": -1}, "bad_profile_counts"),
                      ({"kept_swaps": True}, "bad_profile_counts")):
        with pytest.raises(ValueError, match=code):
            validate_preference_profile(bad)


def test_merge_profiles_adds_and_caps():
    one = learn_preferences([_museum_kept()])
    merged = merge_profiles(one, one, one, one, one)
    assert merged["kept_swaps"] == 5
    assert merged["tag_weights"]["art"] == pytest.approx(0.12)  # cap
    assert merged["domain_weights"]["place"] == pytest.approx(0.06)
    # A single merge returns the profile's own weights unchanged.
    assert merge_profiles(one) == one
    with pytest.raises(ValueError, match="bad_profile"):
        merge_profiles(one, "nope")


# --- ProfileStore ------------------------------------------------------------


def test_store_update_derives_only_from_sealed_history():
    store = ProfileStore()
    result = store.update_from_plans(OWNER, [_museum_kept()])
    assert result["derived"]["kept_swaps"] == 1
    assert result["profile"]["tag_weights"]["art"] == pytest.approx(0.04)
    assert store.get(OWNER) == result["profile"]
    # A chain-broken plan is ignored and counted, and teaches nothing.
    bad = _kaiseki_kept()
    bad["evidence"]["entries"][0]["hash"] = "0" * 64
    result2 = store.update_from_plans(OWNER, [bad])
    assert result2["derived"]["plans_ignored"] == 1
    assert result2["profile"]["kept_swaps"] == 1  # nothing new learned
    assert result2["profile"]["plans_ignored"] == 1  # the refusal counted
    assert store.get(OWNER)["tag_weights"] == result["profile"]["tag_weights"]
    assert store.get(OTHER) is None


def test_store_merges_across_sessions_under_caps():
    store = ProfileStore()
    store.update_from_plans(OWNER, [_museum_kept()])
    store.update_from_plans(OWNER, [_museum_kept()])
    profile = store.get(OWNER)
    assert profile["kept_swaps"] == 2
    assert profile["tag_weights"]["art"] == pytest.approx(0.08)
    for _ in range(4):
        store.update_from_plans(OWNER, [_museum_kept()])
    assert store.get(OWNER)["tag_weights"]["art"] == pytest.approx(0.12)


def test_store_gates_and_clear():
    store = ProfileStore()
    with pytest.raises(ValueError, match="bad_history"):
        store.update_from_plans(OWNER, [])
    with pytest.raises(ValueError, match="bad_history"):
        store.update_from_plans(OWNER, "nope")
    with pytest.raises(ValueError, match="too_many_history_plans"):
        store.update_from_plans(
            OWNER, [_kyoto()] * (MAX_PROFILE_HISTORY_PLANS + 1))
    with pytest.raises(ValueError, match="invalid_owner_token"):
        store.update_from_plans("short", [_museum_kept()])
    with pytest.raises(ValueError, match="invalid_owner_token"):
        store.get(None)
    assert store.clear(OWNER) is False
    store.update_from_plans(OWNER, [_museum_kept()])
    assert store.clear(OWNER) is True
    assert store.get(OWNER) is None


def test_store_persistence_and_token_secrecy(tmp_path):
    path = tmp_path / "profiles.json"
    ProfileStore(path=path).update_from_plans(OWNER, [_museum_kept()])
    raw = path.read_text(encoding="utf-8")
    assert OWNER not in raw  # the token itself is never stored
    assert profile_token_hash(OWNER) in raw  # only its hash keys it
    reopened = ProfileStore(path=path)
    assert reopened.get(OWNER)["tag_weights"]["art"] == pytest.approx(0.04)
    assert reopened.info() == {"persistent": True, "count": 1, "max": 256}
    assert reopened.clear(OWNER) is True
    assert ProfileStore(path=path).get(OWNER) is None


def test_store_tolerates_corrupt_and_malformed_files(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{not json", encoding="utf-8")
    store = ProfileStore(path=path)  # must not raise
    assert store.info()["count"] == 0
    path.write_text(json.dumps({"version": 1, "profiles": [
        {"owner_hash": "short", "profile": {}, "updated_at": 1.0},
        {"owner_hash": profile_token_hash(OWNER),
         "profile": {"tag_weights": {"art": "lots"}}, "updated_at": 1.0},
        {"owner_hash": profile_token_hash(OTHER),
         "profile": {"tag_weights": {"art": 0.04}, "kept_swaps": 1},
         "updated_at": 2.0},
    ]}), encoding="utf-8")
    store = ProfileStore(path=path)
    assert store.get(OWNER) is None  # bad weights dropped, not repaired
    assert store.get(OTHER)["tag_weights"] == {"art": 0.04}
    # The store still works after loading a partially bad file.
    store.update_from_plans(OWNER, [_museum_kept()])
    assert store.get(OWNER)["kept_swaps"] == 1


def test_store_is_bounded():
    store = ProfileStore(max_profiles=2)
    for i in range(3):
        store.update_from_plans(f"bounded-owner-token-{i:04d}-abcdefghij",
                                [_museum_kept()])
    assert store.info()["count"] == 2
    assert store.get("bounded-owner-token-0000-abcdefghij") is None
    assert store.get("bounded-owner-token-0002-abcdefghij") is not None


# --- stored profile in the ranking -------------------------------------------


def test_stored_profile_applies_to_a_fresh_plan():
    store = ProfileStore()
    store.update_from_plans(OWNER, [_kaiseki_kept()])
    stored = store.get(OWNER)
    plain = suggest_branches(_kyoto())
    assert plain["learning"]["stored_applied"] is False
    learned = suggest_branches(_kyoto(), stored_profile=stored)
    assert learned["learning"]["stored_applied"] is True
    top = learned["candidates"][0]
    assert (top["primary"], top["alternative"]) == (IZAKAYA, KAISEKI)
    assert top["learned_adjustment"] == pytest.approx(0.18)
    # A malformed stored profile is refused, never believed.
    with pytest.raises(ValueError, match="bad_profile_weights"):
        suggest_branches(_kyoto(), stored_profile={"tag_weights": {"x": "y"}})


# --- HTTP surface -------------------------------------------------------------


def _request(port, path, method="GET", payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[OWNER_TOKEN_HEADER] = token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read()), \
                response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


@pytest.fixture()
def profile_server(tmp_path):
    path = tmp_path / "profiles.json"
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(TastePilotAgent(), profile_file=path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], path
    server.shutdown()


def test_http_profile_round_trip_and_suggest(profile_server):
    port, path = profile_server
    # Token gates: none → 401, malformed → 400.
    status, body, _ = _request(port, "/api/profile")
    assert status == 401
    status, body, _ = _request(port, "/api/profile", token="short")
    assert status == 400 and "invalid owner token" in body["error"]
    # Empty profile reads as exists=false with a zero profile.
    status, body, headers = _request(port, "/api/profile", token=OWNER)
    assert status == 200 and body["exists"] is False
    assert body["profile"]["tag_weights"] == {}
    assert headers.get("Cache-Control") == "no-store"
    # Save the lesson of a swapped plan.
    swapped = _museum_kept()
    status, body, _ = _request(port, "/api/profile", method="POST",
                               payload={"history": [swapped]}, token=OWNER)
    assert status == 200 and body["derived"]["kept_swaps"] == 1
    assert body["profile"]["tag_weights"]["art"] == pytest.approx(0.04)
    # It persists (a fresh store over the same file sees it) and the
    # token itself is nowhere in the file.
    assert OWNER not in path.read_text(encoding="utf-8")
    assert ProfileStore(path=path).get(OWNER) is not None
    # Health reports the profile store.
    status, body, _ = _request(port, "/api/health")
    assert body["profiles"] == {"persistent": True, "count": 1, "max": 256}
    # Suggest without a token is unchanged; with the owner's token
    # the stored profile applies and is reported.
    status, plan, _ = _request(port, "/api/plan", method="POST",
                               payload={"text": KYOTO})
    assert status == 200
    status, plain, _ = _request(port, "/api/suggest", method="POST",
                                payload={"plan": plan})
    assert plain["learning"]["stored_applied"] is False
    status, learned, _ = _request(port, "/api/suggest", method="POST",
                                  payload={"plan": plan}, token=OWNER)
    assert learned["learning"]["stored_applied"] is True
    museum = _cand(learned, "Arashiyama Bamboo Grove", MUSEUM)
    # All four museum tags (+0.04 each) plus the place domain (+0.02).
    assert museum["learned_adjustment"] == pytest.approx(0.18)
    # Another token gets no profile and no stored learning.
    status, foreign, _ = _request(port, "/api/suggest", method="POST",
                                  payload={"plan": plan}, token=OTHER)
    assert foreign["learning"]["stored_applied"] is False
    # Clear: 200 once, then the profile is gone (GET + DELETE 404).
    status, body, _ = _request(port, "/api/profile", method="DELETE",
                               token=OWNER)
    assert status == 200 and body["cleared"] is True
    status, body, _ = _request(port, "/api/profile", token=OWNER)
    assert body["exists"] is False
    status, body, _ = _request(port, "/api/profile", method="DELETE",
                               token=OWNER)
    assert status == 404


def test_http_profile_update_gates(profile_server):
    port, _ = profile_server
    status, body, _ = _request(port, "/api/profile", method="POST",
                               payload={"history": [_museum_kept()]})
    assert status == 401  # no token
    for payload, code in (
            ({"history": []}, "bad_history"),
            ({"history": "nope"}, "bad_history"),
            ({"history": [{"x": 1}]}, "bad_history"),
            ({"history": [_kyoto()] * (MAX_PROFILE_HISTORY_PLANS + 1)},
             "too_many_history_plans")):
        status, body, _ = _request(port, "/api/profile", method="POST",
                                   payload=payload, token=OWNER)
        assert status == 400 and code in body["error"]
    # A chain-broken history plan is ignored and counted, not a 400.
    bad = _museum_kept()
    bad["evidence"]["entries"][0]["hash"] = "0" * 64
    status, body, _ = _request(port, "/api/profile", method="POST",
                               payload={"history": [bad]}, token=OWNER)
    assert status == 200 and body["derived"]["plans_ignored"] == 1
    assert body["profile"]["tag_weights"] == {}
    # DELETE on other routes stays 405.
    status, body, _ = _request(port, "/api/plan", method="DELETE")
    assert status == 405


# --- CLI ---------------------------------------------------------------------


def test_cli_profile_round_trip(tmp_path, capsys):
    store = tmp_path / "profiles.json"
    history = tmp_path / "history.json"
    history.write_text(json.dumps(_kaiseki_kept()), encoding="utf-8")
    base = ["profile", "--store", str(store), "--owner-token", OWNER]
    rc = cli.main(base + ["--learn-from", str(history)])
    out = capsys.readouterr().out
    assert rc == 0 and "profile:" in out and "kept_swaps=1" in out
    assert OWNER not in store.read_text(encoding="utf-8")
    rc = cli.main(base + ["--show"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["kept_swaps"] == 1
    assert shown["domain_weights"] == {"food": 0.02}
    # Suggest folds the stored profile in.
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_kyoto()), encoding="utf-8")
    rc = cli.main(["suggest", "--plan", str(plan_path),
                   "--profile-store", str(store), "--owner-token", OWNER])
    out = capsys.readouterr().out
    assert rc == 0 and "stored_applied=True" in out and KAISEKI in out
    # Clear, then suggest has no profile to apply.
    rc = cli.main(base + ["--clear"])
    assert rc == 0 and "cleared=True" in capsys.readouterr().out
    rc = cli.main(["suggest", "--plan", str(plan_path),
                   "--profile-store", str(store), "--owner-token", OWNER])
    assert rc == 2
    rc = cli.main(base + ["--clear"])
    assert rc == 1
    # Token and file failures are categorised, never tracebacks.
    rc = cli.main(["profile", "--store", str(store),
                   "--owner-token", "short", "--show"])
    assert rc == 2
    rc = cli.main(base + ["--learn-from", str(tmp_path / "missing.json")])
    assert rc == 2
    assert "Traceback" not in capsys.readouterr().err


def test_cli_serve_help_offers_profile_file(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--help"])
    assert excinfo.value.code == 0
    assert "--profile-file" in capsys.readouterr().out


def test_ui_and_docs_describe_persistent_profiles():
    page = INDEX.read_text()
    assert "profilebox" in page and "Your preference profile" in page
    assert "/api/profile" in page and "stored profile applied" in page
    assert "profile" in GITIGNORE.read_text()
    readme = README.read_text()
    assert "profiles.py" in readme and "Persistent preference" in readme
    submission = SUBMISSION.read_text()
    assert "Persistent preference profiles" in submission
    assert "Cloud-synced" in submission
    deploy = DEPLOY.read_text()
    assert "TASTEPILOT_PROFILE_FILE" in deploy
    assert "probe 16" in deploy.lower() or "Profile probe" in deploy
