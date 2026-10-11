"""Day-by-day pacing (pacing.py): a plan's recommendations laid over its days.

Until now a dated leg was a bag of recommendations with a date
range: the schedule said *when* the leg runs, but nothing said
which recommendation belongs on which day. This module paces the
plan: each leg's primary items are distributed across the leg's
days deterministically, so a traveller can read the trip day by
day instead of domain by domain.

The pacing doctrine matches the rest of the project:

- **Presentation only.** Pacing is recomputed from the plan's
  schedule + items on every call. It is never sealed into the
  evidence record, never a checker input, and a ``itinerary``
  field stored inside a plan JSON is never read back — exports
  and the UI render pacing recomputed from the plan itself, so a
  tampered stored field cannot smuggle a day assignment into an
  export.
- **No invented dates.** A leg with no start date is not given
  days: it contributes one ``date: None`` bucket holding all of
  its items, labelled unscheduled. An ``end`` before ``start``
  (a schedule the parser already flags with a date warning) is
  treated the same way — pacing never silently repairs a broken
  range by inventing a different one.
- **Every item exactly once.** Pacing covers the plan's primary
  items only (alternatives are swap options, not scheduled
  stops). Each primary item appears in exactly one day bucket of
  exactly one leg, in the plan's own item order within the leg.
- **Deterministic distribution.** Within a dated leg, items keep
  the plan's order and are split into consecutive chunks as
  evenly as possible across the leg's days (earlier days take
  the remainder, one extra item each). The same plan always
  paces identically.
"""
from __future__ import annotations

from datetime import date, timedelta


def _leg_days(start, end) -> list[str] | None:
    """The ISO dates of a leg, start..end inclusive, or None when
    the leg has no usable range (no start, unparseable dates, or
    end before start)."""
    if not start:
        return None
    try:
        start_d = date.fromisoformat(str(start))
        end_d = date.fromisoformat(str(end)) if end else start_d
    except ValueError:
        return None
    if end_d < start_d:
        return None
    days = []
    current = start_d
    while current <= end_d:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _distribute(items: list, day_count: int) -> list[list]:
    """Split ``items`` into ``day_count`` consecutive chunks as
    evenly as possible; earlier chunks take the remainder."""
    if day_count <= 0:
        return []
    base, extra = divmod(len(items), day_count)
    chunks = []
    pos = 0
    for idx in range(day_count):
        size = base + (1 if idx < extra else 0)
        chunks.append(list(items[pos:pos + size]))
        pos += size
    return chunks


def _item_ref(item: dict) -> dict:
    entity = item.get("entity") or {}
    return {"name": entity.get("name", ""),
            "domain": item.get("domain"),
            "location": item.get("location"),
            "affinity": item.get("affinity")}


def plan_itinerary(plan: dict) -> dict:
    """The day-by-day pacing of a plan, recomputed from its
    schedule and primary items: ``{legs: [{location, start, end,
    days: [{date, items: [{name, domain, location, affinity}]}]}],
    dated_days, total_items}``.

    Legs come from the plan's schedule (falling back to its single
    location, exactly as the handoff layer does). Items are taken
    in plan order per leg; a single-leg plan whose items carry no
    location still paces all of them under that leg."""
    empty = {"legs": [], "dated_days": 0, "total_items": 0}
    if not isinstance(plan, dict):
        return empty
    items = [i for i in plan.get("items") or [] if isinstance(i, dict)]
    schedule = plan.get("schedule") or []
    if not schedule and plan.get("location"):
        schedule = [{"location": plan.get("location"), "start": None,
                     "end": None}]
    if not schedule and items:
        schedule = [{"location": None, "start": None, "end": None}]
    legs = []
    dated_days = 0
    total = 0
    for entry in schedule:
        if not isinstance(entry, dict):
            continue
        location = entry.get("location")
        leg_items = [i for i in items
                     if (i.get("location") or None) == (location or None)]
        if not leg_items and len(schedule) == 1:
            leg_items = list(items)
        days_iso = _leg_days(entry.get("start"), entry.get("end"))
        if days_iso is None:
            days = [{"date": None,
                     "items": [_item_ref(i) for i in leg_items]}] \
                if leg_items else []
        else:
            dated_days += len(days_iso)
            chunks = _distribute(leg_items, len(days_iso))
            days = [{"date": day, "items": [_item_ref(i) for i in chunk]}
                    for day, chunk in zip(days_iso, chunks)]
        total += len(leg_items)
        legs.append({"location": location,
                     "start": entry.get("start"),
                     "end": entry.get("end"),
                     "days": days})
    return {"legs": legs, "dated_days": dated_days, "total_items": total}
