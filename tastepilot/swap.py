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

Guided suggestions live here too. ``suggest_branches`` ranks
the swaps a plan actually offers — every primary/alternative
pair sharing a domain and a leg, both checker-verified against
the sealed record first — by a deterministic, documented
formula (affinity delta, the alternative's own affinity, and
taste coverage: tags the alternative would introduce that no
primary item carries), and returns them as branch specs that
``explore_branches`` accepts unchanged, plus a combined
"guided tour" branch picking the best swap in each domain.
A suggestion proposes; it never applies — no plan is mutated,
nothing is re-sealed, and a tampered plan is refused with the
same categories as a swap instead of being suggested from.

Learned ranking lives here too. ``learn_preferences`` derives a
deterministic preference profile from a user's own past swap
choices — the sealed swap events of the base plan itself plus
any history plans the caller supplies. A swap the user kept is
a positive example (its chosen alternative's tags and domain
gain weight); a swap the user later undid is a negative one
(the same tags and domain lose the same weight). Activity is
computed from the undo chain alone — an event is in effect
unless an in-effect undo reverses it, and undoing an undo
restores it — and history is read only through swap_history,
so a history plan whose chain does not verify contributes
nothing and is counted as ignored rather than believed.
``suggest_branches`` folds the profile into its fixed formula
as a bounded learned adjustment per candidate — the learning
is inspectable (the profile and every candidate's adjustment
are returned), capped, and exactly zero when there is no
history, so unpractised plans rank exactly as before.

Persistent profiles live here too, as pure functions:
``validate_preference_profile`` shape-gates a stored profile
(the learn_preferences output shape, weights within the caps)
and ``merge_profiles`` combines two profiles additively under
the same caps — the arithmetic the persistent store
(profiles.ProfileStore) uses to carry a profile across plans
and sessions without ever trusting caller-supplied weights:
a stored profile is only ever the merge of profiles derived
from sealed swap histories.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import checker
from .evidence import EvidenceLog, verify_chain
from .models import tag_names


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


MAX_SUGGESTIONS = MAX_BRANCHES  # a suggestion list is a branch spec
COVERAGE_BONUS = 0.05           # score points per newly covered tag

MAX_HISTORY_PLANS = 4           # extra plans learned from per suggest
LEARN_TAG_STEP = 0.04           # weight per kept (or undone) swap tag
LEARN_DOMAIN_STEP = 0.02        # weight per kept (or undone) swap domain
LEARN_TAG_CAP = 0.12            # |tag weight| ceiling (3 net swaps)
LEARN_DOMAIN_CAP = 0.06         # |domain weight| ceiling


def _root_event(events: list[dict], event: dict) -> dict:
    """The non-undo event at the root of an event's undo chain."""
    by_id = {e.get("swap_id"): e for e in events}
    seen = set()
    current = event
    while current.get("undo_of") and current["undo_of"] in by_id \
            and current["undo_of"] not in seen:
        seen.add(current["undo_of"])
        current = by_id[current["undo_of"]]
    return current


