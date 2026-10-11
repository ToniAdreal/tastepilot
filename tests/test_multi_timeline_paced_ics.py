"""Multi-timeline bundles in paced iCalendar exports (run 55):
a bundle of named branch timelines as ONE calendar at day
granularity — one paced event series per column per timeline,
the bundle counterpart of the paced timeline calendar (run
54), which closed with "multi-timeline bundle ICS stays
leg-level — paced bundles are a separate follow-on".

Doctrine under test: pacing is recomputed per plan from that
plan's schedule + primary items via pacing.plan_itinerary — a
stored ``itinerary`` field is never read back; every column's
verdict is recomputed from its own sealed record — a tampered
branch's days read NOT VERIFIED, never PASS; a timeline with
no paced day contributes no events, and a bundle with no paced
day anywhere raises no_dated_legs instead of inventing dates;
rendering is deterministic with paced bundle UIDs namespaced
apart from the leg-level bundle UIDs, the paced timeline UIDs,
and the single-plan paced UIDs. The multi-feed serves the
paced variant at ?format=ics-days under its own etag, so the
two variants never answer each other's conditional polls.
"""
import copy
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.export import (MULTI_TIMELINE_FORMATS,
                               export_multi_timeline,
                               export_multi_timeline_ics,
                               export_multi_timeline_paced_ics,
                               render_multi_timeline_ics,
                               render_multi_timeline_paced_ics,
                               render_paced_ics,
                               render_timeline_paced_ics)
from tastepilot.pacing import plan_itinerary
from tastepilot.server import make_handler
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


