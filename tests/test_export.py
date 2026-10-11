"""Take-away itinerary exports (export.py + CLI export + POST /api/export).

Doctrine under test: an export's verification banner is always
recomputed from the sealed record — a tampered plan exports with a
NOT VERIFIED banner (and exits non-zero via the CLI), never a clean
one; rendering is deterministic; iCalendar carries one all-day
event per dated leg and refuses a dateless plan categorically.
"""
import copy
import json
import re
import subprocess
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.export import (export_plan, media_for, recomputed_verdict,
                               render_ics, render_markdown, render_text)

APP = Path(__file__).resolve().parent.parent
SAMPLE = APP / "samples" / "plan_weekend_kyoto.json"


def _sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _dated_plan():
    agent = TastePilotAgent()
    return agent.run(
        "a trip: Kyoto Nov 3-5, then Paris Nov 6-8, I love Spirited Away",
        reference_date=date(2026, 10, 10))


def _tampered(plan):
    plan = copy.deepcopy(plan)
    plan["items"][0]["affinity"] += 0.05
    return plan


def test_markdown_export_pass_banner_and_items():
    plan = _sample()
    content, verified = export_plan(plan, "markdown")
    assert verified is True
    assert content.startswith("# TastePilot itinerary")
    assert "Verification: PASS" in content
    assert "evidence chain valid" in content
    assert "Record's own claimed verdict: PASS" in content
    for item in plan["items"]:
        assert item["entity"]["name"] in content
        assert item["evidence_ref"] in content
    assert "## Provenance" in content
    assert "python3 -m tastepilot verify" in content


def test_text_export_has_no_markdown_and_passes():
    content, verified = export_plan(_sample(), "text")
    assert verified is True
    assert "Verification: PASS" in content
    assert "**" not in content and "`" not in content
    assert "Kyoto" in content


def test_tampered_plan_exports_not_verified():
    plan = _tampered(_sample())
    assert recomputed_verdict(plan)["passed"] is False
    for fmt in ("markdown", "text"):
        content, verified = export_plan(plan, fmt)
        assert verified is False
        assert "NOT VERIFIED" in content
        assert "Verification: PASS" not in content
        assert "affinity_mismatch" in content


def test_plan_without_evidence_exports_not_verified():
    content, verified = export_plan({"items": [], "request": "x"},
                                    "markdown")
    assert verified is False
    assert "NOT VERIFIED" in content


def test_exports_are_deterministic():
    plan = _dated_plan()
    for fmt in ("markdown", "text", "ics"):
        first, _ = export_plan(plan, fmt)
        second, _ = export_plan(copy.deepcopy(plan), fmt)
        assert first == second


def test_ics_one_event_per_dated_leg():
    plan = _dated_plan()
    content, verified = export_plan(plan, "ics")
    assert verified is True
    assert content.startswith("BEGIN:VCALENDAR")
    assert content.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:TastePilot: Kyoto" in content
    assert "SUMMARY:TastePilot: Paris" in content
    # All-day events, DTEND exclusive: Nov 3-5 leg ends on the 6th.
    assert "DTSTART;VALUE=DATE:20261103" in content
    assert "DTEND;VALUE=DATE:20261106" in content
    assert "Verification: PASS" in content.replace("\\,", ",")
    # RFC 5545: CRLF line endings, no line over 75 octets.
    assert "\r\n" in content
    for line in content.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_ics_uids_stable_and_unique_per_leg():
    content = render_ics(_dated_plan())
    uids = re.findall(r"UID:([0-9a-f]+)@tastepilot", content)
    assert len(uids) == 2 and len(set(uids)) == 2
    assert render_ics(_dated_plan()) == content


def test_ics_dateless_plan_is_categorised_error():
    with pytest.raises(ValueError, match="no_dated_legs"):
        render_ics(_sample())


def test_ics_tampered_plan_carries_not_verified():
    plan = _tampered(_dated_plan())
    content, verified = export_plan(plan, "ics")
    assert verified is False
    assert "NOT VERIFIED" in content


