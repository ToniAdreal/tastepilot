"""Multi-timeline bundles as take-away documents (run 57):
a bundle of named branch timelines as ONE Markdown or plain-text
grid document — a section per timeline under a bundle banner —
instead of only the two bundle calendars (leg-level and paced)
that existed before. This is the bundle counterpart of the
single-timeline document exports.

Doctrine under test: every verdict is recomputed per column from
its own sealed record — a tampered branch deep in the bundle is
a FAIL column under a NOT VERIFIED bundle banner naming its
timeline and column, never clean, while clean timelines beside
it still PASS; the document formats need no dates (an undated
bundle renders its grids even though both calendars refuse it
with no_dated_legs); rendering is deterministic; subscription
feeds stay calendar-only (MULTI_FEED_FORMATS) even though the
bundle exporter now renders documents."""
import copy
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.export import (MULTI_FEED_FORMATS,
                               MULTI_TIMELINE_FORMATS,
                               export_multi_timeline,
                               export_multi_timeline_markdown,
                               export_multi_timeline_text,
                               render_multi_timeline_markdown,
                               render_multi_timeline_text)
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
UNDATED = "a slow weekend in Kyoto, I love Spirited Away"
PRIMARY = "Gion, Kyoto"
ALT = "Kyoto National Museum"


def _timeline(name, text=DATED):
    base = TastePilotAgent().run(text)
    return {"name": name, "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}


def _payload():
    return {"timelines": [_timeline("Japan-France"),
                          _timeline("Kyoto only", DATED_ONE)]}


def _tampered_payload():
    payload = _payload()
    branch = payload["timelines"][0]["branches"][0]["plan"]
    branch["items"][0]["affinity"] = 0.01
    return payload


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(url, obj):
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _cli(*argv):
    return subprocess.run(
        [sys.executable, "-m", "tastepilot", *argv],
        capture_output=True, text=True, cwd=APP_ROOT)


# -- the bundle document renderers --------------------------------------


def test_markdown_bundle_sections_banner_and_summaries():
    timelines = _payload()["timelines"]
    content, summaries = render_multi_timeline_markdown(timelines)
    assert content.startswith("# TastePilot multi-timeline bundle")
    assert "Verification: PASS" in content
    assert "NOT VERIFIED" not in content
    assert "## Timeline: Japan-France" in content
    assert "## Timeline: Kyoto only" in content
    # Each timeline keeps its own banner + columns line + leg tables.
    assert content.count("Columns:") == 2
    assert "### Leg: Kyoto" in content and "### Leg: Paris" in content
    assert "As planned (PASS" in content and "Museum (PASS" in content
    assert [s["name"] for s in summaries] == ["Japan-France", "Kyoto only"]
    assert all(s["all_passed"] for s in summaries)
    assert summaries[0]["columns"] == ["As planned", "Museum"]
    assert summaries[0]["legs"] == 2 and summaries[1]["legs"] == 1


def test_text_bundle_is_plain_and_complete():
    timelines = _payload()["timelines"]
    content, summaries = render_multi_timeline_text(timelines)
    assert content.startswith("TastePilot multi-timeline bundle")
    assert "Verification: PASS" in content
    assert "Timeline: Japan-France" in content
    assert "Leg: Kyoto" in content
    assert "|" not in content.split("Timelines:")[0]  # header is plain
    assert [s["name"] for s in summaries] == ["Japan-France", "Kyoto only"]
    assert all(s["all_passed"] for s in summaries)


def test_bundle_documents_are_deterministic():
    timelines = _payload()["timelines"]
    assert render_multi_timeline_markdown(timelines) == \
        render_multi_timeline_markdown(copy.deepcopy(timelines))
    assert render_multi_timeline_text(timelines) == \
        render_multi_timeline_text(copy.deepcopy(timelines))


def test_bundle_documents_tampered_branch_fails_loudly():
    timelines = _tampered_payload()["timelines"]
    for render in (render_multi_timeline_markdown,
                   render_multi_timeline_text):
        content, summaries = render(timelines)
        assert "Verification: NOT VERIFIED" in content
        assert "Japan-France / Museum" in content  # banner names it
        assert summaries[0]["all_passed"] is False
        assert summaries[1]["all_passed"] is True
        assert "Museum (FAIL" in content
    _content, all_passed, _s = export_multi_timeline(timelines, "markdown")
    assert all_passed is False


def test_bundle_documents_need_no_dates():
    base = TastePilotAgent().run(UNDATED)
    timelines = [{"name": "No dates", "base": base, "branches": [
        {"name": "Museum", "plan": swap_plan(base, PRIMARY, ALT)}]}]
    for fmt, wrapper in (("markdown", export_multi_timeline_markdown),
                         ("text", export_multi_timeline_text)):
        content, all_passed, summaries = wrapper(timelines)
        assert all_passed is True and "Verification: PASS" in content
        assert summaries[0]["legs"] >= 1
        via, via_passed, via_summaries = export_multi_timeline(
            timelines, fmt)
        assert (via, via_passed, via_summaries) == (
            content, all_passed, summaries)
    # The calendars still refuse the same bundle — no invented dates.
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_multi_timeline(timelines, "ics")
    with pytest.raises(ValueError, match="no_dated_legs"):
        export_multi_timeline(timelines, "ics-days")


def test_dispatcher_formats_and_feed_formats():
    assert MULTI_TIMELINE_FORMATS == ("markdown", "text", "ics", "ics-days")
    # Feeds stay calendar-only: documents are downloads, not feeds.
    assert MULTI_FEED_FORMATS == ("ics", "ics-days")
    timelines = _payload()["timelines"]
    for fmt in ("markdown", "text"):
        content, all_passed, summaries = export_multi_timeline(timelines, fmt)
        assert all_passed is True and content and summaries
    with pytest.raises(ValueError, match="unsupported"):
        export_multi_timeline(timelines, "html")


# -- the HTTP surface -----------------------------------------------------


def test_http_bundle_document_downloads():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, headers, body = _post(
            f"{base_url}/api/export-multi-timeline?format=markdown",
            _payload())
        assert status == 200
        assert "text/markdown" in headers.get("Content-Type", "")
        assert 'filename="tastepilot-multi-timeline.md"' in \
            headers.get("Content-Disposition", "")
        assert body.decode().startswith(
            "# TastePilot multi-timeline bundle")
        status, headers, body = _post(
            f"{base_url}/api/export-multi-timeline?format=text",
            _payload())
        assert status == 200
        assert "text/plain" in headers.get("Content-Type", "")
        assert 'filename="tastepilot-multi-timeline.txt"' in \
            headers.get("Content-Disposition", "")
        # A tampered bundle still downloads — as NOT VERIFIED.
        status, _h, body = _post(
            f"{base_url}/api/export-multi-timeline?format=markdown",
            _tampered_payload())
        assert status == 200
        assert "NOT VERIFIED" in body.decode()
        # An undated bundle downloads as a document too.
        base = TastePilotAgent().run(UNDATED)
        undated = {"timelines": [{"name": "No dates", "base": base,
                                  "branches": [{"name": "Museum", "plan":
                                                swap_plan(base, PRIMARY,
                                                          ALT)}]}]}
        status, _h, _b = _post(
            f"{base_url}/api/export-multi-timeline?format=text", undated)
        assert status == 200
    finally:
        server.shutdown()


def test_http_multi_feed_stays_calendar_only():
    server, base_url = _serve(make_handler(TastePilotAgent()))
    try:
        status, _h, body = _post(f"{base_url}/api/multi-feeds", _payload())
        assert status == 201
        feed_id = json.loads(body)["feed_id"]
        for fmt in ("markdown", "text"):
            req = urllib.request.Request(
                f"{base_url}/api/multi-feeds/{feed_id}.ics?format={fmt}")
            try:
                with urllib.request.urlopen(req) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
                assert b"unsupported multi-feed format" in exc.read()
            assert status == 400
    finally:
        server.shutdown()


# -- the CLI surface ------------------------------------------------------


def test_cli_bundle_export(tmp_path):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(_payload()), encoding="utf-8")
    out = tmp_path / "bundle.md"
    run = _cli("export", "--bundle", str(bundle_file),
               "--format", "markdown", "--out", str(out))
    assert run.returncode == 0, run.stderr
    assert "bundle=True" in run.stdout and "timelines=2" in run.stdout
    assert "verified=True" in run.stdout
    content = out.read_text(encoding="utf-8")
    assert content.startswith("# TastePilot multi-timeline bundle")
    # A bare list is accepted as the bundle file's shape too.
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(_payload()["timelines"]), encoding="utf-8")
    run = _cli("export", "--bundle", str(bare), "--format", "text")
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("TastePilot multi-timeline bundle")