def _timeline(name, text=DATED):
    base = TastePilotAgent().run(text)
    return {"name": name, "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _payload():
    return {"timelines": [_timeline("Japan-France"),
                          _timeline("Kyoto only", DATED_ONE)]}


def _unfold(content):
    return content.replace("\r\n ", "")


def _paced_days(plan):
    itin = plan_itinerary(plan)
    return [(leg["location"], day) for leg in itin["legs"]
            for day in leg["days"]
            if day["date"] is not None and day["items"]]


def _columns(timeline):
    return [("As planned", timeline["base"])] + [
        (b["name"], b["plan"]) for b in timeline["branches"]]


def _expected_series(timelines):
    """(timeline, column, location, day) in render order."""
    return [(t["name"], cname, location, day)
            for t in timelines for cname, plan in _columns(t)
            for location, day in _paced_days(plan)]


def _expected_events(timelines):
    return len(_expected_series(timelines))


# -- the bundle renderer ---------------------------------------------------


def test_paced_bundle_one_calendar_many_timelines():
    timelines = _payload()["timelines"]
    content, all_passed, summaries = export_multi_timeline_paced_ics(
        timelines)
    assert all_passed is True
    expected = _expected_events(timelines)
    assert expected > 6  # strictly more than the leg-level bundle
    assert content.count("BEGIN:VEVENT") == expected
    assert content.count("BEGIN:VCALENDAR") == 1
    assert "X-WR-CALNAME:TastePilot multi-timeline bundle by day" \
        in content
    assert [s["name"] for s in summaries] == ["Japan-France",
                                              "Kyoto only"]
    assert sum(s["events"] for s in summaries) == expected
    assert all(s["all_passed"] for s in summaries)
    flat = _unfold(content)
    assert ("TastePilot [Japan-France / Museum]: Kyoto · 2026-11-03"
            in flat)
    assert ("TastePilot [Kyoto only / As planned]: Kyoto · "
            "2026-11-03" in flat)
    assert "Timeline: Japan-France" in flat
    assert "NOT VERIFIED" not in flat
    # Byte-deterministic, UIDs unique across timelines + branches.
    again, _, _ = export_multi_timeline_paced_ics(
        copy.deepcopy(timelines))
    assert again == content
    uids = [l for l in content.split("\r\n") if l.startswith("UID:")]
    assert len(uids) == len(set(uids)) == expected


def test_paced_bundle_each_event_only_its_days_items():
    timelines = _payload()["timelines"]
    content = render_multi_timeline_paced_ics(timelines)[0]
    events = _unfold(content).split("BEGIN:VEVENT")[1:]
    series = _expected_series(timelines)
    assert len(events) == len(series)
    for (tname, cname, _location, day), block in zip(series, events):
        assert f"[{tname} / {cname}]" in block
        assert day["date"] in block
        for ref in day["items"]:
            # Names are ICS-escaped in the description (commas
            # become \,), so check the unescaped head or the
            # escaped full name.
            assert ref["name"].split(",")[0] in block or \
                ref["name"].replace(",", "\\,") in block
    # A name paced only on a later day of the first column is
    # absent from that column's first event.
    first = series[0]
    first_names = {i["name"] for i in first[3]["items"]}
    later = {i["name"] for s in series[1:]
             if s[0] == first[0] and s[1] == first[1]
             for i in s[3]["items"]}
    for name in later - first_names:
        assert name not in events[0]


def test_paced_bundle_tampered_timeline_not_verified():
    timelines = _payload()["timelines"]
    timelines[1]["branches"][0]["plan"]["items"][0]["affinity"] = 0.01
    content, all_passed, summaries = export_multi_timeline_paced_ics(
        timelines)
    assert all_passed is False
    assert summaries[0]["all_passed"] is True
    assert summaries[1]["all_passed"] is False
    flat = _unfold(content)
    assert "Timeline: Kyoto only" in flat
    assert "Branch: Museum (FAIL" in flat
    assert "NOT VERIFIED" in flat
    assert "Branch: As planned (PASS" in flat


def test_paced_bundle_dateless_and_partial():
    plain = TastePilotAgent().run(
        "a slow weekend in Kyoto, I love Spirited Away")
    dateless = [{"name": "No dates", "base": plain, "branches": [
        {"name": "Museum", "plan": swap_plan(plain, PRIMARY, ALT)}]}]
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_multi_timeline_paced_ics(dateless)
    with pytest.raises(ValueError, match="no_dated_legs"):
        render_multi_timeline_paced_ics(dateless)
    # A dateless timeline inside a dated bundle contributes no
    # events; the dated timeline still renders in full.
    mixed = dateless + _payload()["timelines"][:1]
    content, all_passed, summaries = export_multi_timeline_paced_ics(
        mixed)
    assert all_passed is True
    assert summaries[0]["events"] == 0
    assert content.count("BEGIN:VEVENT") == _expected_events(mixed)
    assert "No dates" not in _unfold(content)


def test_paced_bundle_uids_apart_from_every_other_calendar():
    timelines = _payload()["timelines"]
    content, _ = render_multi_timeline_paced_ics(timelines)
    first = render_multi_timeline_paced_ics(
        copy.deepcopy(timelines))[0]
    assert first == content  # deterministic across regeneration
    uids = set(re.findall(
        r"UID:([0-9a-f]+)@tastepilot-multi-timeline", content))
    assert len(uids) == _expected_events(timelines)
    leg_uids = set(re.findall(
        r"UID:([0-9a-f]+)@tastepilot-multi-timeline",
        render_multi_timeline_ics(timelines)[0]))
    assert not (uids & leg_uids)
    base, branches = timelines[0]["base"], timelines[0]["branches"]
    timeline_uids = set(re.findall(
        r"UID:([0-9a-f]+)@tastepilot-timeline",
        render_timeline_paced_ics(base, branches)))
    assert not (uids & timeline_uids)
    single_uids = set(re.findall(r"UID:([0-9a-f]+)@tastepilot",
                                 render_paced_ics(base)))
    assert not (uids & single_uids)


def test_paced_bundle_rfc_shape_and_dispatcher():
    timelines = _payload()["timelines"]
    content, all_passed, summaries = export_multi_timeline(
        timelines, "ics-days")
    assert content.endswith("\r\n")
    for line in content.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert "ics-days" in MULTI_TIMELINE_FORMATS
    # The dispatcher agrees with both direct entry points.
    direct, d_passed, d_summaries = export_multi_timeline_paced_ics(
        timelines)
    assert (content, all_passed, summaries) == (
        direct, d_passed, d_summaries)
    leg, leg_passed, leg_summaries = export_multi_timeline(
        timelines, "ics")
    direct_leg, dl_passed, dl_summaries = export_multi_timeline_ics(
        timelines)
    assert (leg, leg_passed, leg_summaries) == (
        direct_leg, dl_passed, dl_summaries)
    assert leg.count("BEGIN:VEVENT") == 6
    with pytest.raises(ValueError, match="unsupported"):
        export_multi_timeline(timelines, "markdown")


def test_stored_itinerary_field_never_read_back():
    timelines = _payload()["timelines"]
    timelines[0]["base"]["itinerary"] = {"legs": [
        {"location": "Kyoto", "days": [
            {"date": "1999-01-01", "items": [
                {"name": "evil.example item", "domain": "place",
                 "location": "Kyoto", "affinity": 1.0}]}]}]}
    content = render_multi_timeline_paced_ics(timelines)[0]
    assert "evil.example" not in content
    assert "19990101" not in content


# -- the HTTP surfaces -------------------------------------------------------


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


def test_paced_bundle_export_endpoint_over_http():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        payload = _payload()
        expected = _expected_events(payload["timelines"])
        status, headers, body = _post(
            f"{base_url}/api/export-multi-timeline?format=ics-days",
            payload)
        assert status == 200
        assert "tastepilot-multi-timeline.ics" in \
            headers.get("Content-Disposition", "")
        assert "text/calendar" in headers.get("Content-Type", "")
        assert headers.get("Cache-Control") == "no-store"
        assert body.decode().count("BEGIN:VEVENT") == expected
        # The leg-level format over the same endpoint is unchanged.
        status, _h, body = _post(
            f"{base_url}/api/export-multi-timeline?format=ics",
            payload)
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 6
        # Tampered branch: still 200, its days NOT VERIFIED.
        payload["timelines"][0]["branches"][0]["plan"]["items"][0][
            "affinity"] = 0.01
        status, _h, body = _post(
            f"{base_url}/api/export-multi-timeline?format=ics-days",
            payload)
        assert status == 200
        assert "NOT VERIFIED" in body.decode().replace("\r\n ", "")
        # Dateless bundle -> categorised 400.
        plain = TastePilotAgent().run(
            "a slow weekend in Kyoto, I love Spirited Away")
        dateless = {"timelines": [{"name": "No dates", "base": plain,
                    "branches": [{"name": "Museum", "plan": swap_plan(
                        plain, PRIMARY, ALT)}]}]}
        status, _h, body = _post(
            f"{base_url}/api/export-multi-timeline?format=ics-days",
            dateless)
        assert status == 400 and "no_dated_legs" in body.decode()
        # Unknown format and bundle-shape problems -> 400s.
        status, _h, _b = _post(
            f"{base_url}/api/export-multi-timeline?format=html",
            _payload())
        assert status == 400
        status, _h, body = _post(
            f"{base_url}/api/export-multi-timeline?format=ics-days",
            {"timelines": [{"name": "A", "base": {}, "branches": []},
                            {"name": "A", "base": {},
                             "branches": []}]})
        assert status == 400
        assert "duplicate_timeline_name" in body.decode()
    finally:
        server.shutdown()


def test_paced_multi_feed_fetch_and_variant_etags():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        payload = _payload()
        expected = _expected_events(payload["timelines"])
        status, _h, body = _post(f"{base_url}/api/multi-feeds",
                                 payload)
        assert status == 201
        created = json.loads(body)
        assert created["paced_url"].endswith(
            ".ics?format=ics-days")
        leg_url = base_url + created["url"]
        paced_url = base_url + created["paced_url"]
        # Leg-level fetch: unchanged count + the feed's own etag.
        status, headers, body = _request(leg_url)
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == 6
        leg_etag = headers.get("ETag", "").strip('"')
        assert leg_etag == created["etag"]
        # Paced fetch: day-granularity events under a -days etag.
        status, headers, body = _request(paced_url)
        assert status == 200
        text = body.decode().replace("\r\n ", "")
        assert text.count("BEGIN:VEVENT") == expected
        assert "bundle by day" in text
        paced_etag = headers.get("ETag", "").strip('"')
        assert paced_etag == leg_etag + "-days"
        # Each variant answers its own conditional poll with 304.
        status, _h, body = _request(
            paced_url, headers={"If-None-Match": f'"{paced_etag}"'})
        assert status == 304 and body == b""
        status, _h, body = _request(
            leg_url, headers={"If-None-Match": f'"{leg_etag}"'})
        assert status == 304 and body == b""
        # …and never the other variant's: the leg etag presented
        # to the paced URL is a full 200 download, not a 304.
        status, _h, body = _request(
            paced_url, headers={"If-None-Match": f'"{leg_etag}"'})
        assert status == 200
        assert body.decode().count("BEGIN:VEVENT") == expected
        # An unknown format on the feed URL is a categorised 400.
        status, _h, body = _request(
            f"{leg_url}?format=html")
        assert status == 400 and "unsupported" in body.decode()
        # Meta names the paced URL too.
        status, _h, body = _request(
            f"{base_url}/api/multi-feeds/{created['feed_id']}")
        assert json.loads(body)["paced_url"] == created["paced_url"]
    finally:
        server.shutdown()


def test_paced_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Download bundle (iCalendar by day)" in html
    assert "/api/export-multi-timeline" in html
    assert "paced_url" in html
    readme = README.read_text(encoding="utf-8")
    assert "bundle by day" in readme
    assert "export-multi-timeline" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "paced bundle" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "Paced bundle calendar probe" in deploy
