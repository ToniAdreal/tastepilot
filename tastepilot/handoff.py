"""Native handoffs: one-tap calendar / maps links for a plan.

Until now a plan left the app only as a file (the run-20 exports) or
a share link. This module builds the *handoff links* that open the
user's own calendar and maps apps directly: a Google Calendar
"create event" template URL per dated leg, and a Google Maps search
URL per leg and per recommendation.

The handoff doctrine matches the rest of the project:

- **No account, no credential, no network.** A handoff is just a
  URL. TastePilot never signs in anywhere, sends nothing, and holds
  no token — the user's own browser/app completes the action under
  the user's own account when (and only when) they tap the link.
- **No invented dates.** A leg with no start date gets no calendar
  link (``calendar_url`` is None) — the same refusal the iCalendar
  exporter makes, per leg instead of for the whole plan. Maps links
  need no dates and are always present.
- **Recomputed, never trusted.** Links are derived deterministically
  from the plan's schedule and items on every call. A ``handoffs``
  field stored inside a plan JSON is never read back — exports and
  the UI render links recomputed from the plan itself, so a tampered
  stored field cannot smuggle a link into an export.
- **Bounded.** Calendar details list at most the leg's items and
  the URL is capped (details truncated with a "+N more" note), so a
  large plan never produces an unusable link.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote_plus

_MAPS_BASE = "https://www.google.com/maps/search/?api=1&query="
_CAL_BASE = "https://calendar.google.com/calendar/render"
_MAX_URL = 2000  # stay well under common browser/app URL limits


def maps_url(query: str) -> str:
    """A Google Maps search URL for a free-text place query."""
    return _MAPS_BASE + quote_plus(" ".join(str(query).split()))


def _maps_query(name: str, location) -> str:
    """Search query for one recommendation: its name, qualified by
    its leg/entity location when that adds information."""
    name = " ".join(str(name or "").split())
    loc = " ".join(str(location or "").split())
    if loc and loc.lower() not in name.lower():
        return f"{name}, {loc}"
    return name


def leg_calendar_url(location, start, end, item_names,
                     request_text=None) -> str | None:
    """Google Calendar template URL for one all-day leg event, or
    None when the leg has no start date (never invent one).

    Dates follow the template convention for all-day events: the
    end date is exclusive, so it is the leg's end + 1 day (or the
    start + 1 day for a single-day leg)."""
    if not start:
        return None
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end or start) + timedelta(days=1)
    title = f"TastePilot: {location or 'Trip leg'}"
    details_lines = []
    if request_text:
        details_lines.append(str(request_text))
    names = [str(n) for n in item_names or []]
    if names:
        details_lines.append("Recommendations:")
        details_lines += [f"- {n}" for n in names]
    details = "\n".join(details_lines)

    def build(details_text: str) -> str:
        return (_CAL_BASE + "?action=TEMPLATE"
                + "&text=" + quote_plus(title)
                + "&dates=" + start_d.strftime("%Y%m%d")
                + "/" + end_d.strftime("%Y%m%d")
                + "&details=" + quote_plus(details_text))

    url = build(details)
    # Truncate the item list (never the URL structure) to fit the cap.
    while len(url) > _MAX_URL and names:
        names = names[:-1]
        kept = [str(n) for n in (item_names or [])[:len(names)]]
        omitted = len(item_names or []) - len(kept)
        lines = ([str(request_text)] if request_text else [])
        if kept:
            lines.append("Recommendations:")
            lines += [f"- {n}" for n in kept]
        if omitted:
            lines.append(f"(+{omitted} more in the TastePilot plan)")
        url = build("\n".join(lines))
    if len(url) > _MAX_URL:  # pathological request text: drop details
        url = build("")
    return url


def plan_handoffs(plan: dict) -> dict:
    """All handoff links for a plan, recomputed from its schedule
    and items: ``{legs: [{location, start, end, calendar_url,
    maps_url}], items: [{name, domain, location, maps_url}]}``.

    Legs come from the plan's schedule (falling back to its single
    location); an item's maps query uses the item's own entity
    location when present, else its leg location."""
    if not isinstance(plan, dict):
        return {"legs": [], "items": []}
    items = [i for i in plan.get("items") or [] if isinstance(i, dict)]
    schedule = plan.get("schedule") or []
    if not schedule and plan.get("location"):
        schedule = [{"location": plan.get("location"), "start": None,
                     "end": None}]
    legs = []
    for entry in schedule:
        if not isinstance(entry, dict):
            continue
        location = entry.get("location")
        leg_items = [i for i in items
                     if (i.get("location") or None) == (location or None)]
        if not leg_items and len(schedule) == 1:
            leg_items = items
        legs.append({
            "location": location,
            "start": entry.get("start"),
            "end": entry.get("end"),
            "calendar_url": leg_calendar_url(
                location, entry.get("start"), entry.get("end"),
                [i.get("entity", {}).get("name", "") for i in leg_items],
                request_text=plan.get("request")),
            "maps_url": maps_url(location) if location else None,
        })
    item_links = []
    for item in items:
        entity = item.get("entity") or {}
        name = entity.get("name", "")
        location = entity.get("location") or item.get("location")
        item_links.append({
            "name": name,
            "domain": item.get("domain"),
            "location": item.get("location"),
            "maps_url": maps_url(_maps_query(name, location)),
        })
    return {"legs": legs, "items": item_links}
