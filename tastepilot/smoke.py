"""Real-mode smoke check: the first thing to run when a Qloo key lands.

One ``search`` call and one ``insights`` call (seeded by the search
results), each validated for payload shape — non-empty results, every
entity carrying a non-empty id and name, affinities finite and
non-negative — and both sealed into a hash-chained evidence record whose
chain is re-verified before the verdict is reported. This is the payload
-alignment tool for the real API: when the live response shape differs
from what the adapter expects, the per-step shape diagnostics say exactly
which step and which field broke, instead of a plan failing downstream.

The report never contains the API key: the client only ever places it in
the ``X-Api-Key`` header, and a test asserts a sentinel key appears
nowhere in the serialized report. Works against the mock client too, so
the smoke path itself is testable zero-key (Judge Mode).
"""
from __future__ import annotations

import math

from .evidence import EvidenceLog, verify_chain
from .qloo import QlooError

DEFAULT_QUERY = "Spirited Away"
DEFAULT_DOMAIN = "music"


def _shape(response) -> dict:
    """Payload-shape diagnostics for one parsed Qloo response."""
    results = response.results
    affinities = [r.affinity for r in results]
    return {
        "response_id": response.response_id,
        "source": response.source,
        "result_count": len(results),
        "domains": sorted({r.entity.domain for r in results}),
        "ids_present": all(bool(r.entity.id) for r in results),
        "names_present": all(bool(r.entity.name) for r in results),
        "affinities_finite": all(
            isinstance(a, (int, float)) and math.isfinite(a) and a >= 0
            for a in affinities),
        "affinity_min": min(affinities) if affinities else None,
        "affinity_max": max(affinities) if affinities else None,
        "sample_names": [r.entity.name for r in results[:3]],
    }


def _validate(step: str, response) -> tuple[bool, str, dict]:
    shape = _shape(response)
    if shape["result_count"] == 0:
        return False, "empty_results", shape
    if not shape["ids_present"]:
        return False, "missing_entity_id", shape
    if not shape["names_present"]:
        return False, "missing_entity_name", shape
    if not shape["affinities_finite"]:
        return False, "bad_affinity", shape
    return True, "ok", shape


def run_smoke(client, query: str = DEFAULT_QUERY,
              target_domain: str = DEFAULT_DOMAIN) -> dict:
    """Run search -> insights against ``client`` and return the report.

    Never raises for an API or shape failure — the failing step is
    reported with its reason and ``passed`` is False. The sealed evidence
    record is included under ``evidence_record`` so a first real-mode run
    can be saved and re-verified later with the ``verify`` tooling.
    """
    steps: list[dict] = []
    log = EvidenceLog(request_text=f"smoke: {query}", mode=f"smoke-{client.source}")

    search_resp = None
    try:
        search_resp = client.search(query)
    except QlooError as exc:
        steps.append({"name": "search", "ok": False, "reason": "api_error",
                      "detail": str(exc)})
    if search_resp is not None:
        log.add_qloo_call(search_resp.to_dict())
        ok, reason, shape = _validate("search", search_resp)
        steps.append({"name": "search", "ok": ok, "reason": reason,
                      "shape": shape})

    insights_resp = None
    if search_resp is not None and steps[-1]["ok"]:
        seed_ids = search_resp.entity_ids()[:3]
        try:
            insights_resp = client.insights(seed_ids, target_domain)
        except QlooError as exc:
            steps.append({"name": "insights", "ok": False,
                          "reason": "api_error", "detail": str(exc)})
        if insights_resp is not None:
            log.add_qloo_call(insights_resp.to_dict())
            ok, reason, shape = _validate("insights", insights_resp)
            steps.append({"name": "insights", "ok": ok, "reason": reason,
                          "seed_ids": seed_ids, "shape": shape})
    else:
        steps.append({"name": "insights", "ok": False,
                      "reason": "skipped_search_failed"})

    calls_ok = all(s["ok"] for s in steps)
    record = log.seal("PASS" if calls_ok else "FAIL")
    chain_valid = verify_chain(record)
    return {
        "tool": "tastepilot-smoke",
        "source": client.source,
        "query": query,
        "target_domain": target_domain,
        "steps": steps,
        "evidence": {"sealed_responses": sum(
            1 for e in record["entries"] if e["kind"] == "qloo_call"),
            "chain_valid": chain_valid,
            "record_hash": record["record_hash"]},
        "evidence_record": record,
        "passed": bool(calls_ok and chain_valid),
    }
