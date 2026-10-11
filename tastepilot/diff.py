"""Plan comparison — what changed between two plans, verdicts recomputed.

The branch explorer (swap.explore_branches) forks one base plan into
named swap paths; until now a branch was summarised by its item names
alone, so "what actually changed?" meant eyeballing two lists. This
module answers it deterministically: two plans are compared item by
item (primary items, plus the alternatives lists), and BOTH plans'
verification verdicts are recomputed from their own sealed evidence
records — a plan's stored checker field is never read, so a tampered
plan diffs as NOT VERIFIED on its side of the comparison, exactly as
it verifies everywhere else.

Items are keyed by (entity_id, domain, location): the same entity in
a different leg of a multi-city plan is a different recommendation
slot (run 12's per-leg rule), so moving an item between legs reads as
a removal plus an addition, never as a silent keep. Affinity changes
are reported for kept slots whose affinity moved. Schedule changes
are reported per leg location. Swap counts come from the sealed swap
history (swap.swap_history), never the stored swaps mirror; a plan
whose chain does not verify reports swap_count=None.

Malformed input (a non-dict plan, non-list items) raises a
categorised ValueError("bad_plan") — never a traceback, never an echo
of the submitted plans.
"""
from __future__ import annotations

from .checker import check_plan, plan_recommendations
from .evidence import verify_chain
from .swap import swap_history

TOLERANCE = 1e-9


def _describe(item: dict) -> dict:
    entity = item.get("entity") or {}
    return {"name": entity.get("name"), "entity_id": entity.get("id"),
            "domain": item.get("domain"), "location": item.get("location"),
            "affinity": item.get("affinity"),
            "evidence_ref": item.get("evidence_ref")}


def _key(item: dict) -> tuple:
    described = _describe(item)
    return (described["entity_id"] or described["name"],
            described["domain"], described["location"])


def _index(items: list[dict]) -> dict:
    indexed = {}
    for item in items:
        indexed.setdefault(_key(item), item)
    return indexed


def _verdict(plan: dict) -> dict:
    """Recompute one plan's verdict from its sealed record."""
    record = plan.get("evidence")
    if not isinstance(record, dict):
        return {"passed": False, "chain_valid": False,
                "violations": [{"item": None,
                                "reason": "missing_evidence"}],
                "record_verdict": None}
    try:
        recommendations = plan_recommendations(plan)
        report = check_plan(recommendations, record)
    except (TypeError, KeyError, AttributeError, ValueError):
        return {"passed": False, "chain_valid": verify_chain(record),
                "violations": [{"item": None, "reason": "bad_plan"}],
                "record_verdict": record.get("verdict")}
    return {"passed": report["passed"],
            "chain_valid": report["chain_valid"],
            "violations": report["violations"],
            "record_verdict": record.get("verdict")}


def _swap_count(plan: dict):
    try:
        return len(swap_history(plan))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _side(plan: dict) -> dict:
    verdict = _verdict(plan)
    return {"request": plan.get("request"),
            "passed": verdict["passed"],
            "chain_valid": verdict["chain_valid"],
            "violations": verdict["violations"],
            "record_verdict": verdict["record_verdict"],
            "item_count": len(plan.get("items") or []),
            "alternative_count": len(plan.get("alternatives") or []),
            "swap_count": _swap_count(plan),
            "locations": list(plan.get("locations") or [])}


def _list_diff(base_items: list[dict], other_items: list[dict]):
    base_index, other_index = _index(base_items), _index(other_items)
    added = [_describe(other_index[k]) for k in other_index
             if k not in base_index]
    removed = [_describe(base_index[k]) for k in base_index
               if k not in other_index]
    kept, affinity_changes = [], []
    for key in base_index:
        if key not in other_index:
            continue
        before, after = base_index[key], other_index[key]
        kept.append(_describe(after))
        base_aff, other_aff = before.get("affinity"), after.get("affinity")
        if isinstance(base_aff, (int, float)) and \
                isinstance(other_aff, (int, float)) and \
                abs(float(other_aff) - float(base_aff)) > TOLERANCE:
            described = _describe(after)
            affinity_changes.append({
                "name": described["name"],
                "entity_id": described["entity_id"],
                "domain": described["domain"],
                "location": described["location"],
                "base_affinity": base_aff, "other_affinity": other_aff,
                "delta": float(other_aff) - float(base_aff)})
    return added, removed, kept, affinity_changes


def _schedule_changes(base: dict, other: dict) -> list[dict]:
    def by_location(plan):
        schedule = plan.get("schedule") or []
        return {entry.get("location"): entry for entry in schedule
                if isinstance(entry, dict)}
    base_map, other_map = by_location(base), by_location(other)
    changes = []
    for location in sorted(set(base_map) | set(other_map), key=str):
        before, after = base_map.get(location), other_map.get(location)
        before_span = (before.get("start"), before.get("end")) \
            if before else (None, None)
        after_span = (after.get("start"), after.get("end")) \
            if after else (None, None)
        if before_span != after_span:
            changes.append({
                "location": location,
                "base": {"start": before_span[0], "end": before_span[1]},
                "other": {"start": after_span[0], "end": after_span[1]}})
    return changes


def diff_plans(base, other) -> dict:
    """Compare two plans; both verdicts recomputed, never trusted.

    Returns {base, other, added, removed, kept, affinity_changes,
    alternatives_added, alternatives_removed, schedule_changes,
    summary}. Raises ValueError("bad_plan") for malformed input.
    """
    for plan in (base, other):
        if not isinstance(plan, dict):
            raise ValueError("bad_plan")
        for field in ("items", "alternatives"):
            value = plan.get(field, [])
            if not isinstance(value, list) or \
                    any(not isinstance(i, dict) for i in value):
                raise ValueError("bad_plan")
    added, removed, kept, affinity_changes = _list_diff(
        base.get("items") or [], other.get("items") or [])
    alt_added, alt_removed, _alt_kept, _alt_aff = _list_diff(
        base.get("alternatives") or [], other.get("alternatives") or [])
    schedule_changes = _schedule_changes(base, other)
    identical = not added and not removed and not affinity_changes \
        and not alt_added and not alt_removed and not schedule_changes
    return {
        "base": _side(base),
        "other": _side(other),
        "added": added, "removed": removed, "kept": kept,
        "affinity_changes": affinity_changes,
        "alternatives_added": alt_added,
        "alternatives_removed": alt_removed,
        "schedule_changes": schedule_changes,
        "summary": {"added": len(added), "removed": len(removed),
                    "kept": len(kept),
                    "affinity_changes": len(affinity_changes),
                    "alternatives_added": len(alt_added),
                    "alternatives_removed": len(alt_removed),
                    "schedule_changes": len(schedule_changes),
                    "identical": identical},
    }