def learn_preferences(plans: list) -> dict:
    """Derive a preference profile from plans' sealed swap history.

    Each plan is read through swap_history — a plan whose chain
    does not verify (or that carries no evidence) is counted in
    ``plans_ignored`` and contributes nothing. For every non-undo
    swap event, the event is *kept* when its exchange is still in
    effect (no in-effect undo reverses it; undoing an undo
    restores it) and *undone* otherwise. A kept swap adds
    LEARN_TAG_STEP to every tag of the alternative the user chose
    (looked up in that plan's own items/alternatives — the sealed
    description carries no tags) and LEARN_DOMAIN_STEP to its
    domain; an undone swap subtracts the same. Weights are
    clamped to the LEARN_*_CAP ceilings, zero weights are dropped,
    and both weight maps are returned with sorted keys — the
    profile is a pure, deterministic function of the supplied
    plans' sealed records."""
    tag_weights: dict[str, float] = {}
    domain_weights: dict[str, float] = {}
    kept = undone = used = ignored = 0
    for plan in plans:
        if not isinstance(plan, dict):
            ignored += 1
            continue
        try:
            history = swap_history(plan)
        except ValueError:
            ignored += 1
            continue
        used += 1
        events = [h for h in history]
        in_effect: dict = {}
        for entry in events:
            if entry.get("is_undo"):
                root = _root_event(events, entry)
                rid = root.get("swap_id")
                in_effect[rid] = not in_effect.get(rid, True)
            else:
                in_effect.setdefault(entry.get("swap_id"), True)
        pool = [i for i in (plan.get("items") or [])
                + (plan.get("alternatives") or []) if isinstance(i, dict)]
        for entry in events:
            if entry.get("is_undo"):
                continue
            was_kept = in_effect.get(entry.get("swap_id"), True)
            sign = 1 if was_kept else -1
            if was_kept:
                kept += 1
            else:
                undone += 1
            chosen = entry.get("alternative") or {}
            tags: set = set()
            if chosen:
                hits = [i for i in pool
                        if _matches(i, _selector_from(chosen))]
                if len(hits) == 1:
                    tags = _tags_of(hits[0])
            for tag in tags:
                tag_weights[tag] = tag_weights.get(tag, 0.0) \
                    + sign * LEARN_TAG_STEP
            domain = chosen.get("domain")
            if domain:
                domain_weights[domain] = domain_weights.get(domain, 0.0) \
                    + sign * LEARN_DOMAIN_STEP

    def _clamp(weights: dict, cap: float) -> dict:
        return {k: round(max(-cap, min(cap, v)), 6)
                for k, v in sorted(weights.items())
                if round(max(-cap, min(cap, v)), 6) != 0}

    return {"plans_used": used, "plans_ignored": ignored,
            "kept_swaps": kept, "undone_swaps": undone,
            "tag_weights": _clamp(tag_weights, LEARN_TAG_CAP),
            "domain_weights": _clamp(domain_weights, LEARN_DOMAIN_CAP)}


def empty_profile() -> dict:
    """A zero preference profile, in the learn_preferences shape."""
    return {"plans_used": 0, "plans_ignored": 0, "kept_swaps": 0,
            "undone_swaps": 0, "tag_weights": {}, "domain_weights": {}}


def validate_preference_profile(profile) -> dict:
    """Shape-gate a stored preference profile and return a
    normalised copy (weights clamped to the LEARN_* caps, zero
    weights dropped, keys sorted, counts coerced to non-negative
    ints). Raises ValueError with a categorised message —
    bad_profile / bad_profile_weights / bad_profile_counts —
    on any malformed shape. This is the only way a stored
    profile may enter a ranking: arbitrary caller weights are
    never believed beyond the caps the learner itself obeys."""
    if not isinstance(profile, dict):
        raise ValueError("bad_profile")
    normalised = empty_profile()
    for key in ("tag_weights", "domain_weights"):
        weights = profile.get(key, {})
        if not isinstance(weights, dict) or any(
                not isinstance(k, str) or not k
                or isinstance(v, bool)
                or not isinstance(v, (int, float))
                for k, v in weights.items()):
            raise ValueError("bad_profile_weights")
        cap = LEARN_TAG_CAP if key == "tag_weights" else LEARN_DOMAIN_CAP
        cleaned = {}
        for name, value in weights.items():
            weight = round(max(-cap, min(cap, float(value))), 6)
            if weight != 0:
                cleaned[name] = weight
        normalised[key] = dict(sorted(cleaned.items()))
    for key in ("plans_used", "plans_ignored", "kept_swaps",
                "undone_swaps"):
        value = profile.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) \
                or value < 0:
            raise ValueError("bad_profile_counts")
        normalised[key] = value
    return normalised


