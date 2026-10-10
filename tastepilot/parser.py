"""Deterministic request parsing (no LLM required).

Extracts the scenario, an optional location, the taste domains to plan,
and the seed entities the user named. Seeds are matched against the
known entity catalogue first; anything quoted is also passed to Qloo
search by the agent, so real-mode seeds are not limited to fixtures.
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


@dataclass
class ParsedRequest:
    text: str
    scenario: str
    domains: list[str]
    location: str | None
    seed_names: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)


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
    # A bare location mention ("...in Kyoto") is not a taste seed.
    if location and location in seed_names:
        seed_names.remove(location)
    quoted = re.findall(r'["\u201c]([^"\u201d]{2,60})["\u201d]', text)
    for q in quoted:
        if q not in seed_names:
            seed_names.append(q)
    return ParsedRequest(text=text, scenario=scenario,
                         domains=list(SCENARIOS[scenario]["domains"]),
                         location=location, seed_names=seed_names, quoted=quoted)
