"""Inter-leg travel-time feasibility (deterministic, offline).

The date-aware schedule (see ``parser``) knows *when* each leg of a
multi-city trip runs; this module checks whether the traveller can
physically get between consecutive legs in the time the schedule
leaves for the transfer.

Estimates are deliberately simple and fully offline — no timetable
API, no network, no keys:

- each known city carries coordinates; the great-circle (haversine)
  distance between leg cities drives a door-to-door estimate —
  surface travel (``distance / 75 km/h`` plus a 0.75 h local-transfer
  allowance) under 400 km, otherwise air travel (``distance /
  780 km/h`` plus a flat 3 h airport overhead for getting to/from the
  airport, check-in, and boarding);
- the schedule window for a transfer is the gap between the previous
  leg's end date and the next leg's start date at day granularity:
  a next-day start leaves 24 h, a same-day start leaves 12 h (leave
  in the morning, arrive by the evening), and a start before the
  previous leg ends leaves 0 h;
- a transfer is ``ok`` when the estimate fits within three quarters
  of the window, ``tight`` when it fits but uses more than that, and
  ``infeasible`` when it exceeds the window. ``tight`` and
  ``infeasible`` transfers also surface as ``travel_warnings``
  (``transfer_tight`` / ``transfer_infeasible``) in the same shape as
  the parser's ``date_warnings``: reported, never silently repaired,
  never blocking the plan.

Anything the model cannot judge — a leg without dates, a city with
no coordinates (for example a free-text location), or consecutive
legs in the same city — is reported as status ``unknown`` (or skipped
for same-city pairs) and produces no warning. Transfer estimates are
composition constraints only, exactly like dates: they are never
sent to Qloo and never enter the evidence log.

The estimate source is pluggable: ``assess_transfers`` accepts a
timetable provider (see ``timetable``) whose scheduled durations
replace the estimate for the city pairs it covers. Every transfer
records its ``source`` so a scheduled duration is never presented
as an estimate, or vice versa.
"""
from __future__ import annotations

import math
from datetime import date as _date

# Canonical city name (as produced by parser.extract_locations) ->
# (latitude, longitude). Every city the parser can recognise as a leg
# has coordinates here.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "Kyoto": (35.0116, 135.7681),
    "Osaka": (34.6937, 135.5023),
    "Tokyo": (35.6762, 139.6503),
    "Seoul": (37.5665, 126.9780),
    "New York": (40.7128, -74.0060),
    "Los Angeles": (34.0522, -118.2437),
    "Paris": (48.8566, 2.3522),
    "London": (51.5074, -0.1278),
    "Berlin": (52.5200, 13.4050),
}

_EARTH_KM = 6371.0
_SURFACE_MAX_KM = 400.0
_SURFACE_KMH = 75.0
_SURFACE_OVERHEAD_H = 0.75
_AIR_KMH = 780.0
_AIR_OVERHEAD_H = 3.0
_SAME_DAY_WINDOW_H = 12.0
_TIGHT_FRACTION = 0.75


def haversine_km(a: str, b: str) -> float | None:
    """Great-circle distance between two known cities, else None."""
    if a not in CITY_COORDS or b not in CITY_COORDS:
        return None
    lat1, lon1 = (math.radians(v) for v in CITY_COORDS[a])
    lat2, lon2 = (math.radians(v) for v in CITY_COORDS[b])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_KM * math.asin(math.sqrt(h))


def estimate_transfer(from_city: str, to_city: str) -> dict:
    """Door-to-door transfer estimate between two leg cities:
    {"from", "to", "distance_km", "mode", "travel_hours"} with
    mode "same_city" / "surface" / "flight" / "unknown"."""
    base = {"from": from_city, "to": to_city}
    if from_city == to_city:
        return {**base, "distance_km": 0, "mode": "same_city",
                "travel_hours": 0.0}
    distance = haversine_km(from_city, to_city)
    if distance is None:
        return {**base, "distance_km": None, "mode": "unknown",
                "travel_hours": None}
    if distance < _SURFACE_MAX_KM:
        hours = distance / _SURFACE_KMH + _SURFACE_OVERHEAD_H
        mode = "surface"
    else:
        hours = distance / _AIR_KMH + _AIR_OVERHEAD_H
        mode = "flight"
    return {**base, "distance_km": round(distance),
            "mode": mode, "travel_hours": round(hours, 1)}