def merge_profiles(*profiles: dict) -> dict:
    """Combine preference profiles additively under the caps.

    Tag and domain weights sum and re-clamp to the LEARN_* caps
    (so accumulating the same lesson across many sessions can
    never outgrow what one session's learner could produce at
    the cap), zero weights drop, keys return sorted, and the
    kept/undone/used/ignored counts sum. Every input is run
    through validate_preference_profile first — a malformed
    profile raises instead of merging."""
    tag_weights: dict[str, float] = {}
    domain_weights: dict[str, float] = {}
    merged = empty_profile()
    for profile in profiles:
        clean = validate_preference_profile(profile)
        for key in ("plans_used", "plans_ignored", "kept_swaps",
                    "undone_swaps"):
            merged[key] += clean[key]
        for name, weight in clean["tag_weights"].items():
            tag_weights[name] = tag_weights.get(name, 0.0) + weight
        for name, weight in clean["domain_weights"].items():
            domain_weights[name] = domain_weights.get(name, 0.0) + weight

    def _clamp(weights: dict, cap: float) -> dict:
        return {k: round(max(-cap, min(cap, v)), 6)
                for k, v in sorted(weights.items())
                if round(max(-cap, min(cap, v)), 6) != 0}

    merged["tag_weights"] = _clamp(tag_weights, LEARN_TAG_CAP)
    merged["domain_weights"] = _clamp(domain_weights, LEARN_DOMAIN_CAP)
    return merged


def _learned_adjustment(alternative: dict, domain,
                        profile: dict) -> tuple[float, list]:
    """The learned score adjustment for one candidate: the sum of
    the profile's weights over the alternative's tags, plus its
    domain weight. Returns (adjustment, matched_tags sorted)."""
    tags = _tags_of(alternative)
    matched = sorted(t for t in tags
                     if profile["tag_weights"].get(t))
    adjustment = sum(profile["tag_weights"][t] for t in matched) \
        + profile["domain_weights"].get(domain, 0.0)
    return round(adjustment, 6), matched


def _tags_of(item: dict) -> set:
    """Every taste tag an item carries: its entity tags plus the
    tags it shares with the seeds that produced it."""
    entity = item.get("entity") or {}
    tags = set(tag_names(entity.get("tags") or []))
    tags.update(tag_names(item.get("shared_tags") or []))
    return tags


def _selector_of(item: dict) -> dict:
    return _selector_from(_describe(item))


def _branch_name(text: str, seen: set) -> str:
    """A unique branch name of at most MAX_BRANCH_NAME chars."""
    name = text.strip()[:MAX_BRANCH_NAME].strip() or "Suggested swap"
    if name not in seen:
        seen.add(name)
        return name
    for n in range(2, 100):
        suffix = f" ({n})"
        candidate = name[:MAX_BRANCH_NAME - len(suffix)] + suffix
        if candidate not in seen:
            seen.add(candidate)
            return candidate
    raise ValueError("bad_branch_name")  # pragma: no cover


def _candidate(primary: dict, alternative: dict,
               plan_tags: set, profile: dict | None = None) -> dict:
    """Ranking metrics for one swappable pair.

    score = alternative_affinity + affinity_delta
            + COVERAGE_BONUS * coverage_gain + learned_adjustment,
    where the delta is alternative minus primary (negative is a
    loss the score charges in full), coverage_gain counts the
    tags the alternative carries that no primary item in the plan
    carries — the taste coverage the swap would add — and the
    learned adjustment (learn_preferences over the user's past
    swap choices) is exactly zero when there is no history."""
    profile = profile or {"tag_weights": {}, "domain_weights": {}}
    p_aff = float(primary.get("affinity") or 0)
    a_aff = float(alternative.get("affinity") or 0)
    delta = round(a_aff - p_aff, 6)
    new_tags = sorted(_tags_of(alternative) - plan_tags)
    adjustment, learned_tags = _learned_adjustment(
        alternative, primary.get("domain"), profile)
    score = round(a_aff + delta + COVERAGE_BONUS * len(new_tags)
                  + adjustment, 6)
    return {
        "primary": (primary.get("entity") or {}).get("name"),
        "alternative": (alternative.get("entity") or {}).get("name"),
        "domain": primary.get("domain"),
        "location": primary.get("location"),
        "primary_affinity": p_aff,
        "alternative_affinity": a_aff,
        "affinity_delta": delta,
        "coverage_gain": len(new_tags),
        "new_tags": new_tags,
        "learned_adjustment": adjustment,
        "learned_tags": learned_tags,
        "score": score,
        "primary_selector": _selector_of(primary),
        "alternative_selector": _selector_of(alternative),
    }