def test_unknown_format_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        export_plan(_sample(), "pdf")
    with pytest.raises(ValueError, match="unsupported"):
        media_for("pdf")
    assert media_for("ics") == ("text/calendar; charset=utf-8", "ics")


def test_markdown_multi_leg_sections_and_schedule():
    plan = _dated_plan()
    content, _ = export_plan(plan, "markdown")
    assert "## Kyoto (2026-11-03 to 2026-11-05, 2 nights)" in content
    assert "## Paris (2026-11-06 to 2026-11-08, 2 nights)" in content
    assert "Schedule and transfers" in content


def _cli(*argv):
    return subprocess.run([sys.executable, "-m", "tastepilot", *argv],
                          cwd=APP, capture_output=True, text=True,
                          timeout=60)


def test_cli_export_markdown_and_ics(tmp_path):
    dated = tmp_path / "dated.json"
    dated.write_text(json.dumps(_dated_plan()), encoding="utf-8")
    out_md = tmp_path / "plan.md"
    run = _cli("export", "--plan", str(dated), "--out", str(out_md))
    assert run.returncode == 0, run.stderr
    assert "verified=True" in run.stdout
    assert "Verification: PASS" in out_md.read_text(encoding="utf-8")
    run = _cli("export", "--plan", str(dated), "--format", "ics")
    assert run.returncode == 0, run.stderr
    assert run.stdout.count("BEGIN:VEVENT") == 2


def test_cli_export_tampered_exits_1_but_writes(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_tampered(_sample())), encoding="utf-8")
    out = tmp_path / "bad.md"
    run = _cli("export", "--plan", str(bad), "--out", str(out))
    assert run.returncode == 1
    assert "NOT VERIFIED" in out.read_text(encoding="utf-8")
    assert "Traceback" not in run.stderr


def test_cli_export_errors_exit_2_without_traceback(tmp_path):
    run = _cli("export", "--plan", str(SAMPLE), "--format", "ics")
    assert run.returncode == 2 and "no_dated_legs" in run.stderr
    run = _cli("export", "--plan", str(tmp_path / "missing.json"))
    assert run.returncode == 2 and "Traceback" not in run.stderr
    junk = tmp_path / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    run = _cli("export", "--plan", str(junk))
    assert run.returncode == 2 and "Traceback" not in run.stderr
    run = _cli("export", "--help")
    assert run.returncode == 0 and "--format" in run.stdout


# --- server endpoint -------------------------------------------------

from tastepilot.server import make_handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


@pytest.fixture()
def server_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                make_handler(TastePilotAgent()))
    import threading
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _post(url, payload, fmt=None):
    req = urllib.request.Request(
        url + (f"?format={fmt}" if fmt else ""),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_server_export_markdown_download(server_url):
    status, headers, body = _post(server_url + "/api/export", _sample())
    assert status == 200
    assert headers["Content-Type"].startswith("text/markdown")
    assert "tastepilot-itinerary.md" in headers["Content-Disposition"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Verification: PASS" in body


def test_server_export_ics_and_errors(server_url):
    status, headers, body = _post(server_url + "/api/export",
                                  _dated_plan(), fmt="ics")
    assert status == 200
    assert headers["Content-Type"].startswith("text/calendar")
    assert body.count("BEGIN:VEVENT") == 2
    status, _, body = _post(server_url + "/api/export", _sample(),
                            fmt="ics")
    assert status == 400 and "no_dated_legs" in body
    status, _, body = _post(server_url + "/api/export", _sample(),
                            fmt="pdf")
    assert status == 400 and "unsupported" in body
    status, _, body = _post(server_url + "/api/export",
                            {"items": "nope"})
    assert status == 400


def test_server_export_tampered_not_verified(server_url):
    status, _, body = _post(server_url + "/api/export",
                            _tampered(_sample()))
    assert status == 200
    assert "NOT VERIFIED" in body and "Verification: PASS" not in body


def test_ui_carries_export_wiring():
    html = (APP / "tastepilot" / "static" / "index.html").read_text(
        encoding="utf-8")
    assert 'id="exportbtn"' in html and 'id="exportfmt"' in html
    assert 'id="vexportbtn"' in html and "/api/export?format=" in html
    assert "tastepilot-itinerary." in html
