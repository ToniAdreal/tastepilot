"""External taste-file imports (offline, no accounts, no network).

A user can already paste a labelled list into the request (see
``parser.extract_seed_lists``). This module covers the next step:
export *files* the user downloaded from their own taste accounts —
a Spotify playlist export, a Letterboxd watchlist, a Goodreads
library — parsed locally into seeds for the same evidence-checked
loop. Nothing here logs in, opens an account, or calls a network:
the file the user hands over is the whole input.

Supported shapes (detected from the extension and the content):

- ``spotify_json`` — Spotify playlist export JSON:
  ``{"playlists": [{"items": [{"track": {"trackName", "artistName"}}]}]}``
  (the privacy-export shape), a ``{"tracks": [...]}`` object, or a
  bare list of track objects / title strings.
- ``letterboxd_csv`` — Letterboxd CSV exports (watchlist / diary /
  films): the ``Name`` column is the film title.
- ``goodreads_csv`` — Goodreads library CSV: ``Title`` + ``Author``.
- ``generic_csv`` — any CSV whose header names a title column
  (``title``/``name``/``track``/``film``/``song``/``book``) and,
  optionally, a creator column (``artist``/``author``/``director``).
- ``generic_json`` — a JSON list, or an object holding a list under
  ``items``/``films``/``books``/``watchlist``/``library``, of strings
  or ``{name|title, artist|author}`` objects.
- ``text_list`` — one title per line (bullets and numbering are
  stripped; ``#`` lines are comments). Commas are kept, so a title
  containing one survives intact.

Every imported seed carries provenance: the detected format, a
domain hint (music for Spotify, film for Letterboxd, book for
Goodreads, else None), the title, and the creator when the export
names one. The seed *name* handed to Qloo search follows the
catalogue convention — ``"Kind of Blue (Miles Davis)"`` — for music
and books, where the creator disambiguates; films keep the bare
title. Catalogue titles are canonicalised exactly as pasted lists
are, so an imported "Kind of Blue" resolves to the same seed as a
typed one; unknown titles pass verbatim to Qloo search.

The same export schema can also be *fetched* over HTTP from a
taste-feed endpoint the user names (``fetch_import``: one GET,
bounded retries on transient failures like the real Qloo client,
injectable transport for tests) — the generic form of account
sync, with no specific provider account, OAuth flow, or key
involved. The endpoint is fetched once and then parsed exactly
like a file, and only its host is ever recorded as provenance:
the full URL (whose query string could carry a token) never
enters a plan, a log line, or an error message. API-sourced
formats are stamped accordingly (``spotify_api_json``,
``letterboxd_api_csv``, …) and carry ``source_kind: "api"``, so a
fetched list is never presented as a downloaded file, or vice
versa.

Safety rails, in the same spirit as the server caps: the file is
capped at 1 MiB, at most 200 items are imported, and titles are
2–120 characters after cleaning. A malformed file raises
``ValueError`` with a category (``too_large``, ``undecodable``,
``bad_json``, ``no_items``, ``unsupported``) — never a traceback and
never an echo of the file's contents. The plan records only the
file's basename and format, never its path.
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

MAX_IMPORT_BYTES = 1_048_576  # 1 MiB — exports are small; caps abuse
MAX_IMPORT_ITEMS = 200
MIN_TITLE_CHARS = 2
MAX_TITLE_CHARS = 120

_TITLE_KEYS = ("trackname", "title", "name", "track", "film", "song",
               "book", "work")
_CREATOR_KEYS = ("artistname", "artist", "artists", "author",
                 "director", "creator")
_LIST_KEYS = ("playlists", "tracks", "items", "films", "books",
              "watchlist", "library", "entries")
_CSV_TITLE_HEADERS = {"name", "title", "track", "film", "song",
                      "book", "work"}
_CSV_CREATOR_HEADERS = {"artist", "artistname", "author", "director",
                        "creator"}
_BULLET_OR_NUMBER_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip().strip('"\u201c\u201d').strip()
    if not MIN_TITLE_CHARS <= len(text) <= MAX_TITLE_CHARS:
        return None
    return text


def _creator_of(row: dict) -> str | None:
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for key in _CREATOR_KEYS:
        value = low.get(key)
        if isinstance(value, list):  # e.g. Spotify "artists": [{name}]
            names = [_clean(v.get("name") if isinstance(v, dict) else v)
                     for v in value]
            value = next((n for n in names if n), None)
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return None


def _title_of(row) -> str | None:
    if isinstance(row, str):
        return _clean(row)
    if not isinstance(row, dict):
        return None
    # Spotify wraps the track: {"track": {"trackName", "artistName"}}
    nested = row.get("track")
    if isinstance(nested, dict):
        merged = {k: v for k, v in row.items() if k != "track"}
        merged.update(nested)
        return _title_of(merged)
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for key in _TITLE_KEYS:
        cleaned = _clean(low.get(key))
        if cleaned:
            return cleaned
    return None


def _rows_from_json(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                if key == "playlists":
                    rows: list = []
                    for playlist in value:
                        if isinstance(playlist, dict):
                            items = playlist.get("items") or []
                            rows.extend(i for i in items
                                        if isinstance(i, (dict, str)))
                    return rows
                return value
        if _title_of(data):
            return [data]
    return []


def _parse_json(text: str, hint: str | None) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("bad_json: the file is not valid JSON")
    rows = _rows_from_json(data)
    fmt = "spotify_json" if hint == "spotify" or (
        isinstance(data, dict) and "playlists" in data) else "generic_json"
    return _seeds_from_rows(rows, fmt)


def _parse_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text))
    try:
        rows = [r for r in reader if any(c.strip() for c in r)]
    except csv.Error:
        raise ValueError("unsupported: the file is not readable as CSV")
    if not rows:
        raise ValueError("no_items: the file contains no rows")
    header = [c.strip().lower() for c in rows[0]]
    title_idx = next((i for i, h in enumerate(header)
                      if h in _CSV_TITLE_HEADERS), None)
    if title_idx is None:
        raise ValueError("unsupported: no title column in the CSV header")
    creator_idx = next((i for i, h in enumerate(header)
                        if h in _CSV_CREATOR_HEADERS), None)
    header_text = " ".join(header)
    if "letterboxd" in header_text or header[title_idx] == "name" \
            and "year" in header:
        fmt, kind = "letterboxd_csv", "film"
    elif "author" in header and ("bookshelves" in header
                                 or "my rating" in header
                                 or "date read" in header):
        fmt, kind = "goodreads_csv", "book"
    elif "author" in header:
        fmt, kind = "generic_csv", "book"
    else:
        fmt, kind = "generic_csv", None
    seeds = []
    for row in rows[1:]:
        if title_idx >= len(row):
            continue
        title = _clean(row[title_idx])
        creator = _clean(row[creator_idx]) if (
            creator_idx is not None and creator_idx < len(row)) else None
        if title:
            seeds.append(_seed(title, creator, fmt, kind))
    return seeds


def _parse_text(text: str) -> list[dict]:
    seeds = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        title = _clean(_BULLET_OR_NUMBER_RE.sub("", stripped))
        if title:
            seeds.append(_seed(title, None, "text_list", None))
    return seeds


def _seed(title: str, creator: str | None, fmt: str,
          kind: str | None) -> dict:
    # Catalogue convention: music and book seeds carry the creator in
    # parentheses ("Kind of Blue (Miles Davis)"); films stay bare.
    name = title
    if creator and kind in ("music", "book") \
            and creator.lower() not in title.lower():
        name = f"{title} ({creator})"
    return {"name": name, "title": title, "creator": creator,
            "source": fmt, "kind": kind}


def _seeds_from_rows(rows: list, fmt: str) -> list[dict]:
    kind = {"spotify_json": "music"}.get(fmt)
    seeds = []
    for row in rows:
        title = _title_of(row)
        if title:
            creator = _creator_of(row) if isinstance(row, dict) else None
            if isinstance(row, dict) and isinstance(row.get("track"), dict):
                creator = _creator_of(row["track"]) or creator
            seeds.append(_seed(title, creator, fmt, kind))
    return seeds


def _canonical(name: str) -> str:
    """Map an imported seed name to its catalogue name when it names a
    known entity — the same canonicalisation pasted lists get. An
    imported music/book name carries its creator in parentheses, so
    the bare title is tried as well as the full name."""
    from . import fixtures
    from .parser import _canonical_seed_name
    canon = _canonical_seed_name(name)
    if canon != name:
        return canon
    base = name.split(" (")[0]
    if base != name:
        low = base.lower()
        for cat_name, ent in fixtures.BY_NAME.items():
            if low == cat_name or low == ent.name.split(" (")[0].lower():
                return ent.name
    return name


def parse_import_bytes(data: bytes, filename: str = "",
                       source_hint: str | None = None) -> dict:
    """Parse one exported taste file into import provenance + seeds.

    Returns {"source_file": basename, "format": ..., "items": [seed
    dicts], "seed_names": [canonical names]}. Raises ValueError with a
    category prefix on any malformed input."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError(f"too_large: import file exceeds "
                         f"{MAX_IMPORT_BYTES} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("undecodable: import file is not UTF-8 text")
    suffix = Path(filename).suffix.lower()
    stripped = text.lstrip()
    hint = (source_hint or "").strip().lower() or None
    if suffix == ".json" or (suffix not in (".csv", ".txt", ".md")
                             and stripped[:1] in "{["):
        seeds = _parse_json(text, hint)
        fmt = seeds[0]["source"] if seeds else (
            "spotify_json" if hint == "spotify" else "generic_json")
    elif suffix == ".csv" or (suffix not in (".txt", ".md")
                              and "," in (stripped.splitlines() or [""])[0]
                              and any(h in stripped.splitlines()[0].lower()
                                      for h in _CSV_TITLE_HEADERS)):
        seeds = _parse_csv(text)
        fmt = seeds[0]["source"] if seeds else "generic_csv"
    else:
        seeds = _parse_text(text)
        fmt = "text_list"
    seen: set[str] = set()
    items: list[dict] = []
    for seed in seeds:
        key = seed["name"].lower()
        if key not in seen:
            seen.add(key)
            items.append(seed)
        if len(items) >= MAX_IMPORT_ITEMS:
            break
    if not items:
        raise ValueError("no_items: no usable titles in the import file")
    return {"source_file": Path(filename).name or "import",
            "format": fmt,
            "items": items,
            "seed_names": [_canonical(s["name"]) for s in items]}


