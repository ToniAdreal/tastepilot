"""One-tap swapping: exchange a primary item with one of its
evidence-backed alternatives, with the checker re-run and the plan
re-sealed around the swap.

A swap is a plan mutation, and mutations of an evidence-backed plan
are held to the same standard as the plan itself:

  * Preconditions are recomputed, never read from stored fields: the
    sealed chain must verify, the whole plan (items + alternatives)
    must pass the checker, and both swapped recommendations must
    individually trace to their sealed Qloo responses.
  * A swap only ever exchanges two recommendations for the SAME
    domain and the SAME leg (location) — an alternative is a swap
    option for the domain/leg whose insights response produced it.
  * The swap introduces no new recommendation and no new Qloo data:
    both items were already in the plan, already verified. What
    changes is which one is primary.
  * The exchange is then sealed into the evidence record itself as
    a ``swap_event`` entry (see evidence.EvidenceLog.add_swap_event):
    the re-sealed record carries a new hash/signature and the
    recomputed verdict, the original Qloo calls are replayed
    byte-identically, and verify_chain attests the whole history —
    original calls plus every swap — at once. A stored ``swaps``
    field in a plan JSON is provenance for readers; verification
    never trusts it, because the sealed event is the record.

Failures raise ValueError with a categorised message (``swap
failed:`` prefix added by callers) — never a traceback, never an
echo of the submitted plan.

History and undo live here too. ``swap_history`` recomputes a
plan's audit trail from the sealed swap_event entries alone (the
stored ``swaps`` mirror is never trusted), and ``undo_last_swap``
reverses the newest sealed event by re-running the same exchange
through every swap precondition and sealing the reversal as
another event whose ``undo_of`` names the swap it walked back.

Branching lives here too. ``explore_branches`` forks ONE base
plan into several named swap paths — each branch is a list of
swaps applied to the base, independently — and returns every
branch's resulting plan plus a comparison whose verdicts
(passed / chain_valid / sealed swap count) are recomputed from
each branch's own re-sealed record. Branches never share state:
applying one branch cannot change another, a failing branch is
reported with its categorised refusal beside the successful
ones instead of aborting the exploration, and the base plan is
never mutated. A branch plan is an ordinary swapped plan (its
``branch`` field is reader-facing provenance, like the ``swaps``
mirror — verification never trusts it), so every existing
surface — verify, export, share, further swaps, undo — works
on it unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import checker
from .evidence import EvidenceLog, verify_chain


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _matches(item: dict, selector) -> bool:
    """A selector is a name string or a dict with any of name /
    entity_id / domain / location / evidence_ref. Every field the
    selector names must match the item exactly."""
    if isinstance(selector, str):
        selector = {"name": selector}
    if not isinstance(selector, dict):
        return False
    entity = item.get("entity") or {}
    fields = {"name": entity.get("name"),
              "entity_id": entity.get("id"),
              "domain": item.get("domain"),
              "location": item.get("location"),
              "evidence_ref": item.get("evidence_ref")}
    named = {k: v for k, v in selector.items() if k in fields}
    if not named:
        return False
    return all(fields[k] == v for k, v in named.items())


def _find(items: list[dict], selector, not_found: str) -> int:
    hits = [i for i, item in enumerate(items) if _matches(item, selector)]
    if not hits:
        raise ValueError(not_found)
    if len(hits) > 1:
        raise ValueError("ambiguous_selector")
    return hits[0]


def _describe(item: dict) -> dict:
    return {"entity_id": (item.get("entity") or {}).get("id"),
            "name": (item.get("entity") or {}).get("name"),
            "domain": item.get("domain"),
            "location": item.get("location"),
            "evidence_ref": item.get("evidence_ref"),
            "affinity": item.get("affinity")}


def _regroup(plan: dict, items: list[dict]) -> tuple[list, list]:
    """Rebuild groups + legs from an item list exactly as the agent
    builds them: domain groups in the plan's domain order; legs in
    location order, carrying their schedule dates."""
    domains = plan.get("domains") or []
    extra_domains = [i["domain"] for i in items if i["domain"] not in domains]
    ordered_domains = list(domains) + list(dict.fromkeys(extra_domains))
    groups = []
    for domain in ordered_domains:
        members = [i for i in items if i["domain"] == domain]
        if members:
            groups.append({"domain": domain, "items": members})
    dates_by_location = {d.get("location"): d
                         for d in plan.get("schedule") or []
                         if isinstance(d, dict)}
    locations = list(plan.get("locations") or [])
    for item in items:
        if item.get("location") not in locations:
            locations.append(item.get("location"))
    if not locations:
        locations = [None]
    legs = []
    for location in locations:
        leg_groups = []
        for domain in ordered_domains:
            members = [i for i in items if i["domain"] == domain
                       and i.get("location") == location]
            if members:
                leg_groups.append({"domain": domain, "items": members})
        if leg_groups:
            leg = {"location": location, "groups": leg_groups}
            info = dates_by_location.get(location) or {}
            for key in ("start", "end", "nights"):
                leg[key] = info.get(key)
            legs.append(leg)
    return groups, legs


def sealed_swap_events(record) -> list[dict]:
    """The swap events sealed into a record, in chain order.

    This is the only authoritative swap history: a plan's stored
    ``swaps`` field is a reader-facing mirror, and any surface that
    reports history derives it from the sealed entries here. A
    malformed record simply yields no events (callers that need to
    distinguish "no swaps" from "unverifiable" check the chain
    first — see swap_history)."""
    if not isinstance(record, dict):
        return []
    entries = record.get("entries")
    if not isinstance(entries, list):
        return []
    events = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("kind") == "swap_event" \
                and isinstance(entry.get("swap"), dict):
            events.append(entry["swap"])
    return events


def swap_history(plan: dict) -> list[dict]:
    """Recompute a plan's swap history from its sealed record.

    Returns one entry per sealed swap_event, in order:
    ``{index, swap_id, primary, alternative, record_hash_before,
    undo_of, is_undo}``. The stored ``swaps`` field is never read —
    editing it changes nothing here. Raises ValueError with a
    categorised message: bad_plan / missing_evidence /
    evidence_chain_invalid (a history read from a broken chain
    would be a claim, not an audit)."""
    if not isinstance(plan, dict):
        raise ValueError("bad_plan")
    record = plan.get("evidence")
    if not isinstance(record, dict) or \
            not isinstance(record.get("entries"), list) or not record["entries"]:
        raise ValueError("missing_evidence")
    if not verify_chain(record):
        raise ValueError("evidence_chain_invalid")
    history = []
    for i, event in enumerate(sealed_swap_events(record)):
        history.append({
            "index": i + 1,
            "swap_id": event.get("swap_id"),
            "primary": event.get("primary"),
            "alternative": event.get("alternative"),
            "record_hash_before": event.get("record_hash_before"),
            "undo_of": event.get("undo_of"),
            "is_undo": bool(event.get("undo_of")),
        })
    return history


def swap_plan(plan: dict, primary_selector, alternative_selector,
              *, undo_of: str | None = None) -> dict:
    """Return a NEW plan with the selected primary and alternative
    exchanged. The input plan is never mutated.

    Raises ValueError with a categorised message on any refusal:
    bad_plan / missing_evidence / evidence_chain_invalid /
    plan_not_verified / primary_not_found / alternative_not_found /
    ambiguous_selector / domain_mismatch / location_mismatch /
    primary_not_admissible / alternative_not_admissible.
    """
    if not isinstance(plan, dict):
        raise ValueError("bad_plan")
    items = plan.get("items")
    alternatives = plan.get("alternatives")
    if not isinstance(items, list) or not isinstance(alternatives, list) or \
            any(not isinstance(i, dict) for i in items + alternatives):
        raise ValueError("bad_plan")
    record = plan.get("evidence")
    if not isinstance(record, dict) or \
            not isinstance(record.get("entries"), list) or not record["entries"]:
        raise ValueError("missing_evidence")
    if not verify_chain(record):
        raise ValueError("evidence_chain_invalid")
    # The whole plan must verify before it may be mutated: a swap
    # must never launder a tampered plan into a fresh seal.
    try:
        pre = checker.check_plan(checker.plan_recommendations(plan), record)
    except TypeError:
        raise ValueError("bad_plan") from None
    if not pre["passed"]:
        raise ValueError("plan_not_verified")
    pi = _find(items, primary_selector, "primary_not_found")
    ai = _find(alternatives, alternative_selector, "alternative_not_found")
    primary, alternative = items[pi], alternatives[ai]
    if primary.get("domain") != alternative.get("domain"):
        raise ValueError("domain_mismatch")
    if primary.get("location") != alternative.get("location"):
        raise ValueError("location_mismatch")
    reports = checker.item_reports([primary, alternative], record)
    if not reports[0]["admissible"]:
        raise ValueError("primary_not_admissible")
    if not reports[1]["admissible"]:
        raise ValueError("alternative_not_admissible")

    new_plan = copy.deepcopy(plan)
    new_items = new_plan["items"]
    new_alternatives = new_plan["alternatives"]
    new_items[pi], new_alternatives[ai] = new_alternatives[ai], new_items[pi]

    event = {"primary": _describe(primary),
             "alternative": _describe(alternative),
             "record_hash_before": record.get("record_hash")}
    if undo_of is not None:
        # An undo is a swap like any other, but the sealed event
        # names the swap it reverses, so the audit trail reads as a
        # stack: each undo points at the event it walked back.
        event["undo_of"] = undo_of
    event["swap_id"] = hashlib.sha256(
        _canonical(event).encode("utf-8")).hexdigest()[:32]

    # Recompute the verdict over the swapped recommendations against
    # the original record, then re-seal: replay the original entries,
    # append the swap event, and seal with the recomputed verdict.
    post = checker.check_plan(
        checker.plan_recommendations(
            {"items": new_items, "alternatives": new_alternatives}), record)
    verdict = "PASS" if post["passed"] and new_items else "FAIL"
    log = EvidenceLog.from_record(record)
    log.add_swap_event(event)
    new_record = log.seal(verdict)
    if not verify_chain(new_record):  # the reseal must itself verify
        raise ValueError("evidence_chain_invalid")
    new_plan["evidence"] = new_record

    swaps = new_plan.get("swaps")
    new_plan["swaps"] = (list(swaps) if isinstance(swaps, list) else []) \
        + [event]
    # The reader-facing history is recomputed from the new sealed
    # record, never accumulated from the input's stored fields.
    new_plan["swap_history"] = swap_history(new_plan)

    item_reports = checker.item_reports(new_items, new_record)
    alt_reports = checker.item_reports(new_alternatives, new_record)
    for item, rep in zip(new_items, item_reports):
        item["verification"] = rep
    for alt, rep in zip(new_alternatives, alt_reports):
        alt["verification"] = rep
    final = checker.check_plan(
        checker.plan_recommendations(
            {"items": new_items, "alternatives": new_alternatives}),
        new_record)
    new_plan["checker"] = {
        "passed": final["passed"] and bool(new_items),
        "chain_valid": final["chain_valid"],
        "violations": final["violations"],
        "items": item_reports,
        "alternatives": alt_reports}
    new_plan["groups"], new_plan["legs"] = _regroup(new_plan, new_items)
    # The explainer summary is over the primary items: a swap can
    # change which explanations are primary, so recompute its counts.
    summary = dict(new_plan.get("explainer") or {})
    if summary:
        summary["items_explained"] = len(new_items)
        summary["llm_written"] = sum(
            1 for i in new_items
            if (i.get("explanation") or {}).get("source") != "template")
        summary["template_written"] = sum(
            1 for i in new_items
            if (i.get("explanation") or {}).get("source") == "template"
            and not (i.get("explanation") or {}).get("fallback_from"))
        summary["fallback"] = sum(
            1 for i in new_items
            if (i.get("explanation") or {}).get("fallback_from"))
        new_plan["explainer"] = summary
    from .handoff import plan_handoffs
    new_plan["handoffs"] = plan_handoffs(new_plan)
    return new_plan


def _selector_from(described: dict) -> dict:
    """Rebuild an exact selector from a sealed event's item
    description (the _describe shape)."""
    return {"name": described.get("name"),
            "entity_id": described.get("entity_id"),
            "domain": described.get("domain"),
            "location": described.get("location"),
            "evidence_ref": described.get("evidence_ref")}


def undo_last_swap(plan: dict) -> dict:
    """Return a NEW plan with the most recent swap reversed.

    The reversal target comes from the sealed history
    (swap_history), never from the stored ``swaps`` mirror: the
    last event's alternative is currently primary and its primary
    is currently the alternative, so undoing is the same exchange
    in reverse — run through swap_plan's full precondition checks
    and sealed as a new swap_event carrying ``undo_of`` (the
    swap_id it reverses). Undoing an undo therefore redoes the
    original swap; the history is a stack, not a one-way ratchet.
    The undone event is never removed from the record — the chain
    attests swaps and their undos alike.

    Raises ValueError with a categorised message: swap_history's
    categories, no_swaps_to_undo, or any swap_plan refusal."""
    history = swap_history(plan)
    if not history:
        raise ValueError("no_swaps_to_undo")
    last = history[-1]
    return swap_plan(plan, _selector_from(last["alternative"]),
                     _selector_from(last["primary"]),
                     undo_of=last["swap_id"])


MAX_BRANCHES = 6        # branches per exploration
MAX_BRANCH_SWAPS = 10   # swaps per branch path
MAX_BRANCH_NAME = 60    # characters


def validate_branches(branches) -> None:
    """Shape gate for a branch spec, shared by the CLI and server.

    A spec is a non-empty list of ``{name, swaps}`` objects: names
    are 1–60 chars and unique, each ``swaps`` is a list of at most
    MAX_BRANCH_SWAPS ``{primary, alternative}`` steps whose
    selectors are names or selector dicts. Raises ValueError with
    a categorised message — bad_branches / too_many_branches /
    bad_branch / bad_branch_name / duplicate_branch_name /
    bad_branch_swaps / too_many_swaps / bad_branch_step. This is a
    shape check only; whether a path applies is recomputed per
    branch by explore_branches."""
    if not isinstance(branches, list) or not branches:
        raise ValueError("bad_branches")
    if len(branches) > MAX_BRANCHES:
        raise ValueError("too_many_branches")
    seen = set()
    for branch in branches:
        if not isinstance(branch, dict):
            raise ValueError("bad_branch")
        name = branch.get("name")
        if not isinstance(name, str) or not name.strip() \
                or len(name) > MAX_BRANCH_NAME:
            raise ValueError("bad_branch_name")
        if name in seen:
            raise ValueError("duplicate_branch_name")
        seen.add(name)
        swaps = branch.get("swaps")
        if not isinstance(swaps, list):
            raise ValueError("bad_branch_swaps")
        if len(swaps) > MAX_BRANCH_SWAPS:
            raise ValueError("too_many_swaps")
        for step in swaps:
            if not isinstance(step, dict) \
                    or "primary" not in step or "alternative" not in step \
                    or not all(isinstance(step[k], (str, dict))
                               for k in ("primary", "alternative")):
                raise ValueError("bad_branch_step")


def _verify_base(plan: dict) -> None:
    """The swap_plan precondition preamble, for a branch path with
    no swaps: the base itself must be a verifiable plan before an
    empty branch may present it as one path among the compared."""
    if not isinstance(plan, dict):
        raise ValueError("bad_plan")
    items = plan.get("items")
    alternatives = plan.get("alternatives")
    if not isinstance(items, list) or not isinstance(alternatives, list) or \
            any(not isinstance(i, dict) for i in items + alternatives):
        raise ValueError("bad_plan")
    record = plan.get("evidence")
    if not isinstance(record, dict) or \
            not isinstance(record.get("entries"), list) or not record["entries"]:
        raise ValueError("missing_evidence")
    if not verify_chain(record):
        raise ValueError("evidence_chain_invalid")
    try:
        report = checker.check_plan(checker.plan_recommendations(plan),
                                    record)
    except TypeError:
        raise ValueError("bad_plan") from None
    if not report["passed"]:
        raise ValueError("plan_not_verified")


def apply_swap_path(plan: dict, swaps: list) -> dict:
    """Apply a branch's swaps to a plan, in order.

    Each step runs through swap_plan unchanged (full preconditions,
    re-seal), so a path is only ever as trustworthy as the linear
    swaps it is made of. Any refusal propagates with swap_plan's
    categorised message; the input plan is never mutated and no
    partial path is returned."""
    result = plan
    for step in swaps:
        result = swap_plan(result, step["primary"], step["alternative"])
    if result is plan:  # an empty path still proves the base first
        _verify_base(plan)
        result = copy.deepcopy(plan)
    return result


def _branch_verdict(plan: dict) -> dict:
    """Recomputed comparison fields for one branch plan: verdicts
    come from the checker against the branch's own sealed record,
    never from the plan's stored checker field."""
    record = plan.get("evidence") or {}
    try:
        report = checker.check_plan(checker.plan_recommendations(plan),
                                    record)
        passed, chain_valid = report["passed"], report["chain_valid"]
    except TypeError:
        passed, chain_valid = False, False
    try:
        swap_count = len(swap_history(plan))
    except ValueError:
        swap_count = 0
    return {"passed": bool(passed), "chain_valid": bool(chain_valid),
            "swap_count": swap_count,
            "items": [(i.get("entity") or {}).get("name")
                      for i in plan.get("items") or []]}


