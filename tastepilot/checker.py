"""Deterministic evidence checker — the core invariant enforcer.

A recommendation is admissible iff:
  1. it cites an evidence response_id that exists in the sealed log,
  2. that response is an insights call,
  3. the recommended entity actually appears in that response, and
  4. the cited affinity matches the response's affinity exactly.

Anything else is a hallucination by construction and is rejected (strict
mode) or flagged. The LLM never participates in this decision.
"""
from __future__ import annotations

from .evidence import qloo_responses, verify_chain

TOLERANCE = 1e-9


def check_item(item: dict, record: dict,
               responses: dict | None = None) -> dict:
    """Per-item evidence report for one recommendation.

    Returns {"item", "entity_id", "domain", "admissible", "reason",
    "evidence"} where reason is "ok" for an admissible item or the same
    violation code check_plan reports, and evidence summarises the cited
    Qloo response (id, endpoint, source, affinity found in it) so a UI
    can show a reader exactly what backs — or fails to back — the item.
    This is a reporting view over the same deterministic rules; it never
    changes an admissibility decision.
    """
    if responses is None:
        responses = qloo_responses(record)
    name = item.get("entity", {}).get("name")
    report = {"item": name, "entity_id": item.get("entity", {}).get("id"),
              "domain": item.get("domain"), "admissible": False,
              "reason": "ok", "evidence": None}
    ref = item.get("evidence_ref")
    if not ref or ref not in responses:
        report["reason"] = "missing_or_unknown_evidence_ref"
        return report
    resp = responses[ref]
    report["evidence"] = {"response_id": ref, "endpoint": resp.get("endpoint"),
                          "source": resp.get("source"),
                          "affinity_in_response": None}
    if resp.get("endpoint") != "insights":
        report["reason"] = "evidence_ref_not_insights"
        return report
    match = next((r for r in resp["results"]
                  if r["entity"]["id"] == item["entity"]["id"]), None)
    if match is None:
        report["reason"] = "entity_not_in_qloo_response"
        return report
    report["evidence"]["affinity_in_response"] = match["affinity"]
    if abs(float(match["affinity"]) - float(item["affinity"])) > TOLERANCE:
        report["reason"] = "affinity_mismatch"
        return report
    report["admissible"] = True
    return report


def item_reports(items: list[dict], record: dict) -> list[dict]:
    """check_item for every item, sharing one response index."""
    responses = qloo_responses(record)
    return [check_item(item, record, responses) for item in items]


def check_plan(items: list[dict], record: dict) -> dict:
    """Return {"chain_valid": bool, "violations": [...], "passed": bool}."""
    violations: list[dict] = []
    chain_valid = verify_chain(record)
    if not chain_valid:
        violations.append({"item": None, "reason": "evidence_chain_invalid"})
    for rep in item_reports(items, record):
        if not rep["admissible"]:
            violations.append({"item": rep["item"], "reason": rep["reason"]})
    return {"chain_valid": chain_valid, "violations": violations,
            "passed": chain_valid and not violations}


def plan_recommendations(plan: dict) -> list[dict]:
    """Every recommendation a plan makes: its primary ``items`` plus
    its evidence-backed ``alternatives`` (the lower-affinity results
    of the same sealed insights responses, offered as swap options).

    Alternatives are recommendations too, so every verification
    surface — CLI verify, exports, share payloads, the server's
    recomputed report — checks this combined list: tampering with an
    alternative's affinity fails verification exactly as tampering
    with a primary item does. Raises TypeError when either field is
    present but not a list, so malformed input reads as NOT VERIFIED
    rather than silently unchecked."""
    items = plan.get("items") or []
    alternatives = plan.get("alternatives") or []
    if not isinstance(items, list) or not isinstance(alternatives, list):
        raise TypeError("items/alternatives must be lists")
    return list(items) + list(alternatives)


def enforce(items: list[dict], record: dict) -> tuple[list[dict], list[dict], dict]:
    """Split items into (admissible, rejected) using the checker report."""
    report = check_plan(items, record)
    bad_names = {v["item"] for v in report["violations"] if v["item"] is not None}
    ok = [i for i in items if i["entity"]["name"] not in bad_names]
    rejected = [i for i in items if i["entity"]["name"] in bad_names]
    for item in rejected:
        item = dict(item)
    return ok, rejected, report
