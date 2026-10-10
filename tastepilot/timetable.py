"""Timetable providers for inter-leg feasibility (offline, pluggable).

Run 14's feasibility check (see ``travel``) used one source only:
offline door-to-door *estimates* derived from the great-circle
distance between leg cities. This module puts that check behind a
provider interface, so a *scheduled* service — a real flight or train
duration the user supplies — can replace the estimate for the pairs
it covers, behind exactly the same feasibility interface:

- a provider is any object with a ``name`` and
  ``lookup(from_city, to_city)`` returning a transfer dict
  (``{"mode", "travel_hours", "distance_km", "source", ...}``) or
  ``None`` when it has nothing for that pair;
- ``OfflineEstimateProvider`` wraps ``travel.estimate_transfer`` —
  the original behaviour, provider name ``offline_estimate``;
- ``FileTimetableProvider`` serves scheduled durations parsed from a
  timetable *file* the user hands over (a JSON or CSV export of the
  services they are actually considering — parsed entirely offline,
  no timetable API, no account, no network, no keys);
- ``ChainedTimetableProvider`` tries providers in order and falls
  back to the next one, so the normal composition is
  file-then-offline: scheduled where the file covers the pair,
  estimated where it does not. A transfer records which source
  produced it (``source``: ``timetable_file`` / ``offline_estimate``)
  so a plan never presents an estimate as a schedule or vice versa.

Timetable files mirror the taste-import rails in ``imports``: capped
at 1 MiB and 500 entries, durations must be positive and at most
72 h, city names 2–60 chars, and a malformed file raises
``ValueError`` with a category (``too_large``, ``undecodable``,
``bad_json``, ``no_items``, ``unsupported``, ``unreadable``) — never
a traceback and never an echo of the file's contents. Only the
file's basename is ever recorded, never its path. A scheduled lookup
is directional in the file but is also tried in reverse: trains and
flights on a city pair normally run both ways at the same duration,
and the reversed hit is flagged (``reversed: true``) rather than
hidden.

Like dates and estimates, scheduled durations are composition
constraints only: they are never sent to Qloo and never enter the
evidence log.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from .travel import estimate_transfer, haversine_km

MAX_TIMETABLE_BYTES = 1_048_576  # 1 MiB — service lists are small
MAX_TIMETABLE_ENTRIES = 500
MIN_CITY_CHARS = 2
MAX_CITY_CHARS = 60
MAX_DURATION_HOURS = 72.0

_LIST_KEYS = ("services", "transfers", "entries", "routes", "schedules",
              "legs")
_FROM_KEYS = ("from", "origin", "departure_city", "departure")
_TO_KEYS = ("to", "destination", "arrival_city", "arrival")
_SERVICE_KEYS = ("service", "operator", "carrier", "name", "flight",
                 "train")
_MODE_KEYS = ("mode", "type", "transport")
_CSV_FROM_HEADERS = {"from", "origin", "departure_city", "departure"}
_CSV_TO_HEADERS = {"to", "destination", "arrival_city", "arrival"}
_CSV_SERVICE_HEADERS = {"service", "operator", "carrier", "name"}


def _clean_city(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip().strip('"')
    if not MIN_CITY_CHARS <= len(text) <= MAX_CITY_CHARS:
        return None
    return text


def _duration_hours(row: dict) -> float | None:
    """Scheduled duration from a row, in hours: an explicit
    ``duration_hours`` / ``travel_hours`` wins, then
    ``duration_minutes``. Must be positive and sane; anything else
    makes the row unusable (it is skipped, never guessed)."""
    low = {str(k).strip().lower(): v for k, v in row.items()}
    hours = None
    for key in ("duration_hours", "travel_hours", "hours"):
        if isinstance(low.get(key), (int, float)):
            hours = float(low[key])
            break
        if isinstance(low.get(key), str):
            try:
                hours = float(low[key])
                break
            except ValueError:
                continue
    if hours is None and isinstance(low.get("duration_minutes"), (int, float)):
        hours = float(low["duration_minutes"]) / 60.0
    if hours is None and isinstance(low.get("duration_minutes"), str):
        try:
            hours = float(low["duration_minutes"]) / 60.0
        except ValueError:
            hours = None
    if hours is None or not 0 < hours <= MAX_DURATION_HOURS:
        return None
    return round(hours, 2)


def _entry_of(row) -> dict | None:
    if not isinstance(row, dict):
        return None
    low = {str(k).strip().lower(): v for k, v in row.items()}
    from_city = next((_clean_city(low[k]) for k in _FROM_KEYS
                      if _clean_city(low.get(k))), None)
    to_city = next((_clean_city(low[k]) for k in _TO_KEYS
                    if _clean_city(low.get(k))), None)
    hours = _duration_hours(row)
    if not from_city or not to_city or hours is None:
        return None
    mode = next((str(low[k]).strip().lower() for k in _MODE_KEYS
                 if isinstance(low.get(k), str) and str(low[k]).strip()),
                "scheduled")
    service = next((str(low[k]).strip() for k in _SERVICE_KEYS
                    if isinstance(low.get(k), str) and str(low[k]).strip()),
                   None)
    return {"from": from_city, "to": to_city, "travel_hours": hours,
            "mode": mode, "service": service}


def _rows_from_json(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
        if _entry_of(data):
            return [data]
    return []


def _parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("bad_json: the file is not valid JSON")
    return _entries_from_rows(_rows_from_json(data))


def _entries_from_rows(rows: list) -> list[dict]:
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        entry = _entry_of(row)
        if entry is None:
            continue
        key = (entry["from"].lower(), entry["to"].lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
        if len(entries) >= MAX_TIMETABLE_ENTRIES:
            break
    return entries


def _parse_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    try:
        rows = [r for r in reader if any(c.strip() for c in r)]
    except csv.Error:
        raise ValueError("unsupported: the file is not readable as CSV")
    if not rows:
        raise ValueError("no_items: the file contains no rows")
    header = [c.strip().lower() for c in rows[0]]
    from_idx = next((i for i, h in enumerate(header)
                     if h in _CSV_FROM_HEADERS), None)
    to_idx = next((i for i, h in enumerate(header)
                   if h in _CSV_TO_HEADERS), None)
    hours_idx = next((i for i, h in enumerate(header)
                      if h in ("duration_hours", "travel_hours", "hours")),
                     None)
    minutes_idx = next((i for i, h in enumerate(header)
                        if h == "duration_minutes"), None)
    if from_idx is None or to_idx is None or (
            hours_idx is None and minutes_idx is None):
        raise ValueError("unsupported: CSV needs from/to and a duration "
                         "column (duration_hours or duration_minutes)")
    mode_idx = next((i for i, h in enumerate(header)
                     if h in ("mode", "type", "transport")), None)
    service_idx = next((i for i, h in enumerate(header)
                        if h in _CSV_SERVICE_HEADERS), None)

    def cell(row, idx):
        return row[idx] if idx is not None and idx < len(row) else None

    raw = []
    for row in rows[1:]:
        raw_row = {"from": cell(row, from_idx), "to": cell(row, to_idx),
                   "mode": cell(row, mode_idx),
                   "service": cell(row, service_idx)}
        if hours_idx is not None:
            raw_row["duration_hours"] = cell(row, hours_idx)
        if minutes_idx is not None:
            raw_row["duration_minutes"] = cell(row, minutes_idx)
        raw.append(raw_row)
    return _entries_from_rows(raw)


def parse_timetable_bytes(data: bytes, filename: str = "") -> dict:
    """Parse one timetable file into provenance + scheduled entries.

    Returns {"source_file": basename, "format": "timetable_json" /
    "timetable_csv", "entries": [{"from", "to", "travel_hours",
    "mode", "service"}]}. Raises ValueError with a category prefix
    on any malformed input."""
    if len(data) > MAX_TIMETABLE_BYTES:
        raise ValueError(f"too_large: timetable file exceeds "
                         f"{MAX_TIMETABLE_BYTES} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("undecodable: timetable file is not UTF-8 text")
    suffix = Path(filename).suffix.lower()
    stripped = text.lstrip()
    if suffix == ".json" or (suffix != ".csv" and stripped[:1] in "{["):
        entries = _parse_json(text)
        fmt = "timetable_json"
    elif suffix == ".csv" or "," in (stripped.splitlines() or [""])[0]:
        entries = _parse_csv(text)
        fmt = "timetable_csv"
    else:
        raise ValueError("unsupported: timetable files are JSON or CSV")
    if not entries:
        raise ValueError("no_items: no usable services in the "
                         "timetable file")
    return {"source_file": Path(filename).name or "timetable",
            "format": fmt, "entries": entries}


def load_timetable(path) -> dict:
    """Read *path* from disk and parse it (see parse_timetable_bytes)."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        raise ValueError(f"unreadable: cannot read timetable file {p.name}")
    return parse_timetable_bytes(data, filename=p.name)


