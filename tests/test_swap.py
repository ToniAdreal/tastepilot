"""One-tap swapping: a primary item exchanges places with one of
its evidence-backed alternatives. The swap is held to the plan's
own standard — preconditions recomputed (chain + full-plan checker
+ both items admissible), same domain and same leg only, and the
exchange is sealed into the evidence record as a swap_event so the
re-sealed record attests the whole history. A tampered plan can
never be swapped into a fresh seal."""
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
from tastepilot.checker import check_plan, plan_recommendations
from tastepilot.evidence import verify_chain
from tastepilot.export import export_plan
from tastepilot.handoff import plan_handoffs
from tastepilot.server import make_handler
from tastepilot.swap import swap_plan

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX = APP_ROOT / "tastepilot" / "static" / "index.html"
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
PRIMARY = "Gion, Kyoto"  # place primary in the Kyoto plan
ALT = "Kyoto National Museum"  # place alternative (affinity 0.75)


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def _swap(plan, primary=PRIMARY, alternative=ALT):
    return swap_plan(plan, primary, alternative)


def test_swap_exchanges_primary_and_alternative():
    plan = _kyoto()
    assert ALT in [a["entity"]["name"] for a in plan["alternatives"]]
    swapped = _swap(plan)
    names = [i["entity"]["name"] for i in swapped["items"]]
    assert ALT in names and PRIMARY not in names
    alt_names = [a["entity"]["name"] for a in swapped["alternatives"]]
    assert PRIMARY in alt_names and ALT not in alt_names
    # Position is preserved: the alternative takes the primary's slot.
    place_items = [i["entity"]["name"] for i in swapped["items"]
                   if i["domain"] == "place"]
    assert place_items[0] == ALT  # Gion was the first place primary
    assert swapped["checker"]["passed"] and swapped["checker"]["chain_valid"]
    for item in swapped["items"] + swapped["alternatives"]:
        assert item["verification"]["admissible"] is True
        assert item["verification"]["reason"] == "ok"


def test_swap_input_plan_is_not_mutated():
    plan = _kyoto()
    before = json.dumps(plan, sort_keys=True)
    _swap(plan)
    assert json.dumps(plan, sort_keys=True) == before


def test_swap_reseals_evidence_with_swap_event():
    plan = _kyoto()
    swapped = _swap(plan)
    record = swapped["evidence"]
    assert verify_chain(record)
    assert record["verdict"] == "PASS"
    assert record["record_hash"] != plan["evidence"]["record_hash"]
    events = [e for e in record["entries"] if e["kind"] == "swap_event"]
    assert len(events) == 1
    event = events[0]["swap"]
    assert event["primary"]["name"] == PRIMARY
    assert event["alternative"]["name"] == ALT
    assert event["record_hash_before"] == plan["evidence"]["record_hash"]
    # The original Qloo calls are replayed byte-identically.
    old_calls = [e["response"] for e in plan["evidence"]["entries"]
                 if e["kind"] == "qloo_call"]
    new_calls = [e["response"] for e in record["entries"]
                 if e["kind"] == "qloo_call"]
    assert old_calls == new_calls
    # Plan-level provenance mirrors the sealed event.
    assert swapped["swaps"] == [event]
    # And the swapped plan verifies through the standard surface.
    report = check_plan(plan_recommendations(swapped), record)
    assert report["passed"] and report["chain_valid"]


def test_swap_regroups_legs_groups_and_handoffs():
    plan = _kyoto()
    swapped = _swap(plan)
    grouped = [i["entity"]["name"] for g in swapped["groups"]
               for i in g["items"]]
    assert ALT in grouped and PRIMARY not in grouped
    legged = [i["entity"]["name"] for leg in swapped["legs"]
              for g in leg["groups"] for i in g["items"]]
    assert legged == grouped
    handed = [i["name"] for i in swapped["handoffs"]["items"]]
    assert ALT in handed and PRIMARY not in handed
    assert swapped["handoffs"] == plan_handoffs(swapped)


def test_swap_back_restores_primaries():
    swapped = _swap(_kyoto())
    restored = swap_plan(swapped, ALT, PRIMARY)
    assert [i["entity"]["name"] for i in restored["items"]] == \
        [i["entity"]["name"] for i in _kyoto()["items"]]
    assert len(restored["swaps"]) == 2
    assert len([e for e in restored["evidence"]["entries"]
                if e["kind"] == "swap_event"]) == 2
    assert verify_chain(restored["evidence"])
    assert restored["checker"]["passed"]


def test_swap_selector_dict_with_location():
    plan = _kyoto()
    swapped = swap_plan(
        plan,
        {"name": PRIMARY, "domain": "place", "location": "Kyoto"},
        {"entity_id": "plc_kyoto_museum"})
    assert ALT in [i["entity"]["name"] for i in swapped["items"]]


def test_swap_refusals_are_categorised():
    plan = _kyoto()
    with pytest.raises(ValueError, match="primary_not_found"):
        swap_plan(plan, "No Such Place", ALT)
    with pytest.raises(ValueError, match="alternative_not_found"):
        swap_plan(plan, PRIMARY, "No Such Place")
    with pytest.raises(ValueError, match="domain_mismatch"):
        # Kaiseki is a food alternative; Gion is a place primary.
        swap_plan(plan, PRIMARY, "A seasonal kaiseki tasting menu")
    with pytest.raises(ValueError, match="missing_evidence"):
        swap_plan({"items": [], "alternatives": []}, PRIMARY, ALT)
    with pytest.raises(ValueError, match="bad_plan"):
        swap_plan("not a plan", PRIMARY, ALT)


