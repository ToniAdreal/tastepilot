"""Paced variant of the single-timeline calendar feed
(SUBMISSION "What's next" follow-on, built early in generic
form): GET /api/feeds/<id>.ics?format=ics-days — the feed
counterpart of the paced bundle variant (run 55), which closed
with "single-timeline feeds stay leg-level — a paced
single-feed variant is a separate follow-on".

Doctrine under test: the paced variant is the SAME stored
timeline rendered at day granularity — pacing recomputed per
plan via pacing.plan_itinerary, every column's verdict
recomputed from its own sealed record, so a tampered branch's
paced days read NOT VERIFIED, never PASS. Change notification
mirrors the multi-feed: the feed carries an ETag (feed_etag, a
hash of the stored payload), the paced variant answers under
its own etag (the feed etag with a -days suffix), and the two
variants never answer each other's conditional polls. An
in-place update changes both etags; re-posting the identical
timeline changes neither.
"""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.pacing import plan_itinerary
from tastepilot.server import TimelineFeedStore, feed_etag, make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
DATED_ONE = ("a slow weekend in Kyoto Nov 3-5, "
             "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _payload(text=DATED):
    base = TastePilotAgent().run(text)
    branches = [{"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]
    return {"base": base, "branches": branches}


def _columns(payload):
    return [("As planned", payload["base"])] + [
        (b["name"], b["plan"]) for b in payload["branches"]]


def _expected_paced_events(payload):
    total = 0
    for _name, plan in _columns(payload):
        itin = plan_itinerary(plan)
        total += sum(1 for leg in itin["legs"] for day in leg["days"]
                     if day["date"] is not None and day["items"])
    return total


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _post(url, obj, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    return _request(url, method="POST",
                    body=json.dumps(obj).encode(), headers=headers)


# -- the etag --------------------------------------------------------------


def test_feed_etag_is_a_stable_payload_hash():
    payload = _payload()
    etag = feed_etag(payload)
    assert len(etag) == 32
    # The same stored payload (a deep copy of it) hashes the
    # same; a different timeline hashes differently.
    assert feed_etag(copy.deepcopy(payload)) == etag
    assert feed_etag(_payload(DATED_ONE)) != etag
    store = TimelineFeedStore(clock=FakeClock())
    meta = store.create(payload)
    assert meta["etag"] == etag
    assert store.etag(meta["feed_id"]) == etag
    assert store.etag("missing") is None


def test_feed_etag_expiry_and_update_rules():
    clock = FakeClock()
    store = TimelineFeedStore(ttl_seconds=100, clock=clock)
    meta = store.create(_payload())
    feed_id = meta["feed_id"]
    # Re-posting the identical timeline keeps the etag: the
    # served calendar genuinely did not change.
    status, meta2 = store.update_status(
        feed_id, copy.deepcopy(store.get(feed_id)))
    assert status == "updated" and meta2["etag"] == meta["etag"]
    # Replacing the timeline changes it.
    status, meta3 = store.update_status(feed_id, _payload(DATED_ONE))
    assert status == "updated" and meta3["etag"] != meta["etag"]
    assert store.etag(feed_id) == meta3["etag"]
    # An expired feed has no etag, exactly like an unknown one.
    clock.advance(101)
    assert store.etag(feed_id) is None


# -- the HTTP surface --------------------------------------------------------


def test_paced_feed_fetch_and_variant_etags():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        payload = _payload()
        expected = _expected_paced_events(payload)
        assert expected > 4  # strictly more than the leg-level feed
        status, _h, body = _post(f"{base_url}/api/feeds", payload)
        assert status == 201
        created = json.loads(body)
        assert created["paced_url"].endswith(".ics?format=ics-days")
        assert created["etag"] == feed_etag(payload)
        leg_url = base_url + created["url"]
        paced_url = base_url + created["paced_url"]
        # Leg-level fetch: unchanged count + the feed's own etag.
        status, headers, body = _request(leg_url)
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 4
        assert headers.get("Cache-Control") == "public, max-age=300"
        leg_etag = headers.get("ETag", "").strip('"')
        assert leg_etag == created["etag"]
        # Paced fetch: day-granularity events under a -days etag.
        status, headers, body = _request(paced_url)
        assert status == 200
        text = body.decode().replace("\r\n ", "")
        assert text.count("BEGIN:VEVENT") == expected
        assert "branch timeline by day" in text
        assert "TastePilot [Museum]: Kyoto · 2026-11-03" in text
        assert "NOT VERIFIED" not in text
        paced_etag = headers.get("ETag", "").strip('"')
        assert paced_etag == leg_etag + "-days"
        # Each variant answers its own conditional poll with 304.
        status, _h, body = _request(
            paced_url, headers={"If-None-Match": f'"{paced_etag}"'})
        assert status == 304 and body == b""
        status, _h, body = _request(
            leg_url, headers={"If-None-Match": f'"{leg_etag}"'})
        assert status == 304 and body == b""
        # …and never the other variant's, in either direction.
        status, _h, body = _request(
            paced_url, headers={"If-None-Match": f'"{leg_etag}"'})
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == expected
        status, _h, body = _request(
            leg_url, headers={"If-None-Match": f'"{paced_etag}"'})
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 4
        # An unknown format on the feed URL is a categorised 400.
        status, _h, body = _request(f"{leg_url}?format=html")
        assert status == 400
        assert "unsupported feed format" in body.decode()
        # Meta names the paced URL and the etag too.
        status, _h, body = _request(
            f"{base_url}/api/feeds/{created['feed_id']}")
        meta = json.loads(body)
        assert meta["paced_url"] == created["paced_url"]
        assert meta["etag"] == created["etag"]
    finally:
        server.shutdown()


def test_paced_feed_update_changes_both_etags():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/feeds", _payload())
        created = json.loads(body)
        feed_id = created["feed_id"]
        leg_url = base_url + created["url"]
        paced_url = base_url + created["paced_url"]
        _s, headers, _b = _request(leg_url)
        old_leg_etag = headers.get("ETag", "").strip('"')
        _s, headers, _b = _request(paced_url)
        old_paced_etag = headers.get("ETag", "").strip('"')
        # Update in place with a one-leg timeline.
        newer = _payload(DATED_ONE)
        status, _h, body = _post(f"{base_url}/api/feeds/{feed_id}",
                                 newer)
        assert status == 200
        updated = json.loads(body)
        assert updated["etag"] != old_leg_etag
        assert updated["paced_url"] == created["paced_url"]
        # Both variants serve the new series under new etags;
        # neither old etag earns a 304 any more.
        status, headers, body = _request(
            leg_url, headers={"If-None-Match": f'"{old_leg_etag}"'})
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 2
        new_leg_etag = headers.get("ETag", "").strip('"')
        assert new_leg_etag == updated["etag"]
        status, headers, body = _request(
            paced_url, headers={"If-None-Match": f'"{old_paced_etag}"'})
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == \
            _expected_paced_events(newer)
        assert headers.get("ETag", "").strip('"') == \
            new_leg_etag + "-days"
    finally:
        server.shutdown()


def test_paced_feed_tampered_branch_days_not_verified():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        payload = _payload()
        payload["branches"][0]["plan"]["items"][0]["affinity"] = 0.01
        status, _h, body = _post(f"{base_url}/api/feeds", payload)
        assert status == 201  # tampering is a verdict, not a refusal
        created = json.loads(body)
        assert created["all_passed"] is False
        status, _h, body = _request(base_url + created["paced_url"])
        assert status == 200
        text = body.decode().replace("\r\n ", "")
        assert "NOT VERIFIED" in text
        assert "Branch: Museum (FAIL" in text
        assert "Branch: As planned (PASS" in text
    finally:
        server.shutdown()


def test_paced_feed_unknown_and_revoked_ids_404():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, _b = _request(
            f"{base_url}/api/feeds/nope.ics?format=ics-days")
        assert status == 404
        status, _h, body = _post(f"{base_url}/api/feeds", _payload())
        created = json.loads(body)
        feed_id = created["feed_id"]
        status, _h, _b = _request(
            f"{base_url}/api/feeds/{feed_id}", method="DELETE")
        assert status == 200
        status, _h, _b = _request(base_url + created["paced_url"])
        assert status == 404
        status, _h, _b = _request(base_url + created["url"])
        assert status == 404
    finally:
        server.shutdown()


def test_paced_feed_survives_restart_with_same_etag(tmp_path):
    feed_file = tmp_path / "feeds.json"
    payload = _payload()
    server, base_url = _serve(make_handler(
        TastePilotAgent(), feed_file=feed_file))
    try:
        status, _h, body = _post(f"{base_url}/api/feeds", payload)
        assert status == 201
        created = json.loads(body)
        paced_path, etag = created["paced_url"], created["etag"]
    finally:
        server.shutdown()
    server, base_url = _serve(make_handler(
        TastePilotAgent(), feed_file=feed_file))
    try:
        status, headers, body = _request(base_url + paced_path)
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == \
            _expected_paced_events(payload)
        # The etag is a payload hash, so it survives the restart
        # byte-identically and still earns its 304.
        assert headers.get("ETag", "").strip('"') == etag + "-days"
        status, _h, body = _request(
            base_url + paced_path,
            headers={"If-None-Match": f'"{etag}-days"'})
        assert status == 304 and body == b""
    finally:
        server.shutdown()


def test_paced_feed_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Calendar subscription (etag " in html
    assert "Paced by day: " in html and "d.paced_url" in html
    readme = README.read_text(encoding="utf-8")
    assert "paced variant of that subscription" in readme
    assert "feed_etag" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "single-timeline feed has a paced form too" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "Paced feed calendar probe" in deploy
