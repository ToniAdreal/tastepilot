"""Swap history + undo: the sealed swap_event chain as an audit
trail. History is recomputed from the sealed record alone (the
stored swaps mirror is never trusted), and undo reverses the
newest sealed event through swap_plan's full preconditions,
sealing the reversal as a further event that names the swap it
walked back. Undoing an undo redoes; nothing is ever removed
from the chain."""
import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.evidence import verify_chain
from tastepilot.export import export_plan
from tastepilot.server import make_handler
from tastepilot.swap import swap_history, swap_plan, undo_last_swap

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
DEPLOY = APP_ROOT / "DEPLOY.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
PRIMARY = "Gion, Kyoto"  # place primary
ALT = "Kyoto National Museum"  # place alternative


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def _swapped():
    return swap_plan(_kyoto(), PRIMARY, ALT)


def _second_swap(plan):
    """Swap a second domain's pair, chosen dynamically: the first
    alternative whose domain has a primary in the plan."""
    alt = next(a for a in plan["alternatives"]
               if any(i["domain"] == a["domain"]
                      and (i.get("location") or None) ==
                      (a.get("location") or None)
                      for i in plan["items"]))
    primary = next(i for i in plan["items"]
                   if i["domain"] == alt["domain"]
                   and (i.get("location") or None) ==
                   (alt.get("location") or None))
    return swap_plan(plan, primary["entity"]["name"],
                     alt["entity"]["name"]), primary, alt


def test_history_is_empty_for_a_fresh_plan():
    plan = _kyoto()
    assert swap_history(plan) == []
    assert "swap_history" not in plan  # only swapped plans carry it


def test_history_is_recomputed_from_the_sealed_record():
    swapped = _swapped()
    history = swap_history(swapped)
    assert len(history) == 1
    event = history[0]
    assert event["index"] == 1
    assert event["primary"]["name"] == PRIMARY
    assert event["alternative"]["name"] == ALT
    assert event["is_undo"] is False and event["undo_of"] is None
    assert event["swap_id"] == swapped["swaps"][0]["swap_id"]
    assert event["record_hash_before"]
    # swap_plan's reader field is the same recomputation.
    assert swapped["swap_history"] == history


def test_history_ignores_the_stored_swaps_mirror():
    swapped = _swapped()
    swapped["swaps"] = [{"swap_id": "fake", "primary": {"name": "Forged"},
                          "alternative": {"name": "Forged In"}}]
    history = swap_history(swapped)
    assert len(history) == 1
    assert history[0]["primary"]["name"] == PRIMARY


def test_history_requires_a_valid_chain():
    swapped = _swapped()
    swapped["evidence"]["entries"][1]["response"]["results"][0][
        "affinity"] = 0.01
    with pytest.raises(ValueError, match="evidence_chain_invalid"):
        swap_history(swapped)
    with pytest.raises(ValueError, match="missing_evidence"):
        swap_history({"items": [], "alternatives": []})
    with pytest.raises(ValueError, match="bad_plan"):
        swap_history("not a plan")


def test_undo_restores_the_primary_and_seals_the_reversal():
    swapped = _swapped()
    undone = undo_last_swap(swapped)
    names = [i["entity"]["name"] for i in undone["items"]]
    assert PRIMARY in names and ALT not in names
    assert ALT in [a["entity"]["name"] for a in undone["alternatives"]]
    assert undone["checker"]["passed"] and verify_chain(undone["evidence"])
    history = swap_history(undone)
    assert len(history) == 2
    assert history[1]["is_undo"] is True
    assert history[1]["undo_of"] == history[0]["swap_id"]
    # The reversed exchange is described in swap direction: the
    # alternative that was primary is named as the event's primary.
    assert history[1]["primary"]["name"] == ALT
    assert history[1]["alternative"]["name"] == PRIMARY
    assert undone["swap_history"] == history
    # The input plan is not mutated.
    assert len(swap_history(swapped)) == 1


def test_undo_of_undo_redoes_the_swap():
    undone = undo_last_swap(_swapped())
    redone = undo_last_swap(undone)
    names = [i["entity"]["name"] for i in redone["items"]]
    assert ALT in names and PRIMARY not in names
    history = swap_history(redone)
    assert len(history) == 3
    assert history[2]["is_undo"] is True
    assert history[2]["undo_of"] == history[1]["swap_id"]


def test_undo_is_lifo_across_domains():
    swapped, primary2, alt2 = _second_swap(_swapped())
    assert len(swap_history(swapped)) == 2
    undone = undo_last_swap(swapped)
    # Only the SECOND swap is reversed; the first stands.
    names = [i["entity"]["name"] for i in undone["items"]]
    assert primary2["entity"]["name"] in names
    assert ALT in names and PRIMARY not in names
    assert swap_history(undone)[-1]["undo_of"] == \
        swap_history(swapped)[1]["swap_id"]


def test_undo_refusals_are_categorised():
    with pytest.raises(ValueError, match="no_swaps_to_undo"):
        undo_last_swap(_kyoto())
    swapped = _swapped()
    swapped["items"][0]["affinity"] = 0.01  # checker no longer passes
    with pytest.raises(ValueError, match="plan_not_verified"):
        undo_last_swap(swapped)
    swapped2 = _swapped()
    swapped2["evidence"]["entries"][1]["response"]["results"][0][
        "affinity"] = 0.01  # chain itself broken
    with pytest.raises(ValueError, match="evidence_chain_invalid"):
        undo_last_swap(swapped2)


