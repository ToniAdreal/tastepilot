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


def check_plan(items: list[dict], record: dict) -> dict:
    """Return {"chain_valid": bool, "violations": [...], "passed": bool}."""
    violations: list[dict] = []
    chain_valid = verify_chain(record)
    if not chain_valid:
        violations.append({"item": None, "reason": "evidence_chain_invalid"})
    responses = qloo_responses(record)
    for item in items:
        ref = item.get("evidence_ref")
        name = item.get("entity", {}).get("name")
        if not ref or ref not in responses:
            violations.append({"item": name, "reason": "missing_or_unknown_evidence_ref"})
            continue
        resp = responses[ref]
        if resp.get("endpoint") != "insights":
            violations.append({"item": name, "reason": "evidence_ref_not_insights"})
            continue
        match = next((r for r in resp["results"]
                      if r["entity"]["id"] == item["entity"]["id"]), None)
        if match is None:
            violations.append({"item": name, "reason": "entity_not_in_qloo_response"})
            continue
        if abs(float(match["affinity"]) - float(item["affinity"])) > TOLERANCE:
            violations.append({"item": name, "reason": "affinity_mismatch"})
    return {"chain_valid": chain_valid, "violations": violations,
            "passed": chain_valid and not violations}


def enforce(items: list[dict], record: dict) -> tuple[list[dict], list[dict], dict]:
    """Split items into (admissible, rejected) using the checker report."""
    report = check_plan(items, record)
    bad_names = {v["item"] for v in report["violations"] if v["item"] is not None}
    ok = [i for i in items if i["entity"]["name"] not in bad_names]
    rejected = [i for i in items if i["entity"]["name"] in bad_names]
    for item in rejected:
        item = dict(item)
    return ok, rejected, report