def _window_hours(gap_days: int) -> float:
    """Schedule window for a transfer, at the schedule's day
    granularity: next-day (or later) starts leave whole days, a
    same-day start leaves a 12 h within-day window, and a start
    before the previous leg ends leaves nothing."""
    if gap_days > 0:
        return gap_days * 24.0
    if gap_days == 0:
        return _SAME_DAY_WINDOW_H
    return 0.0


def assess_transfers(leg_dates: list[dict],
                     provider=None) -> tuple[list[dict], list[dict]]:
    """Feasibility of every inter-leg transfer in a schedule.

    ``leg_dates`` is the parser's per-leg list ({"location", "start",
    "end", ...} with ISO dates or None). Returns (transfers,
    travel_warnings): one transfer entry per consecutive pair of
    legs in different cities, and one warning per tight or
    infeasible transfer, in the date_warnings shape
    ({"code", "location", "detail"} — location is the destination
    leg).

    ``provider`` is a timetable provider (see ``timetable``): when
    it has a scheduled service for a pair, that duration is judged
    instead of the offline estimate, and the transfer records
    ``source="timetable_file"`` (plus the service name when the file
    names one); pairs the provider does not cover fall back to the
    offline estimate with ``source="offline_estimate"``. With no
    provider, every transfer is the offline estimate — the original
    behaviour. The provider is imported lazily here so ``travel``
    stays importable on its own."""
    if provider is None:
        from .timetable import OfflineEstimateProvider
        provider = OfflineEstimateProvider()
    transfers: list[dict] = []
    warnings: list[dict] = []
    for prev, nxt in zip(leg_dates, leg_dates[1:]):
        from_city, to_city = prev.get("location"), nxt.get("location")
        if not from_city or not to_city or from_city == to_city:
            continue
        est = provider.lookup(from_city, to_city)
        if est is None:
            est = {**estimate_transfer(from_city, to_city),
                   "source": "offline_estimate", "service": None}
        prev_ref = prev.get("end") or prev.get("start")
        next_start = nxt.get("start")
        gap_days = window_hours = None
        if est["travel_hours"] is None:
            status = "unknown"
            detail = (f"no transfer estimate for {from_city} -> "
                      f"{to_city} (unknown city)")
        elif not prev_ref or not next_start:
            status = "unknown"
            detail = (f"cannot judge {from_city} -> {to_city}: "
                      f"a leg has no dates")
        else:
            gap_days = (_date.fromisoformat(next_start)
                        - _date.fromisoformat(prev_ref)).days
            window_hours = _window_hours(gap_days)
            hours = est["travel_hours"]
            if hours > window_hours:
                status = "infeasible"
            elif hours > _TIGHT_FRACTION * window_hours:
                status = "tight"
            else:
                status = "ok"
            if est.get("source") == "timetable_file":
                service = f", {est['service']}" if est.get("service") else ""
                rev = ", listed in reverse" if est.get("reversed") else ""
                detail = (f"{from_city} -> {to_city}: ~{hours} h "
                          f"scheduled ({est['mode']}{service}{rev}) "
                          f"in a {window_hours:g} h window")
            else:
                detail = (f"{from_city} -> {to_city}: ~{hours} h "
                          f"door-to-door ({est['mode']}, "
                          f"~{est['distance_km']} km) in a "
                          f"{window_hours:g} h window")
        transfer = {**est, "gap_days": gap_days,
                    "window_hours": window_hours, "status": status,
                    "detail": detail}
        transfers.append(transfer)
        if status in ("tight", "infeasible"):
            warnings.append({
                "code": f"transfer_{status}",
                "location": to_city,
                "detail": detail})
    return transfers, warnings
