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
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import fixtures

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
    seed_names: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)
    # Provenance for richer seed capture: [{"label": ..., "items": [...]}]
    # in the order the lists appear in the request.
    seed_lists: list[dict] = field(default_factory=list)


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


def parse_request(text: str) -> ParsedRequest:
    low = text.lower()
    scenario = "generic"
    for name, spec in SCENARIOS.items():
        if name != "generic" and any(k in low for k in spec["keywords"]):
            scenario = name
            break
    location = None
    for city in CITIES:
        if city.lower() in low:
            location = CITY_CANON.get(city, city)
            break
    if location is None:
        m = re.search(r"\bin\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)", text)
        if m:
            location = m.group(1)
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
    # A bare location mention ("...in Kyoto") is not a taste seed.
    if location and location in seed_names:
        seed_names.remove(location)
    quoted = re.findall(r'["\u201c]([^"\u201d]{2,60})["\u201d]', text)
    for q in quoted:
        if q not in seed_names:
            seed_names.append(q)
    return ParsedRequest(text=text, scenario=scenario,
                         domains=list(SCENARIOS[scenario]["domains"]),
                         location=location, seed_names=seed_names, quoted=quoted,
                         seed_lists=seed_lists)
