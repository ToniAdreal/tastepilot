"""Deterministic request parsing (no LLM required).

Extracts the scenario, an optional location, the taste domains to plan,
and the seed entities the user named. Seeds are matched against the
known entity catalogue first; anything quoted is also passed to Qloo
search by the agent, so real-mode seeds are not limited to fixtures.

Richer seed capture: a pasted labelled list — ``Watchlist: …``,
``Playlist: …``, ``Reading list: …`` (comma/semicolon separated, or one
bulleted item per line) — contributes every item as a seed, including
titles the catalogue does not know, which the agent passes verbatim to
Qloo search. Items are delimited only by commas, semicolons, bullets,
and newlines: ``and`` is never a delimiter, so a title like "Klara and
the Sun" survives intact. A labelled list ends at the next label, at a
blank line after bulleted items, at the first non-bullet line after
bulleted items, or — on the label's own line — at the end of the
sentence, so trailing prose is never mistaken for a seed.

Multi-city trips: every known city the request mentions is a leg, in
mention order (``locations``); ``location`` stays the first leg for
callers that only need one. The agent plans each leg separately with
the same resolved seeds — per-leg taste continuity by construction.

Date-aware legs: calendar dates in the request are attached to legs —
a date belongs to the leg whose city was most recently mentioned
before it (a date before any city belongs to the first leg). Supported
forms, all resolved deterministically against a reference date (the
caller supplies it; the agent defaults to today):

- explicit: ``2026-11-03``, ``Nov 3``, ``November 3, 2026``,
  ``3 November 2026`` (ordinals ok; a missing year rolls forward to
  the next occurrence on/after the reference date);
- ranges: ``from Nov 3 to Nov 5``, ``Nov 3 – Nov 5``, the same-month
  shorthand ``Nov 3-5``, and durations ``Nov 3 for 3 nights``
  (``for N days`` ends N-1 days after the start);
- relative: ``today``, ``tomorrow``, ``in N days``, ``on/this
  <weekday>`` (the upcoming occurrence, today counts) and ``next
  <weekday>`` (that occurrence one week later).

Per leg the parser reports ``start``/``end`` (ISO dates), ``nights``,
and the raw date text it used; cross-leg problems are reported as
``date_warnings`` (``out_of_order``, ``overlap``,
``end_before_start``) instead of being silently repaired. Dates are
composition constraints only — the Qloo API has no date filter, so
nothing date-shaped is ever sent to it.

Inter-leg travel-time feasibility: the schedule is also checked
against offline door-to-door transfer estimates between leg cities
(see ``travel``): the parser reports ``transfers`` (one estimate per
consecutive pair of legs, with its schedule window and an
``ok``/``tight``/``infeasible``/``unknown`` status) and
``travel_warnings`` for tight or infeasible transfers — same
report-never-repair doctrine as ``date_warnings``.

Negative preferences: a short clause behind a cue word ("no jazz",
"avoid ramen and wine", "allergic to shellfish", "don't want
museums") is captured as ``exclusions`` — phrases the agent filters
recommendations against deterministically (exact name/tag/domain
match in ``exclusion_match``) AFTER the evidence checker has run.
Exclusions are user preference only: they are never sent to Qloo,
and an excluded item keeps its (passing) evidence verification —
excluded is not rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta

from . import fixtures
from .travel import assess_transfers

CITIES = ["Kyoto", "New York", "NYC", "Paris", "Tokyo", "London",
          "Los Angeles", "Berlin", "Seoul", "Osaka"]
CITY_CANON = {"NYC": "New York"}

SCENARIOS: dict[str, dict] = {
    "date": {"keywords": ("date", "anniversary", "romantic"),
             "domains": ["food", "place", "music", "film"]},
    "gift": {"keywords": ("gift", "birthday", "present"),
             "domains": ["brand", "book", "music", "film"]},
    "weekend": {"keywords": ("weekend", "trip", "travel", "holiday", "vacation", "getaway"),
                "domains": ["destination", "place", "food", "music", "film"]},
    "evening": {"keywords": ("evening", "night", "dinner", "tonight"),
                "domains": ["food", "music", "film", "place"]},
    "generic": {"keywords": (),
                "domains": ["film", "music", "food", "place", "book"]},
}

DEFAULT_SEEDS = ["Spirited Away", "Kind of Blue (Miles Davis)"]

# Labelled seed lists: label text (lowercase, whitespace-normalised) ->
# the canonical label reported in the plan's seed-list provenance.
LIST_LABELS = {
    "watchlist": "watchlist", "watch list": "watchlist",
    "film list": "watchlist", "movie list": "watchlist",
    "playlist": "playlist", "play list": "playlist",
    "music list": "playlist",
    "reading list": "reading list", "book list": "reading list",
    "favourite": "favourites", "favourites": "favourites",
    "favorite": "favourites", "favorites": "favourites",
    "seed": "seeds", "seeds": "seeds",
}
_LABEL_RE = re.compile(
    r"\b(watch\s*list|film\s+list|movie\s+list|play\s*list|playlist|"
    r"reading\s+list|book\s+list|music\s+list|favourites?|favorites?|"
    r"seeds?)\s*[:\-–—]\s*", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+")
_ITEM_SPLIT_RE = re.compile(r"[,;•]+")
MAX_SEED_CHARS = 60


@dataclass
class ParsedRequest:
    text: str
    scenario: str
    domains: list[str]
    location: str | None
    # Every city mentioned, canonicalised, in mention order — the legs
    # of a multi-city trip. ``location`` is always ``locations[0]``.
    locations: list[str] = field(default_factory=list)
    seed_names: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)
    # Provenance for richer seed capture: [{"label": ..., "items": [...]}]
    # in the order the lists appear in the request.
    seed_lists: list[dict] = field(default_factory=list)
    # Date-aware legs: one entry per leg (aligned with ``locations``,
    # or a single location-less entry), each {"location", "start",
    # "end", "nights", "dates"} with ISO dates or None.
    leg_dates: list[dict] = field(default_factory=list)
    # Cross-leg schedule problems: [{"code", "location", "detail"}].
    date_warnings: list[dict] = field(default_factory=list)
    # Inter-leg transfer estimates (see travel.assess_transfers):
    # [{"from", "to", "distance_km", "mode", "travel_hours",
    # "gap_days", "window_hours", "status", "detail"}].
    transfers: list[dict] = field(default_factory=list)
    # Tight / infeasible transfers, same shape as date_warnings:
    # [{"code", "location", "detail"}] with codes transfer_tight /
    # transfer_infeasible.
    travel_warnings: list[dict] = field(default_factory=list)
    # Negative preferences, in mention order: the phrases the user
    # asked to keep out ("no jazz", "avoid ramen and wine"). The agent
    # applies them as a deterministic filter AFTER the evidence
    # checker — an excluded item is evidence-backed but unwanted,
    # which is a different thing from a rejected hallucination.
    exclusions: list[str] = field(default_factory=list)


def _clean_list_item(raw: str) -> str | None:
    item = raw.strip().strip('"\u201c\u201d\'').strip()
    item = re.sub(r"\s+", " ", item).rstrip(".")
    if not 2 <= len(item) <= MAX_SEED_CHARS:
        return None
    return item


def _split_list_items(segment: str) -> list[str]:
    items: list[str] = []
    seen_bullet = False
    first_line = True
    for line in segment.splitlines():
        if not line.strip():
            if seen_bullet:
                break  # a blank line ends a bulleted list
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            seen_bullet = True
            content = line[bullet.end():]
        elif seen_bullet:
            break  # prose resumed after a bulleted list
        else:
            content = line
            if first_line:
                # The label's own line: the list ends with the sentence,
                # so trailing prose on that line is not a seed.
                content = re.split(r"[.!?]\s+", content, maxsplit=1)[0]
        first_line = False
        for part in _ITEM_SPLIT_RE.split(content):
            item = _clean_list_item(part)
            if item and item not in items:
                items.append(item)
    return items


def extract_seed_lists(text: str) -> list[dict]:
    """Labelled lists in the request: [{"label": ..., "items": [...]}]."""
    matches = list(_LABEL_RE.finditer(text))
    lists: list[dict] = []
    for i, m in enumerate(matches):
        label = LIST_LABELS[re.sub(r"\s+", " ", m.group(1).lower())]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        items = _split_list_items(text[m.end():end])
        if items:
            lists.append({"label": label, "items": items})
    return lists


def _canonical_seed_name(item: str) -> str:
    """Map a list item to its catalogue name when it names a known
    entity (exact name, or the short name before any parenthesis)."""
    low = item.lower()
    for name, ent in fixtures.BY_NAME.items():
        if low == name or low == ent.name.split(" (")[0].lower():
            return ent.name
    return item


def extract_locations(text: str) -> list[str]:
    """Known cities mentioned in the request, canonicalised and in
    mention order (aliases dedupe: "NYC" and "New York" are one leg)."""
    low = text.lower()
    hits: list[tuple[int, str]] = []
    for city in CITIES:
        for m in re.finditer(r"\b" + re.escape(city.lower()) + r"\b", low):
            hits.append((m.start(), CITY_CANON.get(city, city)))
    locations: list[str] = []
    for _, canon in sorted(hits):
        if canon not in locations:
            locations.append(canon)
    return locations


_MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3,
           "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6,
           "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
           "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
           "nov": 11, "november": 11, "dec": 12, "december": 12}
_MONTH_ALT = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
              r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
              r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}
_ORD = r"(?:st|nd|rd|th)?"
_DATE_TOKEN = (
    rf"(?:\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|{_MONTH_ALT}\s+\d{{1,2}}(?!\d){_ORD}(?:,?\s+\d{{4}})?"
    rf"|\d{{1,2}}(?!\d){_ORD}\s+{_MONTH_ALT}(?:,?\s+\d{{4}})?)")
_TOKEN_RE = re.compile(_DATE_TOKEN, re.IGNORECASE)
_SAME_MONTH_RANGE_RE = re.compile(
    rf"({_MONTH_ALT})\s+(\d{{1,2}}(?!\d)){_ORD}\s*[-\u2013\u2014]\s*"
    rf"(\d{{1,2}}(?!\d)){_ORD}(?:,?\s+(\d{{4}}))?", re.IGNORECASE)
_FROM_TO_RE = re.compile(
    rf"\bfrom\s+({_DATE_TOKEN})\s+(?:to|until|through)\s+({_DATE_TOKEN})",
    re.IGNORECASE)
_DASH_RANGE_RE = re.compile(
    rf"({_DATE_TOKEN})\s*[-\u2013\u2014]\s*({_DATE_TOKEN})", re.IGNORECASE)
_DURATION_RE = re.compile(
    rf"({_DATE_TOKEN})\s+for\s+(\d{{1,3}})\s+(nights?|days?)",
    re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"\b(tomorrow|today)\b|\bin\s+(\d{1,3})\s+days?\b"
    r"|\b(next|this|on)\s+(monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b", re.IGNORECASE)


def _resolve_year(month: int, day: int, year: int | None,
                  ref: _date) -> _date | None:
    """Build a date; a missing year rolls forward past the reference."""
    try:
        d = _date(year, month, day) if year else _date(ref.year, month, day)
    except ValueError:
        return None
    if not year and d < ref:
        try:
            d = _date(ref.year + 1, month, day)
        except ValueError:
            return None
    return d


def _parse_date_token(token: str, ref: _date) -> _date | None:
    token = token.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", token)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(
        rf"({_MONTH_ALT})\s+(\d{{1,2}}(?!\d)){_ORD}(?:,?\s+(\d{{4}}))?",
        token, re.IGNORECASE)
    if m:
        return _resolve_year(_MONTHS[m.group(1).lower()], int(m.group(2)),
                             int(m.group(3)) if m.group(3) else None, ref)
    m = re.fullmatch(
        rf"(\d{{1,2}}(?!\d)){_ORD}\s+({_MONTH_ALT})(?:,?\s+(\d{{4}}))?",
        token, re.IGNORECASE)
    if m:
        return _resolve_year(_MONTHS[m.group(2).lower()], int(m.group(1)),
                             int(m.group(3)) if m.group(3) else None, ref)
    return None


def extract_dates(text: str, reference: _date) -> list[dict]:
    """Every calendar date/range in the request, in mention order:
    [{"start": date, "end": date|None, "raw": str, "position": int}].
    A weekday is only a date when introduced by on/this/next, so a
    film called "Friday" is never mistaken for a schedule."""
    found: list[dict] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < c[1] and c[0] < span[1] for c in claimed)

    def add(m: re.Match, start: _date | None, end: _date | None) -> None:
        if start is None:
            return
        claimed.append(m.span())
        found.append({"start": start, "end": end,
                      "raw": text[m.start():m.end()],
                      "position": m.start()})

    for m in _SAME_MONTH_RANGE_RE.finditer(text):
        month = _MONTHS[m.group(1).lower()]
        year = int(m.group(4)) if m.group(4) else None
        add(m, _resolve_year(month, int(m.group(2)), year, reference),
            _resolve_year(month, int(m.group(3)), year, reference))
    for regex in (_FROM_TO_RE, _DASH_RANGE_RE):
        for m in regex.finditer(text):
            if not overlaps(m.span()):
                add(m, _parse_date_token(m.group(1), reference),
                    _parse_date_token(m.group(2), reference))
    for m in _DURATION_RE.finditer(text):
        if overlaps(m.span()):
            continue
        start = _parse_date_token(m.group(1), reference)
        n = int(m.group(2))
        end = None
        if start is not None:
            end = start + timedelta(days=n if m.group(3).lower().startswith("night")
                                    else max(n - 1, 0))
        add(m, start, end)
    for m in _TOKEN_RE.finditer(text):
        if not overlaps(m.span()):
            add(m, _parse_date_token(m.group(0), reference), None)
    for m in _RELATIVE_RE.finditer(text):
        if overlaps(m.span()):
            continue
        if m.group(1):
            d = reference + timedelta(days=1 if m.group(1).lower() == "tomorrow"
                                      else 0)
        elif m.group(2):
            d = reference + timedelta(days=int(m.group(2)))
        else:
            delta = (_WEEKDAYS[m.group(4).lower()] - reference.weekday()) % 7
            if m.group(3).lower() == "next":
                delta += 7
            d = reference + timedelta(days=delta)
        add(m, d, None)
    found.sort(key=lambda f: f["position"])
    return found


def assign_leg_dates(text: str, locations: list[str],
                     reference: _date) -> tuple[list[dict], list[dict]]:
    """Attach the request's dates to legs and derive schedule warnings.

    Returns (leg_dates, date_warnings): one leg_dates entry per leg in
    ``locations`` order (a single location-less entry when the request
    names no city)."""
    found = extract_dates(text, reference)
    leg_names: list[str | None] = list(locations) if locations else [None]
    # A date belongs to the leg whose city was most recently mentioned
    # before it; a date before any city mention belongs to the first.
    low = text.lower()
    mentions: list[tuple[int, str]] = []
    for city in CITIES:
        for m in re.finditer(r"\b" + re.escape(city.lower()) + r"\b", low):
            mentions.append((m.start(), CITY_CANON.get(city, city)))
    mentions.sort()
    buckets: list[list[dict]] = [[] for _ in leg_names]
    for f in found:
        idx = 0
        for pos, canon in mentions:
            if pos > f["position"]:
                break
            if canon in leg_names:
                idx = leg_names.index(canon)
        buckets[idx].append(f)
    leg_dates: list[dict] = []
    for name, bucket in zip(leg_names, buckets):
        starts = [f["start"] for f in bucket]
        ends = [f["end"] for f in bucket if f["end"] is not None]
        if len([f for f in bucket if f["end"] is None]) >= 2:
            ends += [f["start"] for f in bucket if f["end"] is None]
        start = min(starts) if starts else None
        end = max(ends) if ends else None
        leg_dates.append({
            "location": name,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "nights": (end - start).days if start and end else None,
            "dates": [f["raw"] for f in bucket]})
    warnings: list[dict] = []
    prev: dict | None = None
    for leg in leg_dates:
        if leg["start"] and leg["end"] and leg["end"] < leg["start"]:
            warnings.append({"code": "end_before_start",
                             "location": leg["location"],
                             "detail": f"ends {leg['end']} before it "
                                       f"starts {leg['start']}"})
        if prev and leg["start"] and prev["start"]:
            if leg["start"] < prev["start"]:
                warnings.append({"code": "out_of_order",
                                 "location": leg["location"],
                                 "detail": f"starts {leg['start']} before "
                                           f"the previous leg "
                                           f"({prev['location']}, "
                                           f"{prev['start']})"})
            elif prev["end"] and leg["start"] < prev["end"]:
                warnings.append({"code": "overlap",
                                 "location": leg["location"],
                                 "detail": f"starts {leg['start']} before "
                                           f"the previous leg "
                                           f"({prev['location']}) ends "
                                           f"{prev['end']}"})
        if leg["start"]:
            prev = leg
    return leg_dates, warnings


_EXCLUSION_CUE_RE = re.compile(
    r"\b(?:allergic\s+to|avoid|skip|exclude|excluding|without|never|"
    r"no\s+more|no|don'?t\s+want|do\s+not\s+want)\b\s+", re.IGNORECASE)
_EXCLUSION_STOP_RE = re.compile(
    r"[.;!?\n]|\s+but\s+|\s+please\b|,\s+(?:i|we|you)\s+",
    re.IGNORECASE)
_CUE_WORDS_RE = re.compile(
    r"^(?:allergic\s+to|avoid|skip|exclude|excluding|without|never|"
    r"no\s+more|no)\s+", re.IGNORECASE)
_MAX_EXCLUSIONS = 8


def extract_exclusions(text: str) -> list[str]:
    """Negative preferences stated in the request, in mention order.

    A cue ("no", "avoid", "skip", "without", "never", "allergic to",
    "don't want", …) captures the text up to the end of its clause;
    the clause is split on commas, "and", "or", and "&" into phrases,
    with articles stripped. A phrase is only ever a *request* to
    filter — whether it removes anything is decided by the agent's
    exact name/tag/domain matching, so an unmatched phrase ("no
    plans") is recorded and harmlessly removes nothing. Labelled seed
    lists are not exclusion clauses: a cue inside a list segment is
    ignored so a watchlist title is never read as an exclusion."""
    list_spans = []
    for m in _LABEL_RE.finditer(text):
        list_spans.append((m.start(), m.end()))
    phrases: list[str] = []
    for m in _EXCLUSION_CUE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in list_spans):
            continue
        segment = text[m.end():]
        stop = _EXCLUSION_STOP_RE.search(segment)
        if stop:
            segment = segment[:stop.start()]
        # Guards against prose that merely contains a cue word: a
        # genuine exclusion clause is short ("no jazz or ramen");
        # "don't want to miss X" wants X, it does not exclude it; and
        # "never been to Kyoto" is biography, not a preference.
        if segment.strip().lower().startswith(("to ", "been ")):
            continue
        if len(segment.split()) > 5:
            continue
        for part in re.split(r",|\band\b|\bor\b|&", segment):
            phrase = re.sub(r"\s+", " ", part.strip().lower())
            # A second cue can hide inside the first cue's clause
            # ("allergic to shellfish, skip kaiseki") — strip it so
            # the recorded phrase is the thing excluded, not the cue.
            phrase = _CUE_WORDS_RE.sub("", phrase)
            phrase = re.sub(r"^(any|the|a|an|more)\s+", "", phrase)
            phrase = phrase.strip(" \"'“”")
            if (2 <= len(phrase) <= 40 and not phrase.isdigit()
                    and phrase not in phrases):
                phrases.append(phrase)
            if len(phrases) >= _MAX_EXCLUSIONS:
                return phrases
    return phrases


def exclusion_match(item: dict, exclusions: list[str]) -> str | None:
    """The exclusion phrase that removes ``item``, or None.

    Deterministic and exact: a phrase matches when it equals the
    entity's name or short name, equals its domain, is one of its
    tags, or has a word (≥3 chars, naive singulars count) that is
    one of its tags or one of the words of its name — so "jazz"
    removes jazz-tagged items, "jazz bars" still finds the "jazz"
    tag, and "no museums" finds the museum by name, while an
    unrelated phrase removes nothing."""
    entity = item.get("entity") or {}
    name = (entity.get("name") or "").lower()
    short = name.split(" (")[0]
    tags = {t.lower() for t in entity.get("tags") or []}
    name_words = {w for w in re.split(r"[^a-z0-9]+", name) if w}
    domain = (item.get("domain") or "").lower()

    def variants(word: str) -> tuple[str, ...]:
        # "books" should find the "book" domain, "museums" the
        # "museum" in a name: try the naive singular beside the word.
        return (word, word[:-1]) if word.endswith("s") and len(word) > 3 \
            else (word,)

    for phrase in exclusions:
        if phrase in (name, short, domain) or phrase in tags:
            return phrase
        if any(v in (domain,) for v in variants(phrase)):
            return phrase
        for word in phrase.split():
            if len(word) < 3:
                continue
            if any(v in tags or v in name_words for v in variants(word)):
                return phrase
    return None


def parse_request(text: str,
                  reference_date: _date | None = None,
                  timetable_provider=None) -> ParsedRequest:
    low = text.lower()
    scenario = "generic"
    for name, spec in SCENARIOS.items():
        if name != "generic" and any(k in low for k in spec["keywords"]):
            scenario = name
            break
    locations = extract_locations(text)
    if not locations:
        m = re.search(r"\bin\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)", text)
        if m:
            locations = [m.group(1)]
    location = locations[0] if locations else None
    seed_names: list[str] = []
    for name, ent in fixtures.BY_NAME.items():
        short = ent.name.split(" (")[0].lower()
        if (name in low or short in low) and ent.name not in seed_names:
            seed_names.append(ent.name)
    # Richer seed capture: every item of a pasted labelled list becomes
    # a seed — catalogue titles under their canonical name, unknown
    # titles verbatim (the agent passes them to Qloo search as-is).
    seed_lists = extract_seed_lists(text)
    for lst in seed_lists:
        for item in lst["items"]:
            canonical = _canonical_seed_name(item)
            if canonical not in seed_names:
                seed_names.append(canonical)
    # A bare location mention ("...in Kyoto") is not a taste seed —
    # for any leg of the trip, not just the first.
    for loc in locations:
        if loc in seed_names:
            seed_names.remove(loc)
    quoted = re.findall(r'["\u201c]([^"\u201d]{2,60})["\u201d]', text)
    for q in quoted:
        if q not in seed_names:
            seed_names.append(q)
    leg_dates, date_warnings = assign_leg_dates(
        text, locations, reference_date or _date.today())
    transfers, travel_warnings = assess_transfers(
        leg_dates, provider=timetable_provider)
    return ParsedRequest(text=text, scenario=scenario,
                         domains=list(SCENARIOS[scenario]["domains"]),
                         location=location, locations=locations,
                         seed_names=seed_names, quoted=quoted,
                         seed_lists=seed_lists, leg_dates=leg_dates,
                         date_warnings=date_warnings,
                         transfers=transfers,
                         travel_warnings=travel_warnings,
                         exclusions=extract_exclusions(text))