def test_cli_bundle_export_failures(tmp_path):
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(_tampered_payload()), encoding="utf-8")
    out = tmp_path / "tampered.md"
    run = _cli("export", "--bundle", str(tampered), "--out", str(out))
    assert run.returncode == 1  # exports, but unverified
    assert "NOT VERIFIED" in out.read_text(encoding="utf-8")
    run = _cli("export", "--bundle", str(tmp_path / "missing.json"))
    assert run.returncode == 2 and "cannot read bundle" in run.stderr
    run = _cli("export")
    assert run.returncode == 2
    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    run = _cli("export", "--plan", str(plan_file),
               "--bundle", str(tampered))
    assert run.returncode == 2 and "mutually exclusive" in run.stderr
    run = _cli("export", "--help")
    assert run.returncode == 0 and "--bundle" in run.stdout


# -- docs / UI wiring -----------------------------------------------------


def test_ui_and_docs_wiring():
    html = INDEX.read_text(encoding="utf-8")
    assert "Download bundle (Markdown)" in html
    assert "Download bundle (Text)" in html
    assert "Download bundle (iCalendar)" in html
    readme = README.read_text(encoding="utf-8")
    assert "export_multi_timeline_markdown" in readme
    assert "--bundle" in readme
    submission = SUBMISSION.read_text(encoding="utf-8")
    assert "Markdown" in submission and "bundle" in submission
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "Bundle document probe" in deploy
