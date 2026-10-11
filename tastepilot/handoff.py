"""Native handoffs: one-tap calendar / maps links for a plan.

Until now a plan left the app only as a file (the run-20 exports) or
a share link. This module builds the *handoff links* that open the
user's own calendar and maps apps directly: a Google Calendar
"create event" template URL per dated leg, and a Google Maps search
URL per leg and per recommendation. On top of those web links it
also builds the platform-native forms (run 22): an Apple Maps URL
(the maps app on iOS/macOS) and a ``geo:`` URI (the Android maps
intent) for the same queries, plus a Web Share API payload
(``{title, text}``) for the OS share sheet — the browser presents
the payload to the user's own apps; TastePilot itself shares
nothing. The share text carries a verification line **recomputed**
from the sealed evidence, exactly like the export banner: a
tampered plan is shared as NOT VERIFIED, never clean.

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
  large plan never produces an unusable link. The share payload is
  capped the same way (item lists truncated with a "+N more" note).
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote, quote_plus

_MAPS_BASE = "https://www.google.com/maps/search/?api=1&query="
_APPLE_MAPS_BASE = "https://maps.apple.com/?q="
_CAL_BASE = "https://calendar.google.com/calendar/render"
_MAX_URL = 2000  # stay well under common browser/app URL limits
_MAX_SHARE_TEXT = 1000  # share-sheet payloads stay short and readable


def maps_url(query: str) -> str:
    """A Google Maps search URL for a free-text place query."""
    return _MAPS_BASE + quote_plus(" ".join(str(query).split()))


def apple_maps_url(query: str) -> str:
    """An Apple Maps search URL (opens the native Maps app on
    Apple platforms, the Apple Maps web view elsewhere)."""
    return _APPLE_MAPS_BASE + quote_plus(" ".join(str(query).split()))


def geo_url(query: str) -> str:
    """A ``geo:`` URI for a free-text place query — the Android
    maps intent form (RFC 5870 with a query; no coordinates are
    invented for a place we only know by name)."""
    return "geo:0,0?q=" + quote(" ".join(str(query).split()), safe="")


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


def _share_verdict_line(plan: dict) -> str:
    """The verification line for a share payload, recomputed from
    the plan's sealed evidence (never the plan's stored checker
    field): PASS only when the chain verifies and the deterministic
    checker admits every item."""
    items = plan.get("items") or []
    record = plan.get("evidence")
    if not isinstance(record, dict) or not isinstance(items, list):
        return "Verification: NOT VERIFIED (no sealed evidence record)."
    try:
        from .checker import check_plan
        report = check_plan(items, record)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return "Verification: NOT VERIFIED (evidence check failed)."
    if report["passed"]:
        return (f"Verification: PASS — {len(items)} recommendation(s), "
                f"every one traced to a Qloo response.")
    return "Verification: NOT VERIFIED — this plan fails its evidence check."


def plan_share_payload(plan: dict) -> dict:
    """A Web Share API payload ``{title, text}`` for a plan,
    recomputed from its schedule and items on every call.

    The title names the legs in order; the text carries the request,
    one line per leg (dates when present, then its recommendation
    names), and the recomputed verification line. Bounded: leg item
    lists are truncated with a "+N more" note so the payload never
    exceeds ``_MAX_SHARE_TEXT`` characters."""
    if not isinstance(plan, dict):
        return {"title": "TastePilot itinerary",
                "text": "Verification: NOT VERIFIED "
                        "(no sealed evidence record)."}
    items = [i for i in plan.get("items") or [] if isinstance(i, dict)]
    schedule = plan.get("schedule") or []
    if not schedule and plan.get("location"):
        schedule = [{"location": plan.get("location"), "start": None,
                     "end": None}]
    places = [e.get("location") for e in schedule
              if isinstance(e, dict) and e.get("location")]
    title = ("TastePilot plan: " + " → ".join(places)) if places \
        else "TastePilot itinerary"
    verdict = _share_verdict_line(plan)
    request = " ".join(str(plan.get("request") or "").split())

    def build(keep_per_leg):
        lines = []
        if request:
            lines.append(request)
        for entry in schedule:
            if not isinstance(entry, dict):
                continue
            location = entry.get("location")
            leg_items = [i for i in items
                         if (i.get("location") or None) ==
                         (location or None)]
            if not leg_items and len(schedule) == 1:
                leg_items = items
            names = [str(i.get("entity", {}).get("name", ""))
                     for i in leg_items]
            kept = names if keep_per_leg is None else names[:keep_per_leg]
            head = str(location or "Anywhere")
            if entry.get("start"):
                head += f" ({entry['start']}"
                if entry.get("end"):
                    head += f" – {entry['end']}"
                head += ")"
            line = head + ": " + ", ".join(kept) if kept else head
            omitted = len(names) - len(kept)
            if omitted > 0:
                line += f" (+{omitted} more)"
            lines.append(line)
        if not schedule and items:
            lines.append(", ".join(
                str(i.get("entity", {}).get("name", "")) for i in items))
        lines.append(verdict)
        return "\n".join(lines)

    text = build(None)
    keep = None
    while len(text) > _MAX_SHARE_TEXT and (keep is None or keep > 0):
        keep = 3 if keep is None else keep - 1
        text = build(keep)
    if len(text) > _MAX_SHARE_TEXT:  # pathological: verdict always survives
        text = text[:_MAX_SHARE_TEXT - len(verdict) - 1].rstrip() \
            + "\n" + verdict
    return {"title": title, "text": text}


def plan_handoffs(plan: dict) -> dict:
    """All handoff links for a plan, recomputed from its schedule
    and items: ``{legs: [{location, start, end, calendar_url,
    maps_url, maps_apple_url, maps_geo_url}], items: [{name,
    domain, location, maps_url, maps_apple_url, maps_geo_url}],
    share: {title, text}}``.

    Legs come from the plan's schedule (falling back to its single
    location); an item's maps query uses the item's own entity
    location when present, else its leg location."""
    if not isinstance(plan, dict):
        return {"legs": [], "items": [],
                "share": plan_share_payload(plan)}
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
            "maps_apple_url": apple_maps_url(location)
            if location else None,
            "maps_geo_url": geo_url(location) if location else None,
        })
    item_links = []
    for item in items:
        entity = item.get("entity") or {}
        name = entity.get("name", "")
        location = entity.get("location") or item.get("location")
        query = _maps_query(name, location)
        item_links.append({
            "name": name,
            "domain": item.get("domain"),
            "location": item.get("location"),
            "maps_url": maps_url(query),
            "maps_apple_url": apple_maps_url(query),
            "maps_geo_url": geo_url(query),
        })
    return {"legs": legs, "items": item_links,
            "share": plan_share_payload(plan)}