def explore_branches(plan: dict, branches: list) -> dict:
    """Fork a base plan into named swap branches and compare them.

    Returns ``{base, branches}``: ``base`` is the base plan's
    recomputed verdict summary (no plan body); each branch entry is
    ``{name, ok, swaps_applied, passed, chain_valid, swap_count,
    items, plan}`` on success — ``plan`` being the branch's own
    re-sealed plan, stamped with a reader-facing ``branch``
    provenance field — or ``{name, ok: False, error}`` with the
    categorised refusal when that path does not apply. One
    branch's failure never affects another branch, and the base
    plan is never mutated. Spec-shape problems raise ValueError
    via validate_branches (a caller error, not a branch result)."""
    validate_branches(branches)
    try:
        _verify_base(plan)
        base = _branch_verdict(plan)
    except ValueError:
        base = {"passed": False,
                "chain_valid": bool(isinstance(plan, dict)
                                    and verify_chain(
                                        plan.get("evidence") or {})),
                "swap_count": 0,
                "items": [(i.get("entity") or {}).get("name")
                          for i in (plan.get("items") or [])
                          if isinstance(i, dict)]
                if isinstance(plan, dict) else []}
    results = []
    for branch in branches:
        name, swaps = branch["name"], branch["swaps"]
        try:
            forked = apply_swap_path(plan, swaps)
        except ValueError as exc:  # categorised refusal, per-branch
            results.append({"name": name, "ok": False,
                            "error": str(exc)})
            continue
        except (KeyError, TypeError, AttributeError, IndexError):
            results.append({"name": name, "ok": False,
                            "error": "bad_plan"})
            continue
        forked["branch"] = {
            "name": name,
            "swaps_applied": len(swaps),
            "base_swap_count": base["swap_count"]}
        results.append({"name": name, "ok": True,
                        "swaps_applied": len(swaps),
                        **_branch_verdict(forked), "plan": forked})
    return {"base": base, "branches": results}