def load_import(path, source_hint: str | None = None) -> dict:
    """Read *path* from disk and parse it (see parse_import_bytes)."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        raise ValueError(f"unreadable: cannot read import file {p.name}")
    return parse_import_bytes(data, filename=p.name,
                              source_hint=source_hint)


#: HTTP statuses worth retrying for a taste-feed fetch: rate limiting
#: and transient server-side failures, mirroring the real Qloo client.
#: Other statuses (400/401/403/404) fail immediately.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


def _api_origin(url: str) -> tuple[urllib.parse.ParseResult, str]:
    """Validate a taste-feed URL and derive its provenance label:
    the host (with port when present) and nothing else — no scheme,
    path, userinfo, or query string ever reaches a plan or a log."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("unsupported: import URL must be an "
                         "http(s) URL with a host")
    label = parsed.hostname
    if parsed.port:
        label = f"{label}:{parsed.port}"
    return parsed, label


def _api_format(fmt: str) -> str:
    """Stamp a file format as API-sourced: ``spotify_json`` becomes
    ``spotify_api_json``, ``text_list`` becomes ``text_api_list``."""
    if "_" in fmt:
        head, tail = fmt.split("_", 1)
        return f"{head}_api_{tail}"
    return f"{fmt}_api"


def fetch_import(url: str, transport=None, timeout: float = 10.0,
                 max_attempts: int = 3, sleeper=None,
                 source_hint: str | None = None) -> dict:
    """Fetch one taste-feed endpoint and parse it like a file.

    ``transport`` is injectable for tests: a callable
    ``(url, timeout) -> (status, body_bytes)``. The default transport
    is a plain urllib GET with no headers beyond ``accept`` — no key
    or credential is ever sent. Transient failures (HTTP 429/5xx and
    transport-level network errors) are retried up to
    ``max_attempts`` total attempts with exponential backoff through
    the injectable ``sleeper``. Every failure raises ``ValueError``
    with a category (``unsupported`` / ``http_error`` /
    ``network_error``, plus the parse categories of
    ``parse_import_bytes``) — never a traceback, and never an echo
    of the URL, its query string, or the response body.

    Returns the ``parse_import_bytes`` shape with provenance
    rewritten for an API source: ``source_file`` is the endpoint's
    host label only, ``format`` (and every item's ``source``) is the
    API-stamped format, and ``source_kind`` is ``"api"``.
    """
    parsed_url, origin = _api_origin(url)
    sleep = sleeper or time.sleep
    attempts = max(1, max_attempts)
    filename = Path(urllib.parse.unquote(parsed_url.path)).name \
        or "taste-api"
    for attempt in range(attempts):
        try:
            if transport is not None:
                status, body = transport(url, timeout)
            else:
                req = urllib.request.Request(
                    url, headers={"accept": "application/json, text/csv, "
                                            "text/plain"},
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
                f"network_error: taste API unreachable "
                f"({type(exc).__name__})") from exc
        if status == 200:
            result = parse_import_bytes(body, filename=filename,
                                        source_hint=source_hint)
            api_fmt = _api_format(result["format"])
            result["format"] = api_fmt
            for item in result["items"]:
                item["source"] = api_fmt
            result["source_file"] = origin
            result["source_kind"] = "api"
            return result
        if status in TRANSIENT_STATUSES and attempt + 1 < attempts:
            sleep(min(0.5 * (2 ** attempt), 4.0))
            continue
        raise ValueError(f"http_error: taste API returned "
                         f"HTTP {status}")
    raise ValueError("network_error: taste API request failed")