def test_cli_history_and_undo_round_trip(tmp_path, capsys):
    src = tmp_path / "plan.json"
    src.write_text(json.dumps(_swapped()), encoding="utf-8")
    rc = cli.main(["history", "--plan", str(src)])
    out = capsys.readouterr().out
    assert rc == 0 and f"{PRIMARY} -> {ALT}" in out
    assert "swap history: 1 sealed swap(s)" in out
    undone_path = tmp_path / "undone.json"
    rc = cli.main(["undo", "--plan", str(src), "--out", str(undone_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "undo:" in out and "swaps=2" in out
    undone = json.loads(undone_path.read_text(encoding="utf-8"))
    assert PRIMARY in [i["entity"]["name"] for i in undone["items"]]
    assert cli.main(["verify", "--record", str(undone_path)]) == 0


def test_cli_history_and_undo_failures(tmp_path, capsys):
    plan = _kyoto()
    src = tmp_path / "plan.json"
    src.write_text(json.dumps(plan), encoding="utf-8")
    rc = cli.main(["undo", "--plan", str(src)])
    err = capsys.readouterr().err
    assert rc == 2 and "no_swaps_to_undo" in err
    assert "Traceback" not in err
    tampered = _swapped()
    tampered["evidence"]["entries"][1]["response"]["results"][0][
        "affinity"] = 0.01
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    rc = cli.main(["history", "--plan", str(bad)])
    err = capsys.readouterr().err
    assert rc == 1 and "evidence_chain_invalid" in err
    rc = cli.main(["history", "--plan", str(tmp_path / "missing.json")])
    assert rc == 2


def _serve(agent):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(agent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def test_server_undo_endpoint_and_verify_history():
    server, port = _serve(TastePilotAgent())
    try:
        status, plan, _ = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        status, swapped, _ = _post(port, "/api/swap", {
            "plan": plan, "primary": PRIMARY, "alternative": ALT})
        assert status == 200
        # The verify report carries the recomputed audit trail.
        status, report, _ = _post(port, "/api/verify", swapped)
        assert status == 200 and report["passed"]
        assert report["swap_count"] == 1 and report["swaps_verified"]
        assert report["swap_history"][0]["primary"]["name"] == PRIMARY
        # Undo over HTTP restores the primary and seals the reversal.
        status, undone, headers = _post(port, "/api/undo",
                                        {"plan": swapped})
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        assert PRIMARY in [i["entity"]["name"] for i in undone["items"]]
        assert len(undone["swap_history"]) == 2
        status, report, _ = _post(port, "/api/verify", undone)
        assert status == 200 and report["passed"]
        assert report["swap_history"][1]["is_undo"] is True
        # Refusals: a plan with no swaps, a tampered plan, no plan.
        status, body, _ = _post(port, "/api/undo", {"plan": plan})
        assert status == 400 and "no_swaps_to_undo" in body["error"]
        tampered = copy.deepcopy(swapped)
        tampered["items"][0]["affinity"] = 0.01
        status, body, _ = _post(port, "/api/undo", {"plan": tampered})
        assert status == 400 and "plan_not_verified" in body["error"]
        status, body, _ = _post(port, "/api/undo", {})
        assert status == 400
        # A stored-mirror forgery is reported as claimed, never as
        # sealed history: verify still passes the plan (the mirror is
        # not evidence) but the audit trail stays the sealed one.
        forged = copy.deepcopy(swapped)
        forged["swaps"] = [{"swap_id": "x" * 32,
                            "primary": {"name": "Forged"},
                            "alternative": {"name": "Forged In"}}]
        status, report, _ = _post(port, "/api/verify", forged)
        assert status == 200 and report["swap_count"] == 1
        assert report["swaps_claimed"] == 1
        assert report["swap_history"][0]["primary"]["name"] == PRIMARY
    finally:
        server.shutdown()


def test_exports_carry_the_sealed_audit_trail():
    undone = undo_last_swap(_swapped())
    md, verified = export_plan(undone, "markdown")
    assert verified is True
    assert "## Swap history (sealed audit trail)" in md
    assert f"{PRIMARY} -> {ALT}" in md and "undo of swap" in md
    text, _ = export_plan(undone, "text")
    assert "Swap history (sealed audit trail):" in text
    # A plan with no swaps has no section.
    clean, _ = export_plan(_kyoto(), "markdown")
    assert "Swap history" not in clean
    # A tampered swapped plan exports no audit trail — the banner
    # says NOT VERIFIED and a broken chain yields no history.
    tampered = _swapped()
    tampered["evidence"]["entries"][1]["response"]["results"][0][
        "affinity"] = 0.01
    content, verified = export_plan(tampered, "markdown")
    assert verified is False and "NOT VERIFIED" in content
    assert "Swap history" not in content


def test_ui_and_docs_describe_history_and_undo():
    page = INDEX.read_text()
    assert "/api/undo" in page and "undobtn" in page
    assert "Undo last swap" in page and "swap_history" in page
    assert "Swap history" in page
    readme = README.read_text()
    assert "undo_last_swap" in readme and "swap_history" in readme
    submission = SUBMISSION.read_text()
    assert "Undo" in submission
    assert "Undo and swap history in the UI" not in submission
    assert "probe" in DEPLOY.read_text().lower()
    assert "Undo probe" in DEPLOY.read_text()