def test_tampered_plan_cannot_be_swapped_into_a_fresh_seal():
    plan = _kyoto()
    plan["alternatives"][0]["affinity"] = 0.01
    with pytest.raises(ValueError, match="plan_not_verified"):
        _swap(plan)
    plan2 = _kyoto()
    plan2["evidence"]["entries"][1]["response"]["results"][0][
        "affinity"] = 0.01
    with pytest.raises(ValueError, match="evidence_chain_invalid"):
        _swap(plan2)


def test_multi_city_swap_stays_within_its_leg():
    plan = TastePilotAgent().run(
        "a trip from Kyoto to Paris, I love Spirited Away and Amelie")
    # The museum is a Kyoto-leg place alternative; Montmartre is a
    # Paris-leg place primary — same domain, different leg: refused.
    with pytest.raises(ValueError, match="location_mismatch"):
        swap_plan(plan, "Montmartre, Paris", ALT)
    # A same-leg swap in a multi-city plan works and touches only
    # that leg's items.
    swapped = swap_plan(plan, PRIMARY, ALT)
    paris = [i["entity"]["name"] for leg in swapped["legs"]
             if leg["location"] == "Paris"
             for g in leg["groups"] for i in g["items"]]
    assert "Montmartre, Paris" in paris and ALT not in paris


def test_swapped_plan_exports_verified():
    swapped = _swap(_kyoto())
    content, verified = export_plan(swapped, "markdown")
    assert verified is True and "NOT VERIFIED" not in content
    assert ALT in content


def test_cli_swap_round_trip(tmp_path, capsys):
    plan = _kyoto()
    src = tmp_path / "plan.json"
    src.write_text(json.dumps(plan), encoding="utf-8")
    out = tmp_path / "swapped.json"
    rc = cli.main(["swap", "--plan", str(src), "--primary", PRIMARY,
                   "--alternative", ALT, "--out", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "checker_passed=True" in text and "swaps=1" in text
    swapped = json.loads(out.read_text(encoding="utf-8"))
    assert ALT in [i["entity"]["name"] for i in swapped["items"]]
    assert cli.main(["verify", "--record", str(out)]) == 0


def test_cli_swap_refusal_exits_2_without_traceback(tmp_path, capsys):
    plan = _kyoto()
    src = tmp_path / "plan.json"
    src.write_text(json.dumps(plan), encoding="utf-8")
    rc = cli.main(["swap", "--plan", str(src), "--primary", PRIMARY,
                   "--alternative", "No Such Place"])
    err = capsys.readouterr().err
    assert rc == 2 and "alternative_not_found" in err
    assert "Traceback" not in err
    rc = cli.main(["swap", "--plan", str(tmp_path / "missing.json"),
                   "--primary", PRIMARY, "--alternative", ALT])
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


def test_server_swap_endpoint():
    server, port = _serve(TastePilotAgent())
    try:
        status, plan, _ = _post(port, "/api/plan", {"text": KYOTO})
        assert status == 200
        status, swapped, headers = _post(port, "/api/swap", {
            "plan": plan,
            "primary": {"name": PRIMARY, "domain": "place",
                        "location": "Kyoto"},
            "alternative": {"name": ALT}})
        assert status == 200
        assert headers.get("Cache-Control") == "no-store"
        assert ALT in [i["entity"]["name"] for i in swapped["items"]]
        assert swapped["checker"]["passed"]
        assert verify_chain(swapped["evidence"])
        # The swapped plan verifies through the standard endpoint.
        status, report, _ = _post(port, "/api/verify", swapped)
        assert status == 200 and report["passed"]
        # Refusals: unknown alternative, domain mismatch, no plan.
        status, body, _ = _post(port, "/api/swap", {
            "plan": plan, "primary": PRIMARY,
            "alternative": "No Such Place"})
        assert status == 400 and "alternative_not_found" in body["error"]
        status, body, _ = _post(port, "/api/swap", {
            "plan": plan, "primary": PRIMARY,
            "alternative": "A seasonal kaiseki tasting menu"})
        assert status == 400 and "domain_mismatch" in body["error"]
        status, body, _ = _post(port, "/api/swap", {"primary": PRIMARY})
        assert status == 400
        # A tampered plan is refused, not re-sealed.
        tampered = copy.deepcopy(plan)
        tampered["items"][0]["affinity"] = 0.01
        status, body, _ = _post(port, "/api/swap", {
            "plan": tampered, "primary": PRIMARY, "alternative": ALT})
        assert status == 400 and "plan_not_verified" in body["error"]
    finally:
        server.shutdown()


def test_ui_and_docs_describe_swapping():
    page = INDEX.read_text()
    assert "/api/swap" in page and "swapbtn" in page
    assert "Swap in for" in page and "renderPlan" in page
    assert "swap" in README.read_text().lower()
    assert "One-tap swapping" not in SUBMISSION.read_text()
