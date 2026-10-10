"""Persistent shared-plan storage with expiry and revocation.

The share store's third act (SUBMISSION "What's next", built): links
can outlive the demo process (file-backed store, atomic writes),
every link expires after its TTL, and a link holder can revoke a link
with DELETE. Expiry and revocation never grant or remove trust in the
plan itself — verification is still recomputed on every read — they
only govern whether the stored plan is served at all.
"""
import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.cli import main as cli_main
from tastepilot.server import (DEFAULT_SHARE_TTL_SECONDS, ShareStore,
                               make_handler, share_id_for)

APP_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = APP_ROOT / "samples" / "plan_weekend_kyoto.json"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post_json(url, obj):
    return _request(url, method="POST", body=json.dumps(obj).encode(),
                    headers={"Content-Type": "application/json"})


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def sample_plan():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# --- ShareStore unit behaviour ----------------------------------------------

def test_store_put_get_round_trip_and_meta(sample_plan):
    store = ShareStore(clock=FakeClock())
    meta = store.put(sample_plan)
    assert meta["share_id"] == share_id_for(sample_plan)
    assert meta["expires_at"].startswith("2023-11-")  # fake clock + 7d
    assert store.get(meta["share_id"]) == sample_plan
    assert store.meta(meta["share_id"]) == meta
    assert store.info() == {"persistent": False, "count": 1, "max": 128,
                            "ttl_seconds": DEFAULT_SHARE_TTL_SECONDS}


def test_store_returns_deep_copies(sample_plan):
    store = ShareStore(clock=FakeClock())
    meta = store.put(sample_plan)
    got = store.get(meta["share_id"])
    got["items"][0]["why"] = "mutated by the caller"
    assert store.get(meta["share_id"]) == sample_plan


def test_store_expires_after_ttl(sample_plan):
    clock = FakeClock()
    store = ShareStore(ttl_seconds=60, clock=clock)
    meta = store.put(sample_plan)
    clock.advance(59)
    assert store.get(meta["share_id"]) == sample_plan
    clock.advance(2)  # now past the expiry
    assert store.get(meta["share_id"]) is None
    assert store.meta(meta["share_id"]) is None
    assert store.revoke(meta["share_id"]) is False  # nothing left to revoke
    assert store.info()["count"] == 0


def test_re_store_does_not_extend_expiry(sample_plan):
    clock = FakeClock()
    store = ShareStore(ttl_seconds=60, clock=clock)
    first = store.put(sample_plan)
    clock.advance(50)
    second = store.put(sample_plan)  # idempotent: same id, same expiry
    assert second == first
    clock.advance(11)
    assert store.get(first["share_id"]) is None


def test_store_revoke(sample_plan):
    store = ShareStore(clock=FakeClock())
    meta = store.put(sample_plan)
    assert store.revoke(meta["share_id"]) is True
    assert store.get(meta["share_id"]) is None
    assert store.revoke(meta["share_id"]) is False


def test_store_rejects_bad_config():
    with pytest.raises(ValueError):
        ShareStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        ShareStore(max_plans=0)


# --- persistence across restarts ---------------------------------------------

def test_store_persists_across_instances(tmp_path, sample_plan):
    path = tmp_path / "shares.json"
    clock = FakeClock()
    meta = ShareStore(path=path, clock=clock).put(sample_plan)
    assert path.is_file()
    # The file holds the plan and its stamps — and no key material.
    raw = path.read_text(encoding="utf-8")
    assert "QLOO_API_KEY" not in raw

    reopened = ShareStore(path=path, clock=clock)
    assert reopened.persistent is True
    assert reopened.get(meta["share_id"]) == sample_plan
    assert reopened.meta(meta["share_id"]) == meta
    # Revocation persists too.
    assert reopened.revoke(meta["share_id"]) is True
    assert ShareStore(path=path, clock=clock).get(meta["share_id"]) is None


def test_store_persistence_drops_expired_entries_on_load(tmp_path,
                                                         sample_plan):
    path = tmp_path / "shares.json"
    clock = FakeClock()
    ShareStore(path=path, ttl_seconds=60, clock=clock).put(sample_plan)
    clock.advance(3600)
    reopened = ShareStore(path=path, clock=clock)
    assert reopened.info()["count"] == 0


