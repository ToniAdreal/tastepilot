"""Profile sync: a persistent preference profile carried
between devices/servers as a sync document, merged under
deterministic conflict rules.

Until now a profile lived in exactly one store. Now it travels:
export_sync emits a portable document (format, version, owner
FINGERPRINT — never the token — updated-at stamp, profile), and
import_sync / resolve_profile_sync merge two copies of the same
owner's profile. Sync is deliberately NOT the additive lesson
merge: two devices that learned independently hold overlapping
history, so weights resolve by rule (newer document wins; a
timestamp tie goes to the larger absolute weight, then the
larger value) and counts take the element-wise maximum — shared
history is never double-counted — and every disagreement is
reported as a conflict, never silently resolved.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.profiles import (SYNC_FORMAT, ProfileStore,
                                 owner_token_hash, resolve_profile_sync,
                                 validate_sync_document)
from tastepilot.server import OWNER_TOKEN_HEADER, make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
OWNER = "sync-owner-token-0123456789abcdef"
OTHER = "sync-other-token-0123456789abcdef"
GION = "Gion, Kyoto"
MUSEUM = "Kyoto National Museum"
T_OLD = "2026-10-01T00:00:00+00:00"
T_NEW = "2026-10-05T00:00:00+00:00"


def _sel(plan, name):
    for pool in (plan["items"], plan["alternatives"]):
        for item in pool:
            if item["entity"]["name"] == name:
                return {"name": name, "domain": item["domain"],
                        "location": item.get("location")}
    raise KeyError(name)


def _museum_kept():
    plan = TastePilotAgent().run(KYOTO)
    return swap_plan(plan, _sel(plan, GION), _sel(plan, MUSEUM))


def _profile(**overrides):
    profile = {"plans_used": 1, "plans_ignored": 0, "kept_swaps": 1,
               "undone_swaps": 0, "tag_weights": {"quiet": 0.04},
               "domain_weights": {"place": 0.02}}
    profile.update(overrides)
    return profile


def _doc(profile=None, updated_at=T_OLD, token=OWNER, **overrides):
    document = {"format": SYNC_FORMAT, "version": 1,
                "owner_fingerprint": owner_token_hash(token)[:12],
                "updated_at": updated_at,
                "profile": _profile() if profile is None else profile}
    document.update(overrides)
    return document


# --- document gate + resolution rules -------------------------------------


def test_validate_sync_document_normalises_and_clamps():
    document = _doc(profile=_profile(tag_weights={"loud": 0.5}))
    clean = validate_sync_document(document)
    assert clean["profile"]["tag_weights"] == {"loud": 0.12}  # cap
    assert clean["owner_fingerprint"] == owner_token_hash(OWNER)[:12]
    assert clean["updated_at"] == T_OLD


def test_validate_sync_document_refuses_bad_shapes():
    for bad in (None, [], {}, _doc(format="other"), _doc(version=2),
                _doc(owner_fingerprint="xyz"),
                _doc(updated_at="not a date"),
                {**_doc(), "profile": None},
                {**_doc(), "profile": {"tag_weights": {"x": "heavy"}}}):
        with pytest.raises(ValueError):
            validate_sync_document(bad)


def test_resolve_disjoint_weights_merge_without_conflicts():
    local = _doc(profile=_profile(tag_weights={"quiet": 0.04}))
    remote = _doc(profile=_profile(tag_weights={"lively": 0.08},
                                   plans_used=3, kept_swaps=2),
                  updated_at=T_NEW)
    result = resolve_profile_sync(local, remote)
    assert result["conflicts"] == []
    assert result["profile"]["tag_weights"] == \
        {"lively": 0.08, "quiet": 0.04}
    # Counts take the maximum — shared history is not summed.
    assert result["profile"]["plans_used"] == 3
    assert result["profile"]["kept_swaps"] == 2


def test_resolve_conflict_newer_document_wins():
    local = _doc(profile=_profile(tag_weights={"quiet": 0.04}))
    remote = _doc(profile=_profile(tag_weights={"quiet": 0.08}),
                  updated_at=T_NEW)
    result = resolve_profile_sync(local, remote)
    assert result["profile"]["tag_weights"] == {"quiet": 0.08}
    assert result["conflicts"] == [
        {"kind": "tag", "key": "quiet", "local": 0.04, "remote": 0.08,
         "resolved": 0.08, "rule": "newer"}]
    # And the older side wins when IT is the newer document.
    result = resolve_profile_sync(remote, local)
    assert result["profile"]["tag_weights"] == {"quiet": 0.08}


def test_resolve_conflict_timestamp_ties_are_deterministic():
    local = _doc(profile=_profile(tag_weights={"quiet": 0.04}))
    remote = _doc(profile=_profile(tag_weights={"quiet": -0.08}))
    result = resolve_profile_sync(local, remote)  # same stamp
    assert result["profile"]["tag_weights"] == {"quiet": -0.08}
    assert result["conflicts"][0]["rule"] == "larger_absolute"
    local = _doc(profile=_profile(tag_weights={"quiet": 0.08}))
    remote = _doc(profile=_profile(tag_weights={"quiet": -0.08}))
    result = resolve_profile_sync(local, remote)
    assert result["profile"]["tag_weights"] == {"quiet": 0.08}
    assert result["conflicts"][0]["rule"] == "larger_value"


def test_resolve_is_idempotent_and_order_independent():
    local = _doc(profile=_profile(tag_weights={"quiet": 0.04}))
    remote = _doc(profile=_profile(tag_weights={"quiet": 0.08,
                                                 "calm": -0.04}),
                  updated_at=T_NEW)
    same = resolve_profile_sync(local, local)
    assert same["conflicts"] == []
    assert same["profile"] == validate_sync_document(local)["profile"]
    forward = resolve_profile_sync(local, remote)
    backward = resolve_profile_sync(remote, local)
    assert forward["profile"] == backward["profile"]
    assert [c["resolved"] for c in forward["conflicts"]] == \
        [c["resolved"] for c in backward["conflicts"]]


def test_resolve_owner_mismatch_refused():
    with pytest.raises(ValueError, match="owner_mismatch"):
        resolve_profile_sync(_doc(), _doc(token=OTHER))


# --- store export / import ---------------------------------------------------


def test_store_export_shape_and_token_secrecy(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    assert store.export_sync(OWNER) is None
    store.update_from_plans(OWNER, [_museum_kept()])
    document = store.export_sync(OWNER)
    assert document["format"] == SYNC_FORMAT
    assert document["owner_fingerprint"] == owner_token_hash(OWNER)[:12]
    assert document["profile"]["kept_swaps"] == 1
    assert OWNER not in json.dumps(document)


def test_store_import_into_empty_store_adopts(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    result = store.import_sync(OWNER, _doc())
    assert result["conflicts"] == []
    assert result["profile"]["tag_weights"] == {"quiet": 0.04}
    assert store.get(OWNER)["domain_weights"] == {"place": 0.02}


def test_store_import_resolves_conflicts_and_persists(tmp_path):
    path = tmp_path / "profiles.json"
    clock = [1_800_000_000.0]  # 2027-01-15: newer than any test doc
    store = ProfileStore(path=path, clock=lambda: clock[0])
    store.update_from_plans(OWNER, [_museum_kept()])
    stored = store.get(OWNER)
    shared_tag = next(iter(stored["tag_weights"]))
    remote = _doc(profile=_profile(
        tag_weights={shared_tag: 0.01}, plans_used=9),
        updated_at="2027-02-01T00:00:00+00:00")  # newer than the store
    result = store.import_sync(OWNER, remote)
    assert result["profile"]["tag_weights"][shared_tag] == 0.01
    assert any(c["key"] == shared_tag and c["rule"] == "newer"
               for c in result["conflicts"])
    assert result["profile"]["plans_used"] == 9  # max, not a sum
    reopened = ProfileStore(path=path, clock=lambda: clock[0])
    assert reopened.get(OWNER) == result["profile"]


def test_store_import_gates(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    with pytest.raises(ValueError, match="owner_mismatch"):
        store.import_sync(OWNER, _doc(token=OTHER))
    with pytest.raises(ValueError, match="bad_sync_document"):
        store.import_sync(OWNER, {"format": "nope"})
    with pytest.raises(ValueError, match="invalid_owner_token"):
        store.import_sync("short", _doc())


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
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture()
def sync_server(tmp_path):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(TastePilotAgent(),
                     profile_file=tmp_path / "profiles.json"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def test_http_profile_sync_round_trip(sync_server):
    port = sync_server
    # Gates mirror /api/profile: no token → 401, malformed → 400.
    status, _ = _request(port, "/api/profile/export")
    assert status == 401
    status, _ = _request(port, "/api/profile/export", token="short")
    assert status == 400
    # Nothing stored yet → 404.
    status, _ = _request(port, "/api/profile/export", token=OWNER)
    assert status == 404
    # Learn, then export a real document.
    status, _ = _request(port, "/api/profile", method="POST",
                         payload={"history": [_museum_kept()]},
                         token=OWNER)
    assert status == 200
    status, document = _request(port, "/api/profile/export", token=OWNER)
    assert status == 200 and document["format"] == SYNC_FORMAT
    # Clear, then import the document back: profile restored.
    status, _ = _request(port, "/api/profile", method="DELETE",
                         token=OWNER)
    assert status == 200
    status, body = _request(port, "/api/profile/import", method="POST",
                            payload={"document": document}, token=OWNER)
    assert status == 200 and body["conflicts"] == []
    assert body["profile"]["kept_swaps"] == 1
    # Another owner's token cannot import this document.
    status, body = _request(port, "/api/profile/import", method="POST",
                            payload={"document": document}, token=OTHER)
    assert status == 403 and "owner_mismatch" in body["error"]
    # Malformed bodies are categorised 400s, never tracebacks.
    status, body = _request(port, "/api/profile/import", method="POST",
                            payload={"document": {"format": "nope"}},
                            token=OWNER)
    assert status == 400 and "bad_sync_document" in body["error"]
    status, _ = _request(port, "/api/profile/import", method="POST",
                         payload={"document": document})
    assert status == 401


# --- CLI surface ----------------------------------------------------------------


def test_cli_profile_sync_round_trip(tmp_path, capsys):
    store = tmp_path / "profiles.json"
    history = tmp_path / "history.json"
    history.write_text(json.dumps(_museum_kept()), encoding="utf-8")
    base = ["profile", "--store", str(store), "--owner-token", OWNER]
    # Export with nothing stored → exit 1.
    assert cli.main(base + ["--export"]) == 1
    capsys.readouterr()
    assert cli.main(base + ["--learn-from", str(history)]) == 0
    capsys.readouterr()
    assert cli.main(base + ["--export"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["format"] == SYNC_FORMAT
    sync_file = tmp_path / "sync.json"
    sync_file.write_text(json.dumps(document), encoding="utf-8")
    assert cli.main(base + ["--clear"]) == 0
    capsys.readouterr()
    assert cli.main(base + ["--import-file", str(sync_file)]) == 0
    out = capsys.readouterr().out
    assert "conflicts=0" in out and "kept_swaps=1" in out
    # A bad sync file is a categorised exit 2, never a traceback.
    sync_file.write_text("{not json", encoding="utf-8")
    assert cli.main(base + ["--import-file", str(sync_file)]) == 2
    assert "Traceback" not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["profile", "--help"])
    assert "--export" in capsys.readouterr().out


def test_ui_and_docs_describe_profile_sync():
    page = INDEX.read_text()
    assert "profileexportbtn" in page and "profileimportbtn" in page
    assert "/api/profile/export" in page and "/api/profile/import" in page
    assert "sync conflict" in page
    assert "profile sync" in README.read_text().lower()
    assert "conflict" in SUBMISSION.read_text().lower()
    assert "profile sync probe" in DEPLOY.read_text().lower()
