"""Cross-device owner-token portability (run 23).

Owner tokens were per-browser: a dashboard lived in one browser's
localStorage and there was no supported way for a second browser to
present the same token, or for either side to confirm they matched.
Now the token is portable by design: every owned surface names the
token by a *fingerprint* (the first 12 hex chars of its stored hash)
— enough for two browsers to confirm they hold the same token,
useless as a credential — and the UI can adopt a token pasted from
another browser after validating it against the server, so the same
dashboard (list + revoke) follows the user across browsers with no
account system, no registration, and no password anywhere.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.server import (OWNER_TOKEN_HEADER, ShareStore,
                               make_handler, owner_fingerprint,
                               owner_token_hash)

APP_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = APP_ROOT / "samples" / "plan_weekend_kyoto.json"

OWNER = "owner-token-alpha-0123456789"
OTHER = "owner-token-bravo-0123456789"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post_share(url, obj, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[OWNER_TOKEN_HEADER] = token
    return _request(url + "/api/share", method="POST",
                    body=json.dumps(obj).encode(), headers=headers)


def _shares(url, token=None):
    headers = {}
    if token is not None:
        headers[OWNER_TOKEN_HEADER] = token
    status, _, body = _request(url + "/api/shares", headers=headers)
    return status, json.loads(body)


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                  make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def sample_plan():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# -- fingerprint helper ----------------------------------------------------

def test_fingerprint_is_hash_prefix_and_never_the_token():
    fp = owner_fingerprint(OWNER)
    assert fp == owner_token_hash(OWNER)[:12]
    assert len(fp) == 12
    assert fp != OWNER and OWNER not in fp


def test_fingerprint_distinguishes_tokens():
    assert owner_fingerprint(OWNER) != owner_fingerprint(OTHER)


@pytest.mark.parametrize("bad", [None, 42, "short", "has space in it",
                                 "x" * 201])
def test_fingerprint_none_for_invalid_tokens(bad):
    assert owner_fingerprint(bad) is None


# -- store metadata ---------------------------------------------------------

def test_store_meta_carries_fingerprint_only_for_owned():
    store = ShareStore()
    owned = store.put({"items": []}, owner_token=OWNER)
    legacy = store.put({"items": [{"name": "x"}]})
    assert owned["owner_fingerprint"] == owner_fingerprint(OWNER)
    assert legacy["owned"] is False
    assert legacy["owner_fingerprint"] is None
    # The full hash is a stored credential equivalent for matching;
    # it must never ride along in metadata either.
    assert owner_token_hash(OWNER) not in json.dumps(owned)


def test_fingerprint_survives_persistence(tmp_path):
    path = tmp_path / "shares.json"
    store = ShareStore(path=path)
    meta = store.put({"items": []}, owner_token=OWNER)
    reloaded = ShareStore(path=path)
    listed = reloaded.list_for_owner(OWNER)
    assert listed[0]["owner_fingerprint"] == meta["owner_fingerprint"]


# -- HTTP: same token, two browsers -----------------------------------------

def test_share_create_response_names_fingerprint(server_url, sample_plan):
    status, _, body = _post_share(server_url, sample_plan, token=OWNER)
    assert status == 201
    data = json.loads(body)
    assert data["owned"] is True
    assert data["owner_fingerprint"] == owner_fingerprint(OWNER)


def test_legacy_share_create_has_no_fingerprint(server_url, sample_plan):
    status, _, body = _post_share(server_url, sample_plan)
    assert status == 201
    data = json.loads(body)
    assert data["owned"] is False
    assert data["owner_fingerprint"] is None


def test_second_browser_same_token_same_dashboard(server_url,
                                                   sample_plan):
    _post_share(server_url, sample_plan, token=OWNER)
    # "Browser 1" and "browser 2" are the same server/token pair —
    # the second browser adopted the token and sees the same list.
    status1, dash1 = _shares(server_url, OWNER)
    status2, dash2 = _shares(server_url, OWNER)
    assert status1 == status2 == 200
    assert dash1 == dash2
    assert dash1["count"] == 1
    assert dash1["owner_fingerprint"] == owner_fingerprint(OWNER)
    assert dash1["shares"][0]["owner_fingerprint"] == \
        owner_fingerprint(OWNER)


def test_other_token_gets_its_own_empty_dashboard(server_url,
                                                  sample_plan):
    _post_share(server_url, sample_plan, token=OWNER)
    status, dash = _shares(server_url, OTHER)
    assert status == 200
    assert dash["count"] == 0
    assert dash["owner_fingerprint"] == owner_fingerprint(OTHER)
    assert dash["owner_fingerprint"] != owner_fingerprint(OWNER)


def test_adopted_token_can_revoke_from_second_browser(server_url,
                                                      sample_plan):
    _, _, body = _post_share(server_url, sample_plan, token=OWNER)
    share_id = json.loads(body)["share_id"]
    # Second browser: same token, DELETE succeeds.
    status, _, body = _request(
        f"{server_url}/api/share/{share_id}", method="DELETE",
        headers={OWNER_TOKEN_HEADER: OWNER})
    assert status == 200
    assert json.loads(body)["revoked"] is True
    status, _, _ = _request(f"{server_url}/api/share/{share_id}")
    assert status == 404


def test_token_never_echoed_by_any_owned_surface(server_url,
                                                 sample_plan):
    _, _, body = _post_share(server_url, sample_plan, token=OWNER)
    surfaces = [body]
    _, dash = _shares(server_url, OWNER)
    surfaces.append(json.dumps(dash).encode())
    for blob in surfaces:
        assert OWNER.encode() not in blob
        assert owner_token_hash(OWNER).encode() not in blob


# -- UI wiring ---------------------------------------------------------------

def test_ui_portability_wiring():
    page = (APP_ROOT / "tastepilot" / "static" / "index.html") \
        .read_text(encoding="utf-8")
    assert 'id="ownerfp"' in page
    assert 'id="copytokenbtn"' in page
    assert 'id="tokenin"' in page
    assert 'id="usetokenbtn"' in page
    # Adoption validates before replacing, then stores the candidate.
    assert "localStorage.setItem('tastepilot_owner_token', candidate)" \
        in page
    assert "refreshOwnerFingerprint" in page
    assert "owner_fingerprint" in page


def test_docs_describe_portability():
    readme = (APP_ROOT / "README.md").read_text(encoding="utf-8")
    submission = (APP_ROOT / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "fingerprint" in readme
    assert "fingerprint" in submission