def suggest_branches(plan: dict, limit: int = 5,
                     history_plans: list | None = None,
                     stored_profile: dict | None = None) -> dict:
    """Rank a plan's possible swaps and propose branch specs.

    Returns ``{base, candidates, suggestions, branches,
    learning}``: ``candidates`` is every admissible
    primary/alternative pair sharing a domain and a leg, ranked
    by score (ties broken by affinity delta, alternative
    affinity, then names — fully deterministic); ``suggestions``
    is the top single-swap branches (at most ``limit``, reserving
    one slot for the combined branch when it exists), each with
    its score, delta, coverage, new tags, the learned adjustment
    folded into its score, and a plain-language rationale; the
    combined "Guided tour" branch — the best-scoring swap in
    each (domain, leg) group, applied together — is appended
    when at least two groups offer a swap. ``branches`` is the
    suggestions as a plain ``[{name, swaps}]`` spec, directly
    acceptable to explore_branches / the branches CLI.
    ``learning`` is the learn_preferences profile the ranking
    used: the base plan's own sealed swap history plus up to
    MAX_HISTORY_PLANS caller-supplied history plans, merged
    (merge_profiles, same caps) with ``stored_profile`` when
    one is supplied — a persistent profile carried across
    sessions by profiles.ProfileStore, itself only ever
    derived from sealed histories. The returned profile names
    the fact via ``stored_applied``; with no stored profile
    every score is byte-identical to the per-request ranking.

    The plan is verified first (_verify_base: chain + whole-plan
    checker) and both items of every pair are re-checked
    individually — suggestions are derived only from verified
    recommendations. Nothing is applied and the plan is never
    mutated. Raises ValueError with a categorised message
    (bad_limit / bad_history / too_many_history_plans /
    bad_profile* / _verify_base's categories) on refusal; a plan
    with no alternatives simply yields empty lists."""
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not 1 <= limit <= MAX_SUGGESTIONS:
        raise ValueError("bad_limit")
    if history_plans is not None:
        if not isinstance(history_plans, list) \
                or any(not isinstance(p, dict) for p in history_plans):
            raise ValueError("bad_history")
        if len(history_plans) > MAX_HISTORY_PLANS:
            raise ValueError("too_many_history_plans")
    if stored_profile is not None:
        stored_profile = validate_preference_profile(stored_profile)
    _verify_base(plan)
    profile = learn_preferences([plan] + list(history_plans or []))
    if stored_profile is not None:
        profile = merge_profiles(profile, stored_profile)
    profile["stored_applied"] = stored_profile is not None
    items = plan.get("items") or []
    alternatives = plan.get("alternatives") or []
    record = plan.get("evidence") or {}
    admissible = {(r["entity_id"], r["item"],
                   (r["evidence"] or {}).get("response_id"))
                  for r in checker.item_reports(
                      checker.plan_recommendations(plan), record)
                  if r["admissible"]}

    def is_admissible(item) -> bool:
        return ((item.get("entity") or {}).get("id"),
                (item.get("entity") or {}).get("name"),
                item.get("evidence_ref")) in admissible

    plan_tags: set = set()
    for item in items:
        plan_tags.update(_tags_of(item))
    candidates = []
    for alternative in alternatives:
        if not is_admissible(alternative):
            continue
        for primary in items:
            if primary.get("domain") != alternative.get("domain") \
                    or primary.get("location") != alternative.get("location"):
                continue
            if not is_admissible(primary):
                continue
            candidates.append(
                _candidate(primary, alternative, plan_tags, profile))
    candidates.sort(key=lambda c: (-c["score"], -c["affinity_delta"],
                                   -c["alternative_affinity"],
                                   c["primary"] or "",
                                   c["alternative"] or ""))
    # The combined branch: best candidate per (domain, leg).
    best_by_group: dict = {}
    for cand in candidates:  # already ranked: first per group wins
        best_by_group.setdefault((cand["domain"], cand["location"]), cand)
    tour = sorted(best_by_group.values(),
                  key=lambda c: (-c["score"], c["primary"] or ""))
    has_tour = len(tour) >= 2
    single_budget = limit - 1 if has_tour else limit
    seen_names: set = set()
    suggestions = []
    for cand in candidates[:single_budget]:
        full = f"{cand['alternative']} instead of {cand['primary']}"
        # Branch names cap at 60 chars: a long pair falls back to
        # naming the swap-in alone rather than truncating mid-word.
        preferred = full if len(full) <= MAX_BRANCH_NAME \
            else f"Swap in {cand['alternative']}"
        name = _branch_name(preferred, seen_names)
        rationale = (
            f"Swap {cand['primary']} ({cand['primary_affinity']:.2f}) "
            f"for {cand['alternative']} "
            f"({cand['alternative_affinity']:.2f}) in "
            f"{cand['domain']}: affinity delta "
            f"{cand['affinity_delta']:+.2f}"
            + (f", adds taste coverage: {', '.join(cand['new_tags'])}"
               if cand["new_tags"] else
               ", no new taste tags")
            + (f"; learned from your past swaps: "
               f"{cand['learned_adjustment']:+.2f}"
               + (f" (tags you kept choosing: "
                  f"{', '.join(cand['learned_tags'])})"
                  if cand["learned_tags"] else "")
               if cand["learned_adjustment"] else ""))
        suggestions.append({
            "name": name,
            "swaps": [{"primary": cand["primary_selector"],
                       "alternative": cand["alternative_selector"]}],
            "score": cand["score"],
            "affinity_delta": cand["affinity_delta"],
            "coverage_gain": cand["coverage_gain"],
            "new_tags": cand["new_tags"],
            "learned_adjustment": cand["learned_adjustment"],
            "learned_tags": cand["learned_tags"],
            "domain": cand["domain"],
            "location": cand["location"],
            "rationale": rationale,
        })
    if has_tour and len(suggestions) < limit:
        total_delta = round(sum(c["affinity_delta"] for c in tour), 6)
        tour_tags = sorted({t for c in tour for t in c["new_tags"]})
        total_score = round(sum(c["score"] for c in tour), 6)
        name = _branch_name(
            "Guided tour: best swap in each domain", seen_names)
        suggestions.append({
            "name": name,
            "swaps": [{"primary": c["primary_selector"],
                       "alternative": c["alternative_selector"]}
                      for c in tour],
            "score": total_score,
            "affinity_delta": total_delta,
            "coverage_gain": len(tour_tags),
            "new_tags": tour_tags,
            "learned_adjustment": round(
                sum(c["learned_adjustment"] for c in tour), 6),
            "learned_tags": sorted(
                {t for c in tour for t in c["learned_tags"]}),
            "domain": None,
            "location": None,
            "rationale": (
                f"Apply the best-scoring swap in each of "
                f"{len(tour)} domain(s) together "
                f"({', '.join(c['domain'] for c in tour)}): total "
                f"affinity delta {total_delta:+.2f}"
                + (f", adds taste coverage: {', '.join(tour_tags)}"
                   if tour_tags else ", no new taste tags")),
        })
    branches = [{"name": s["name"], "swaps": s["swaps"]}
                for s in suggestions]
    if branches:
        validate_branches(branches)
    return {"base": _branch_verdict(plan),
            "candidates": candidates,
            "suggestions": suggestions,
            "branches": branches,
            "learning": profile}
