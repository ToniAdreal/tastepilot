"""Visual branch timeline data — branches laid over a trip's legs.

Run 33's diff answers "what changed between these two plans?" as
summary lines. This module lays several branches side by side as a
timeline grid the UI renders as a table: rows are recommendation
slots grouped by leg (in the trip's mention order), columns are the
base plan plus each branch, and every cell states that slot's status
in that column — kept, added, removed, re-scored (affinity moved),
or absent — with its affinity.

Every column's verdict is recomputed from that plan's own sealed
record via diff.diff_plans (the stored checker field is never read),
so a tampered branch renders as a FAIL column in the timeline,
exactly as it verifies everywhere else. Slots are keyed by
(entity, domain, location) — diff's per-leg rule — so the same
entity in another leg is a different row.

Malformed input raises a categorised ValueError ("bad_plan" /
"bad_branches") — never a traceback, never an echo of the plans.
Inputs are never mutated.
"""
from __future__ import annotations

from .diff import TOLERANCE, _key, diff_plans

MAX_BRANCHES = 6
BASE_NAME = "As planned"


def validate_timeline_branches(branches) -> None:
    """Shape-gate the branch list; categorised ValueError on failure."""
    if not isinstance(branches, list) or not branches:
        raise ValueError("bad_branches")
    if len(branches) > MAX_BRANCHES:
        raise ValueError("too_many_branches")
    names = []
    for branch in branches:
        if not isinstance(branch, dict):
            raise ValueError("bad_branches")
        name = branch.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("bad_branches")
        if name.strip() in names:
            raise ValueError("duplicate_branch_name")
        names.append(name.strip())
        if not isinstance(branch.get("plan"), dict):
            raise ValueError("bad_branches")


def _describe(item: dict) -> dict:
    entity = item.get("entity") or {}
    return {"name": entity.get("name"), "entity_id": entity.get("id"),
            "domain": item.get("domain"), "location": item.get("location"),
            "affinity": item.get("affinity")}


def _leg_order(plans: list[dict]) -> list:
    order: list = []

    def note(location):
        if location not in order:
            order.append(location)

    for plan in plans:
        for location in plan.get("locations") or []:
            note(location)
        for item in plan.get("items") or []:
            note(item.get("location"))
    return order


def _schedule_of(plan: dict) -> dict:
    return {entry.get("location"): {"start": entry.get("start"),
                                    "end": entry.get("end")}
            for entry in plan.get("schedule") or []
            if isinstance(entry, dict)}


def branch_timeline(base, branches) -> dict:
    """Lay branches over the base plan's legs as a status grid.

    `branches` is a list of {"name": str, "plan": {...}}. Returns
    {columns, legs, summary} where columns[0] is the base itself.
    Raises ValueError("bad_plan"/"bad_branches"/...) for malformed
    input.
    """
    if not isinstance(base, dict):
        raise ValueError("bad_plan")
    validate_timeline_branches(branches)
    plans = [base] + [b["plan"] for b in branches]
    names = [BASE_NAME] + [b["name"].strip() for b in branches]

    # Per-column verdicts + diffs, all recomputed from sealed records.
    columns, diffs = [], []
    for idx, plan in enumerate(plans):
        result = diff_plans(base, plan)  # also shape-gates the plan
        diffs.append(result)
        side = result["base"] if idx == 0 else result["other"]
        columns.append({
            "name": names[idx], "is_base": idx == 0,
            "passed": side["passed"], "chain_valid": side["chain_valid"],
            "swap_count": side["swap_count"],
            "item_count": side["item_count"],
            "summary": result["summary"] if idx else {
                "added": 0, "removed": 0,
                "kept": result["summary"]["kept"],
                "affinity_changes": 0, "identical": True},
        })

    # Slot union: base order first, then slots only branches carry,
    # in branch/plan order.
    slot_order: list = []
    slot_info: dict = {}
    per_plan: list[dict] = []
    for plan in plans:
        index = {}
        for item in plan.get("items") or []:
            key = _key(item)
            index[key] = item
            if key not in slot_info:
                slot_info[key] = _describe(item)
                slot_order.append(key)
        per_plan.append(index)

    schedules = [_schedule_of(p) for p in plans]
    legs = []
    for location in _leg_order(plans):
        rows = []
        for key in slot_order:
            info = slot_info[key]
            if info["location"] != location:
                continue
            cells = []
            for col, index in enumerate(per_plan):
                item = index.get(key)
                in_base = key in per_plan[0]
                if item is None:
                    status = "removed" if (col > 0 and in_base) else "absent"
                    cells.append({"status": status, "affinity": None,
                                  "delta": None})
                    continue
                affinity = item.get("affinity")
                if col == 0 or not in_base:
                    status = "kept" if col == 0 else "added"
                    cells.append({"status": status, "affinity": affinity,
                                  "delta": None})
                    continue
                base_aff = per_plan[0][key].get("affinity")
                delta = None
                status = "kept"
                if isinstance(base_aff, (int, float)) and \
                        isinstance(affinity, (int, float)) and \
                        abs(float(affinity) - float(base_aff)) > TOLERANCE:
                    status = "rescored"
                    delta = float(affinity) - float(base_aff)
                cells.append({"status": status, "affinity": affinity,
                              "delta": delta})
            rows.append({**info, "cells": cells})
        legs.append({
            "location": location,
            "schedules": [s.get(location) for s in schedules],
            "rows": rows,
        })

    return {
        "columns": columns,
        "legs": legs,
        "summary": {
            "branches": len(branches),
            "legs": len(legs),
            "rows": len(slot_order),
            "all_passed": all(c["passed"] for c in columns),
        },
    }
