"""Account-owned share management (SUBMISSION "What's next", built).

Share links gain an owner without any account system: a client sends
an X-Owner-Token bearer credential when creating a share (the demo UI
generates one per browser and keeps it in localStorage), the server
stores only the token's SHA-256 hash, GET /api/shares lists just that
token's links with their recomputed verdicts, and only that token can
revoke an owned link. Legacy shares — created with no token — keep
the original link-holder behaviour and appear in no owner's list.
Ownership, like expiry, governs only whether a stored plan is served
and who may manage it; verification is still recomputed on every read.
"""
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.server import (OWNER_TOKEN_HEADER, ShareStore,
                               make_handler, owner_token_hash)

APP_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = APP_ROOT / "samples" / "plan_weekend_kyoto.json"
SAMPLE2 = APP_ROOT / "samples" / "plan_gift_amelie.json"

OWNER = "owner-token-alpha-0123456789"
OTHER = "owner-token-bravo-0123456789"


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


def _post_json(url, obj, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[OWNER_TOKEN_HEADER] = token
    return _request(url, method="POST", body=json.dumps(obj).encode(),
                    headers=headers)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def server_url():
    server, url = _serve(make_handler(TastePilotAgent()))
    yield url
    server.shutdown()


@pytest.fixture()
def sample_plan():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


@pytest.fixture()
def other_plan():
    return json.loads(SAMPLE2.read_text(encoding="utf-8"))


# --- token hashing ------------------------------------------------------------

def test_owner_token_hash_rules():
    assert owner_token_hash(OWNER) == \
        hashlib.sha256(OWNER.encode()).hexdigest()
    assert owner_token_hash(OWNER) != OWNER  # the hash is not the token
    assert owner_token_hash(None) is None
    assert owner_token_hash("short") is None
    assert owner_token_hash("has a space in it ok") is None
    assert owner_token_hash("x" * 201) is None
    assert owner_token_hash(12345) is None


# --- ShareStore ownership -------------------------------------------------------

def test_store_owned_put_lists_only_for_owner(sample_plan, other_plan):
    store = ShareStore(clock=FakeClock())
    owned = store.put(sample_plan, owner_token=OWNER)
    assert owned["owned"] is True
    legacy = store.put(other_plan)
    assert legacy["owned"] is False
    mine = store.list_for_owner(OWNER)
    assert [m["share_id"] for m in mine] == [owned["share_id"]]
    assert mine[0]["owned"] is True
    assert mine[0]["item_count"] == len(sample_plan["items"])
    assert store.list_for_owner(OTHER) == []
    assert store.list_for_owner(None) == []
    assert store.list_for_owner("short") == []


def test_store_first_token_owns_and_restoring_transfers_nothing(sample_plan):
    store = ShareStore(clock=FakeClock())
    first = store.put(sample_plan, owner_token=OWNER)
    second = store.put(sample_plan, owner_token=OTHER)
    assert second == first  # same meta: id, expiry, and ownership kept
    assert store.list_for_owner(OTHER) == []
    assert [m["share_id"] for m in store.list_for_owner(OWNER)] == \
        [first["share_id"]]
    # An unowned first store stays unowned too: a later token cannot
    # claim a legacy share by re-posting it.
    store2 = ShareStore(clock=FakeClock())
    legacy = store2.put(sample_plan)
    claimed = store2.put(sample_plan, owner_token=OWNER)
    assert claimed == legacy and claimed["owned"] is False
    assert store2.list_for_owner(OWNER) == []


def test_store_revoke_requires_owner_for_owned_shares(sample_plan):
    store = ShareStore(clock=FakeClock())
    meta = store.put(sample_plan, owner_token=OWNER)
    sid = meta["share_id"]
    assert store.revoke_status(sid) == "forbidden"
    assert store.revoke_status(sid, owner_token=OTHER) == "forbidden"
    assert store.revoke(sid, owner_token=OTHER) is False
    assert store.get(sid) == sample_plan  # failed revokes change nothing
    assert store.revoke_status(sid, owner_token=OWNER) == "revoked"
    assert store.get(sid) is None
    assert store.revoke_status(sid, owner_token=OWNER) == "not_found"


def test_store_legacy_revoke_still_open_to_link_holder(sample_plan):
    store = ShareStore(clock=FakeClock())
    meta = store.put(sample_plan)
    assert store.revoke(meta["share_id"]) is True


def test_store_owner_hash_persists_but_token_never_does(tmp_path,
                                                       sample_plan):
    path = tmp_path / "shares.json"
    clock = FakeClock()
    meta = ShareStore(path=path, clock=clock).put(sample_plan,
                                                  owner_token=OWNER)
    raw = path.read_text(encoding="utf-8")
    assert OWNER not in raw  # the credential itself is never persisted
    assert owner_token_hash(OWNER) in raw  # only its hash
    reopened = ShareStore(path=path, clock=clock)
    assert [m["share_id"] for m in reopened.list_for_owner(OWNER)] == \
        [meta["share_id"]]
    assert reopened.revoke_status(meta["share_id"],
                                  owner_token=OTHER) == "forbidden"
    assert reopened.revoke(meta["share_id"], owner_token=OWNER) is True
    assert ShareStore(path=path, clock=clock).get(meta["share_id"]) is None


def test_store_owned_list_prunes_expired(sample_plan):
    clock = FakeClock()
    store = ShareStore(ttl_seconds=60, clock=clock)
    store.put(sample_plan, owner_token=OWNER)
    clock.advance(61)
    assert store.list_for_owner(OWNER) == []


# --- HTTP surface -----------------------------------------------------------------

def test_http_create_with_token_reports_owned(server_url, sample_plan):
    status, headers, raw = _post_json(server_url + "/api/share",
                                      sample_plan, token=OWNER)
    assert status == 201
    assert headers.get("Cache-Control") == "no-store"
    created = json.loads(raw)
    assert created["owned"] is True
    status, _, raw = _post_json(server_url + "/api/share", sample_plan)
    assert status == 201
    # Re-posting without a token keeps the original ownership.
    assert json.loads(raw)["owned"] is True


def test_http_create_with_invalid_token_is_400(server_url, sample_plan):
    status, _, raw = _post_json(server_url + "/api/share", sample_plan,
                                token="short")
    assert status == 400
    assert json.loads(raw)["error"] == "invalid owner token"


def test_http_shares_list_is_owner_scoped(server_url, sample_plan,
                                          other_plan):
    _, _, raw = _post_json(server_url + "/api/share", sample_plan,
                           token=OWNER)
    mine_id = json.loads(raw)["share_id"]
    _post_json(server_url + "/api/share", other_plan, token=OTHER)
    _post_json(server_url + "/api/share",
               json.loads(SAMPLE.read_text(encoding="utf-8")))  # legacy
    status, _, raw = _request(server_url + "/api/shares",
                              headers={OWNER_TOKEN_HEADER: OWNER})
    assert status == 200
    data = json.loads(raw)
    assert data["count"] == 1
    (entry,) = data["shares"]
    assert entry["share_id"] == mine_id
    assert entry["url"] == f"/?share={mine_id}"
    assert entry["verify_url"] == f"/api/share/{mine_id}/verify"
    assert entry["item_count"] == len(sample_plan["items"])
    assert entry["passed"] is True and entry["chain_valid"] is True
    assert entry["owned"] is True and "expires_at" in entry
    # The other owner's list holds only their share.
    status, _, raw = _request(server_url + "/api/shares",
                              headers={OWNER_TOKEN_HEADER: OTHER})
    others = json.loads(raw)["shares"]
    assert len(others) == 1 and others[0]["share_id"] != mine_id


def test_http_shares_requires_a_valid_token(server_url):
    status, _, raw = _request(server_url + "/api/shares")
    assert status == 401
    assert json.loads(raw)["error"] == "owner token required"
    status, _, raw = _request(server_url + "/api/shares",
                              headers={OWNER_TOKEN_HEADER: "short"})
    assert status == 400
    assert json.loads(raw)["error"] == "invalid owner token"


def test_http_revoke_owned_share_needs_the_owner_token(server_url,
                                                       sample_plan):
    _, _, raw = _post_json(server_url + "/api/share", sample_plan,
                           token=OWNER)
    share_id = json.loads(raw)["share_id"]
    url = server_url + f"/api/share/{share_id}"
    status, _, raw = _request(url, method="DELETE")
    assert status == 403
    assert "only the owner" in json.loads(raw)["error"]
    status, _, raw = _request(url, method="DELETE",
                              headers={OWNER_TOKEN_HEADER: OTHER})
    assert status == 403
    status, _, _ = _request(url)  # still alive after forbidden attempts
    assert status == 200
    status, _, raw = _request(url, method="DELETE",
                              headers={OWNER_TOKEN_HEADER: OWNER})
    assert status == 200
    assert json.loads(raw) == {"revoked": True, "share_id": share_id}
    status, _, _ = _request(url)
    assert status == 404


def test_http_revoke_legacy_share_still_open(server_url, sample_plan):
    _, _, raw = _post_json(server_url + "/api/share", sample_plan)
    created = json.loads(raw)
    assert created["owned"] is False
    status, _, raw = _request(
        server_url + f"/api/share/{created['share_id']}", method="DELETE")
    assert status == 200
    assert json.loads(raw)["revoked"] is True


def test_http_dashboard_verdict_recomputed_for_tampered_share(server_url,
                                                              sample_plan):
    tampered = copy.deepcopy(sample_plan)
    tampered["items"][0]["affinity"] = 0.01  # no longer matches evidence
    _, _, raw = _post_json(server_url + "/api/share", tampered, token=OWNER)
    assert json.loads(raw)["owned"] is True
    status, _, raw = _request(server_url + "/api/shares",
                              headers={OWNER_TOKEN_HEADER: OWNER})
    (entry,) = json.loads(raw)["shares"]
    assert entry["passed"] is False  # listing never launders a bad plan


# --- UI wiring ----------------------------------------------------------------------

def test_ui_page_carries_ownership_wiring(server_url):
    status, _, raw = _request(server_url + "/")
    assert status == 200
    page = raw.decode()
    assert 'id="sharesbtn"' in page
    assert 'id="sharesout"' in page
    assert "tastepilot_owner_token" in page  # localStorage credential
    assert "X-Owner-Token" in page
    assert "localStorage" in page
    assert "/api/shares" in page
