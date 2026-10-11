"""Subscriber overview: one summary over everything a
token owns.

Doctrine under test: a subscriber's artefacts were visible
only kind by kind — a shares dashboard, a feed directory,
a webhook directory, a profile panel — and nothing answered
"what do I own, in total, and is it all still verifying?"
``GET /api/subscriber/overview`` aggregates the same rows
those directories return (share and feed verdicts recomputed
at list time, webhook tallies from the feeds' own logs), so
the overview can never disagree with the directory it
summarises. It is read-only and leak-free by construction:
artefacts owned by another token, or by no token, never
appear, and no row or tally ever carries a stored payload,
a webhook URL, a signing secret, the token, or its full
hash.
"""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tastepilot.agent import TastePilotAgent
from tastepilot.server import (OWNER_TOKEN_HEADER, make_handler,
                               owner_token_hash)
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

# Built by concatenation, as in the other subscriber tests:
# a single credential-shaped literal was once rewritten at
# write time (see run 42's incident note).
TOKEN = "overview-token-" + "alpha-0123456789abcd"
OTHER = "overview-token-" + "bravo-0123456789abcd"

DATED = ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
         "I love Spirited Away and Kind of Blue")
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request(url, method="GET", body=None, token=None,
             raw_token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[OWNER_TOKEN_HEADER] = token
    if raw_token is not None:
        headers[OWNER_TOKEN_HEADER] = raw_token
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(url, obj=None, token=None):
    return _request(url, method="POST",
                    body=json.dumps(obj).encode()
                    if obj is not None else b"", token=token)


def _receiver():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length")
                                or 0))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    return _serve(Handler)


def _timeline():
    base = TastePilotAgent().run(DATED)
    return {"base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _overview(base_url, token=TOKEN):
    status, headers, raw = _request(
        f"{base_url}/api/subscriber/overview", token=token)
    assert status == 200
    return headers, json.loads(raw), raw.decode()


def test_http_overview_flow_and_gates():
    receiver, receiver_url = _receiver()
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        # Gates: no token -> 401, malformed token -> 400.
        status, _, _ = _request(f"{base_url}/api/subscriber/overview")
        assert status == 401
        status, _, _ = _request(
            f"{base_url}/api/subscriber/overview", raw_token="short")
        assert status == 400
        # A token with nothing yet: zero counts, no error.
        _, data, _ = _overview(base_url)
        assert data["counts"]["artefacts"] == 0
        assert data["profile"]["exists"] is False
        # One owned share, one owned single feed, one owned
        # multi-feed with a webhook, one unowned share.
        plan = TastePilotAgent().run(DATED)
        status, _, _ = _post(f"{base_url}/api/share", plan,
                             token=TOKEN)
        assert status == 201
        status, _, _ = _post(f"{base_url}/api/share",
                             TastePilotAgent().run(
                                 "a first date in NYC, "
                                 "I love Kind of Blue"))
        assert status == 201
        timeline = _timeline()
        status, _, _ = _post(f"{base_url}/api/feeds", timeline,
                             token=TOKEN)
        assert status == 201
        status, _, raw = _post(
            f"{base_url}/api/multi-feeds",
            {"timelines": [{"name": "Japan-France",
                            "base": timeline["base"],
                            "branches": timeline["branches"]}]},
            token=TOKEN)
        assert status == 201
        multi_id = json.loads(raw)["feed_id"]
        status, _, _ = _post(
            f"{base_url}/api/multi-feeds/{multi_id}/webhooks",
            {"url": f"{receiver_url}/hook"}, token=TOKEN)
        assert status == 201
        # A persistent profile saved from a sealed swap.
        swapped = swap_plan(plan, PRIMARY, ALT)
        status, _, _ = _post(f"{base_url}/api/profile",
                             {"history": [swapped]}, token=TOKEN)
        assert status == 200

        headers, data, blob = _overview(base_url)
        assert headers.get("Cache-Control") == "no-store"
        assert data["owner_fingerprint"] == \
            owner_token_hash(TOKEN)[:12]
        counts = data["counts"]
        assert counts == {"shares": 1, "feeds": 1, "multi_feeds": 1,
                          "calendar_feeds": 2, "webhooks": 1,
                          "webhook_feeds": 1, "artefacts": 4}
        # Every owned artefact recomputes as passing.
        assert data["verification"]["shares"] == \
            {"passed": 1, "failed": 0, "unknown": 0}
        assert data["verification"]["feeds"] == \
            {"passed": 2, "failed": 0, "unknown": 0}
        assert data["verification"]["passed"] == 3
        assert data["verification"]["failed"] == 0
        # The registration ping is the one delivery so far.
        assert data["deliveries"] == {"deliveries": 1,
                                      "delivered": 1, "failed": 0,
                                      "skipped": 0}
        assert data["webhooks_summary"] == {"signed": 1,
                                            "paused": 0}
        profile = data["profile"]
        assert profile["exists"] is True
        assert profile["kept_swaps"] == 1
        assert profile["tag_weight_count"] > 0
        # Rows agree with the per-kind directories.
        assert [r["share_id"] for r in data["shares"]] == \
            [json.loads(_request(
                f"{base_url}/api/shares", token=TOKEN)[2])
             ["shares"][0]["share_id"]]
        assert len(data["feeds"]) == 1
        assert len(data["multi_feeds"]) == 1
        assert len(data["webhooks"]) == 1
        # Leak-free: no token, full hash, webhook URL, or
        # signing secret anywhere in the overview.
        assert TOKEN not in blob
        assert owner_token_hash(TOKEN) not in blob
        assert receiver_url not in blob
        assert "signing_secret" not in blob
        # Another token's overview is empty.
        _, other, _ = _overview(base_url, token=OTHER)
        assert other["counts"]["artefacts"] == 0
        assert other["profile"]["exists"] is False
    finally:
        server.shutdown()
        receiver.shutdown()


def test_http_overview_tampered_artefacts_count_as_failed():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        plan = TastePilotAgent().run(DATED)
        tampered = copy.deepcopy(plan)
        tampered["items"][0]["affinity"] = 0.01
        status, _, _ = _post(f"{base_url}/api/share", tampered,
                             token=TOKEN)
        assert status == 201
        timeline = _timeline()
        branch = copy.deepcopy(timeline["branches"][0]["plan"])
        branch["items"][0]["affinity"] = 0.01
        status, _, _ = _post(
            f"{base_url}/api/feeds",
            {"base": timeline["base"],
             "branches": [{"name": "Museum", "plan": branch}]},
            token=TOKEN)
        assert status == 201
        _, data, _ = _overview(base_url)
        assert data["verification"]["shares"] == \
            {"passed": 0, "failed": 1, "unknown": 0}
        assert data["verification"]["feeds"] == \
            {"passed": 0, "failed": 1, "unknown": 0}
        assert data["verification"]["failed"] == 2
        assert data["verification"]["passed"] == 0
    finally:
        server.shutdown()


def test_ui_and_docs_describe_subscriber_overview():
    page = INDEX.read_text()
    assert "/api/subscriber/overview" in page
    assert "overviewbtn" in page
    assert "Your subscriber overview" in page
    for doc in (README, SUBMISSION, DEPLOY):
        text = doc.read_text()
        assert "/api/subscriber/overview" in text
        assert "subscriber overview" in text.lower()
