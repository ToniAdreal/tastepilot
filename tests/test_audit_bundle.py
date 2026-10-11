"""Audit bundles (bundle.py + CLI bundle/verify-bundle + POST /api/bundle).

Doctrine under test: a bundle's verification report is recomputed at
packing time — a tampered plan packs as NOT VERIFIED, never clean —
and an archive is re-audited offline by manifest hashes plus a fresh
recomputation, never by trusting the report packed inside it.
Packing is deterministic: the same plan packs byte-identically.
"""
import copy
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.bundle import (BUNDLE_FORMAT, build_bundle,
                               bundle_verification, verify_bundle)

APP = Path(__file__).resolve().parent.parent
SAMPLE = APP / "samples" / "plan_weekend_kyoto.json"


def _sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _dated_plan():
    return TastePilotAgent().run(
        "a trip: Kyoto Nov 3-5, then Paris Nov 6-8, I love Spirited Away",
        reference_date=date(2026, 10, 10))


def _tampered(plan):
    plan = copy.deepcopy(plan)
    plan["items"][0]["affinity"] += 0.05
    return plan


def _read(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def _names(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.namelist()


def test_bundle_contents_and_manifest():
    plan = _dated_plan()
    data, verified = build_bundle(plan)
    assert verified is True
    assert _names(data) == ["plan.json", "evidence.json",
                            "verification.json", "itinerary.md",
                            "itinerary.txt", "itinerary.ics",
                            "manifest.json"]
    packed_plan = json.loads(_read(data, "plan.json"))
    assert packed_plan == plan
    assert json.loads(_read(data, "evidence.json")) == plan["evidence"]
    report = json.loads(_read(data, "verification.json"))
    assert report["passed"] is True and report["chain_valid"] is True
    assert report["item_count"] == len(plan["items"])
    assert report["alternative_count"] == len(plan["alternatives"])
    assert report["sealed_responses"] > 0
    assert all(r["admissible"] for r in report["items"])
    assert "Verification: PASS" in _read(data, "itinerary.md").decode()
    assert _read(data, "itinerary.ics").count(b"BEGIN:VEVENT") == 2
    manifest = json.loads(_read(data, "manifest.json"))
    assert manifest["format"] == BUNDLE_FORMAT
    assert manifest["verified"] is True and manifest["has_calendar"]
    for entry in manifest["files"]:
        blob = _read(data, entry["name"])
        assert entry["bytes"] == len(blob)
        assert entry["sha256"] == hashlib.sha256(blob).hexdigest()


def test_bundle_dateless_plan_omits_calendar():
    data, verified = build_bundle(_sample())
    assert verified is True
    assert "itinerary.ics" not in _names(data)
    manifest = json.loads(_read(data, "manifest.json"))
    assert manifest["has_calendar"] is False


def test_bundle_deterministic_and_no_wall_clock():
    plan = _dated_plan()
    first, _ = build_bundle(plan)
    second, _ = build_bundle(plan)
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert {info.date_time for info in archive.infolist()} == \
            {(1980, 1, 1, 0, 0, 0)}


def test_bundle_tampered_plan_packs_not_verified():
    data, verified = build_bundle(_tampered(_sample()))
    assert verified is False
    report = json.loads(_read(data, "verification.json"))
    assert report["passed"] is False
    assert any(v["reason"] == "affinity_mismatch"
               for v in report["violations"])
    assert "NOT VERIFIED" in _read(data, "itinerary.md").decode()
    outcome = verify_bundle(data)
    assert outcome["manifest_valid"] is True
    assert outcome["passed"] is False and outcome["ok"] is False


def test_bundle_evidenceless_plan_packs_not_verified():
    plan = _sample()
    del plan["evidence"]
    data, verified = build_bundle(plan)
    assert verified is False
    assert json.loads(_read(data, "evidence.json")) == {}
    outcome = verify_bundle(data)
    assert outcome["passed"] is False and outcome["chain_valid"] is False


def test_verify_bundle_round_trip():
    data, _ = build_bundle(_dated_plan())
    outcome = verify_bundle(data)
    assert outcome == {"ok": True, "manifest_valid": True,
                       "passed": True, "chain_valid": True,
                       "violations": [],
                       "files": ["evidence.json", "itinerary.ics",
                                 "itinerary.md", "itinerary.txt",
                                 "plan.json", "verification.json"]}


def _rezip(data, replacements):
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, replacements.get(name, src.read(name)))
    return buffer.getvalue()


def test_verify_bundle_edited_plan_fails_manifest():
    data, _ = build_bundle(_sample())
    edited = json.loads(_read(data, "plan.json"))
    edited["items"][0]["affinity"] += 0.05
    forged = _rezip(data, {"plan.json": json.dumps(edited).encode()})
    outcome = verify_bundle(forged)
    assert outcome["manifest_valid"] is False
    assert outcome["passed"] is False and outcome["ok"] is False


def test_verify_bundle_forged_report_never_trusted():
    data, _ = build_bundle(_tampered(_sample()))
    report = json.loads(_read(data, "verification.json"))
    report["passed"] = True
    report["violations"] = []
    forged = _rezip(data, {"verification.json":
                           json.dumps(report).encode()})
    outcome = verify_bundle(forged)
    assert outcome["passed"] is False and outcome["ok"] is False


def test_verify_bundle_rejects_garbage():
    for bad in (b"not a zip", b"", None, 42):
        with pytest.raises(ValueError, match="bad_bundle"):
            verify_bundle(bad)
    data, _ = build_bundle(_sample())
    with pytest.raises(ValueError, match="bad_bundle"):
        verify_bundle(data[: len(data) // 2])


def test_build_bundle_rejects_non_object():
    for bad in ([], "plan", None, 42):
        with pytest.raises(ValueError, match="bad_plan"):
            build_bundle(bad)
    assert bundle_verification(None)["passed"] is False


# --- CLI -----------------------------------------------------------------

def _cli(*argv):
    return subprocess.run([sys.executable, "-m", "tastepilot", *argv],
                          cwd=APP, capture_output=True, text=True,
                          timeout=60)


def test_cli_bundle_and_verify_bundle_round_trip(tmp_path):
    out = tmp_path / "plan.zip"
    run = _cli("bundle", "--plan", str(SAMPLE), "--out", str(out))
    assert run.returncode == 0, run.stderr
    assert "verified=True" in run.stdout
    check = _cli("verify-bundle", "--bundle", str(out))
    assert check.returncode == 0, check.stderr
    assert "ok=True" in check.stdout and "manifest_valid=True" \
        in check.stdout
    run = _cli("bundle", "--help")
    assert run.returncode == 0 and "--out" in run.stdout
    run = _cli("verify-bundle", "--help")
    assert run.returncode == 0 and "--bundle" in run.stdout


def test_cli_bundle_tampered_exits_1_but_writes(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_tampered(_sample())), encoding="utf-8")
    out = tmp_path / "bad.zip"
    run = _cli("bundle", "--plan", str(bad), "--out", str(out))
    assert run.returncode == 1 and out.exists()
    assert "Traceback" not in run.stderr
    check = _cli("verify-bundle", "--bundle", str(out))
    assert check.returncode == 1 and "passed=False" in check.stdout


def test_cli_bundle_errors_exit_2_without_traceback(tmp_path):
    run = _cli("bundle", "--plan", str(tmp_path / "missing.json"),
               "--out", str(tmp_path / "x.zip"))
    assert run.returncode == 2 and "Traceback" not in run.stderr
    run = _cli("verify-bundle", "--bundle",
               str(tmp_path / "missing.zip"))
    assert run.returncode == 2 and "Traceback" not in run.stderr
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"junk")
    run = _cli("verify-bundle", "--bundle", str(junk))
    assert run.returncode == 2 and "bad_bundle" in run.stderr


# --- server endpoint -----------------------------------------------------

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


def _post_bytes(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_server_bundle_download(server_url):
    status, headers, body = _post_bytes(server_url + "/api/bundle",
                                        _dated_plan())
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert "tastepilot-audit-bundle.zip" in \
        headers["Content-Disposition"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    outcome = verify_bundle(body)
    assert outcome["ok"] is True and "itinerary.ics" in outcome["files"]


def test_server_bundle_tampered_and_gates(server_url):
    status, _, body = _post_bytes(server_url + "/api/bundle",
                                  _tampered(_sample()))
    assert status == 200
    outcome = verify_bundle(body)
    assert outcome["passed"] is False
    status, _, _ = _post_bytes(server_url + "/api/bundle",
                               {"items": "nope"})
    assert status == 400


def test_ui_and_docs_carry_bundle_wiring():
    page = (APP / "tastepilot" / "static" / "index.html").read_text(
        encoding="utf-8")
    assert "bundlebtn" in page and "/api/bundle" in page
    assert "tastepilot-audit-bundle.zip" in page
    readme = (APP / "README.md").read_text(encoding="utf-8")
    assert "bundle.py" in readme and "verify-bundle" in readme
    submission = (APP / "SUBMISSION.md").read_text(encoding="utf-8")
    assert "audit bundle" in submission
    deploy = (APP / "DEPLOY.md").read_text(encoding="utf-8")
    assert "Audit bundle probe" in deploy