class OfflineEstimateProvider:
    """The original feasibility source: offline door-to-door
    estimates from the great-circle distance (see ``travel``)."""
    name = "offline_estimate"

    def lookup(self, from_city: str, to_city: str) -> dict | None:
        est = estimate_transfer(from_city, to_city)
        if est["travel_hours"] is None:
            return None
        return {**est, "source": self.name, "service": None}


class FileTimetableProvider:
    """Scheduled durations from one or more parsed timetable files.

    A hit is exact on the (from, to) pair, compared case-insensitively;
    when only the reverse direction is listed it is used and flagged
    ``reversed`` — the service runs both ways, the file just named one.
    """
    name = "timetable_file"

    def __init__(self, timetables: list[dict]):
        self._by_pair: dict[tuple[str, str], dict] = {}
        for parsed in timetables or []:
            for entry in parsed.get("entries", []):
                key = (entry["from"].lower(), entry["to"].lower())
                self._by_pair.setdefault(key, entry)

    def lookup(self, from_city: str, to_city: str) -> dict | None:
        entry = self._by_pair.get((from_city.lower(), to_city.lower()))
        reversed_hit = False
        if entry is None:
            entry = self._by_pair.get((to_city.lower(), from_city.lower()))
            reversed_hit = entry is not None
        if entry is None:
            return None
        distance = haversine_km(from_city, to_city)
        result = {"from": from_city, "to": to_city,
                  "distance_km": round(distance) if distance else None,
                  "mode": entry["mode"],
                  "travel_hours": entry["travel_hours"],
                  "source": self.name, "service": entry.get("service")}
        if reversed_hit:
            result["reversed"] = True
        return result


class ChainedTimetableProvider:
    """Try providers in order; the first hit for a pair wins."""

    def __init__(self, providers: list):
        self.providers = list(providers)
        self.name = "+".join(p.name for p in self.providers)

    def lookup(self, from_city: str, to_city: str) -> dict | None:
        for provider in self.providers:
            hit = provider.lookup(from_city, to_city)
            if hit is not None:
                return hit
        return None


def provider_for(timetables: list[dict] | None):
    """The provider a plan should use: file-then-offline when
    timetable files were supplied, plain offline otherwise (which
    keeps the default plan byte-identical in behaviour)."""
    if timetables:
        return ChainedTimetableProvider(
            [FileTimetableProvider(timetables), OfflineEstimateProvider()])
    return OfflineEstimateProvider()