def test_store_tolerates_corrupt_file(tmp_path, sample_plan):
    path = tmp_path / "shares.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = ShareStore(path=path, clock=FakeClock())  # must not raise
    assert store.info()["count"] == 0
    meta = store.put(sample_plan)  # and the store still works afterwards
    assert store.get(meta["share_id"]) == sample_plan
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_store_skips_malformed_rows_on_load(tmp_path, sample_plan):
    path = tmp_path / "shares.json"
    good = ShareStore(path=path, clock=FakeClock()).put(sample_plan)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"].append({"share_id": 42, "payload": "nope"})
    data["entries"].append("garbage")
    path.write_text(json.dumps(data), encoding="utf-8")
    reopened = ShareStore(path=path, clock=FakeClock())
    assert reopened.info()["count"] == 1
    assert reopened.get(good["share_id"]) == sample_plan


# --- HTTP surface --------------------------------------------------------------

@pytest.fixture()
def clocked_server(tmp_path):
    clock = FakeClock()
    path = tmp_path / "shares.json"
    handler = make_handler(TastePilotAgent(), share_ttl_seconds=60,
                           share_file=path, share_clock=clock)
    server, url = _serve(handler)
    yield url, clock, path
    server.shutdown()


def test_http_share_reports_expiry_and_health(clocked_server, sample_plan):
    url, _, _ = clocked_server
    status, _, raw = _post_json(url + "/api/share", sample_plan)
    assert status == 201
    created = json.loads(raw)
    assert created["expires_at"].startswith("2023-11-")
    status, _, raw = _request(url + created["verify_url"])
    assert status == 200
    report = json.loads(raw)
    assert report["passed"] is True
    assert report["expires_at"] == created["expires_at"]
    status, _, raw = _request(url + "/api/health")
    health = json.loads(raw)
    assert health["shares"]["persistent"] is True
    assert health["shares"]["count"] == 1
    assert health["shares"]["ttl_seconds"] == 60


def test_http_expired_link_404s(clocked_server, sample_plan):
    url, clock, _ = clocked_server
    _, _, raw = _post_json(url + "/api/share", sample_plan)
    share_id = json.loads(raw)["share_id"]
    clock.advance(61)
    for suffix in ("", "/verify"):
        status, _, raw = _request(url + f"/api/share/{share_id}" + suffix)
        assert status == 404
        assert json.loads(raw)["error"] == "unknown share id"


def test_http_revoke_by_delete(clocked_server, sample_plan):
    url, _, _ = clocked_server
    _, _, raw = _post_json(url + "/api/share", sample_plan)
    share_id = json.loads(raw)["share_id"]
    status, headers, raw = _request(url + f"/api/share/{share_id}",
                                    method="DELETE")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"
    assert json.loads(raw) == {"revoked": True, "share_id": share_id}
    status, _, _ = _request(url + f"/api/share/{share_id}")
    assert status == 404  # the revoked link is dead for everyone
    status, _, raw = _request(url + f"/api/share/{share_id}",
                              method="DELETE")
    assert status == 404  # revoking twice revokes nothing
    assert json.loads(raw)["error"] == "unknown share id"


def test_http_delete_elsewhere_stays_405(clocked_server):
    url, _, _ = clocked_server
    for target in ("/api/plan", "/api/verify", "/"):
        status, _, raw = _request(url + target, method="DELETE")
        assert status == 405
        assert json.loads(raw)["error"] == "method not allowed"


def test_http_links_survive_a_restart(tmp_path, sample_plan):
    path = tmp_path / "shares.json"
    server, url = _serve(make_handler(TastePilotAgent(), share_file=path))
    try:
        _, _, raw = _post_json(url + "/api/share", sample_plan)
        share_id = json.loads(raw)["share_id"]
    finally:
        server.shutdown()
    server, url = _serve(make_handler(TastePilotAgent(), share_file=path))
    try:
        status, _, raw = _request(url + f"/api/share/{share_id}")
        assert status == 200
        assert json.loads(raw) == sample_plan
        status, _, raw = _request(url + f"/api/share/{share_id}/verify")
        assert json.loads(raw)["passed"] is True
    finally:
        server.shutdown()


def test_ui_page_carries_revoke_wiring(clocked_server):
    url, _, _ = clocked_server
    status, _, raw = _request(url + "/")
    assert status == 200
    page = raw.decode()
    assert 'id="revokebtn"' in page
    assert "method: 'DELETE'" in page
    assert "expires" in page


# --- CLI wiring ------------------------------------------------------------------

def test_cli_serve_help_offers_share_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["serve", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--share-file" in out and "--share-ttl-days" in out
