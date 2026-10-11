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
- ``ApiTimetableProvider`` serves the same scheduled-service schema
  fetched over HTTP from a timetable API endpoint the user names
  (``fetch_timetable``: one GET, bounded retries on transient
  failures like the real Qloo client, injectable transport for
  tests). No specific provider account or key is involved — the
  endpoint is user-supplied, is fetched once and then served from
  memory exactly like a file, and only its host is ever recorded as
  provenance: the full URL (whose query string could carry a token)
  never enters a plan, a log line, or an error message;
- ``ChainedTimetableProvider`` tries providers in order and falls
  back to the next one, so the normal composition is
  scheduled-then-offline: scheduled where a file or API covers the
  pair, estimated where it does not. A transfer records which source
  produced it (``source``: ``timetable_file`` / ``timetable_api`` /
  ``offline_estimate``) so a plan never presents an estimate as a
  schedule or vice versa.

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
import time
import urllib.error
import urllib.parse
import urllib.request
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


class _ScheduledTimetableProvider:
    """Scheduled durations from parsed timetables (file or API).

    A hit is exact on the (from, to) pair, compared case-insensitively;
    when only the reverse direction is listed it is used and flagged
    ``reversed`` — the service runs both ways, the source just named
    one. Subclasses differ only in the ``source`` label they stamp.
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


class FileTimetableProvider(_ScheduledTimetableProvider):
    """Scheduled durations from one or more parsed timetable files."""
    name = "timetable_file"


class ApiTimetableProvider(_ScheduledTimetableProvider):
    """Scheduled durations fetched from a timetable API endpoint
    (see ``fetch_timetable``); served from memory after the fetch,
    exactly like a file, but stamped ``timetable_api``."""
    name = "timetable_api"


#: HTTP statuses worth retrying for a timetable fetch: rate limiting
#: and transient server-side failures, mirroring the real Qloo client.
#: Other statuses (400/401/403/404) fail immediately.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


def _api_origin(url: str) -> tuple[urllib.parse.ParseResult, str]:
    """Validate a timetable API URL and derive its provenance label:
    the host (with port when present) and nothing else — no scheme,
    path, userinfo, or query string ever reaches a plan or a log."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("unsupported: timetable URL must be an "
                         "http(s) URL with a host")
    label = parsed.hostname
    if parsed.port:
        label = f"{label}:{parsed.port}"
    return parsed, label


def fetch_timetable(url: str, transport=None, timeout: float = 10.0,
                    max_attempts: int = 3, sleeper=None) -> dict:
    """Fetch one timetable API endpoint and parse it like a file.

    ``transport`` is injectable for tests: a callable
    ``(url, timeout) -> (status, body_bytes)``. The default transport
    is a plain urllib GET with no headers beyond ``accept`` — no key
    or credential is ever sent. Transient failures (HTTP 429/5xx and
    transport-level network errors) are retried up to
    ``max_attempts`` total attempts with exponential backoff through
    the injectable ``sleeper``. Every failure raises ``ValueError``
    with a category (``unsupported`` / ``http_error`` /
    ``network_error``, plus the parse categories of
    ``parse_timetable_bytes``) — never a traceback, and never an echo
    of the URL, its query string, or the response body.

    Returns the ``parse_timetable_bytes`` shape with provenance
    rewritten for an API source: ``source_file`` is the endpoint's
    host label only, ``format`` is ``timetable_api_json`` /
    ``timetable_api_csv``, and ``source_kind`` is ``"api"``.
    """
    parsed_url, origin = _api_origin(url)
    sleep = sleeper or time.sleep
    attempts = max(1, max_attempts)
    filename = Path(urllib.parse.unquote(parsed_url.path)).name \
        or "timetable-api"
    for attempt in range(attempts):
        try:
            if transport is not None:
                status, body = transport(url, timeout)
            else:
                req = urllib.request.Request(
                    url, headers={"accept": "application/json, text/csv"},
                    method="GET")
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        status, body = resp.status, resp.read()
                except urllib.error.HTTPError as exc:  # live path
                    status, body = exc.code, b""
        except (urllib.error.URLError, OSError) as exc:
            # The message names only the exception type — a URLError's
            # own text can embed the full URL, query string included.
            if attempt + 1 < attempts:
                sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            raise ValueError(
                f"network_error: timetable API unreachable "
                f"({type(exc).__name__})") from exc
        if status == 200:
            result = parse_timetable_bytes(body, filename=filename)
            result["source_file"] = origin
            result["format"] = result["format"].replace(
                "timetable_", "timetable_api_", 1)
            result["source_kind"] = "api"
            return result
        if status in TRANSIENT_STATUSES and attempt + 1 < attempts:
            sleep(min(0.5 * (2 ** attempt), 4.0))
            continue
        raise ValueError(f"http_error: timetable API returned "
                         f"HTTP {status}")
    raise ValueError("network_error: timetable API request failed")


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
    """The provider a plan should use: scheduled-then-offline when
    timetables were supplied (each parsed timetable gets its own
    scheduled provider, in supply order — file entries stamped
    ``timetable_file``, API entries ``timetable_api``), plain offline
    otherwise (which keeps the default plan byte-identical in
    behaviour)."""
    if timetables:
        scheduled = [
            ApiTimetableProvider([tt])
            if tt.get("source_kind") == "api"
            or str(tt.get("format", "")).startswith("timetable_api")
            else FileTimetableProvider([tt])
            for tt in timetables]
        return ChainedTimetableProvider(
            [*scheduled, OfflineEstimateProvider()])
    return OfflineEstimateProvider()
