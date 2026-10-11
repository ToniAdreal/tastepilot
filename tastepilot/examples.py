"""Curated demo examples: the canonical Judge Mode request catalogue.

One source of truth for the requests the demo leads with. The web UI's
example buttons, ``GET /api/examples``, and the ``examples`` CLI
subcommand all serve this list, and the test suite runs **every**
example end-to-end through the agent in mock mode, asserting the
checker passes with a valid evidence chain — so the demo script can
never silently rot when the parser, fixtures, or checker change.

Every example is fictional demo data. ``reference_date`` pins the
date context for examples that name dates, so their schedules render
identically whenever they are run (the dates themselves are
composition constraints only — they never reach a Qloo call).
"""

from __future__ import annotations

import copy

EXAMPLES: tuple[dict, ...] = (
    {
        "id": "kyoto-weekend",
        "title": "Slow weekend in Kyoto",
        "request": ("a slow weekend in Kyoto, I love Spirited Away "
                    "and Kind of Blue"),
        "demonstrates": ["agent loop", "cross-domain taste transfer",
                         "per-item evidence"],
    },
    {
        "id": "nyc-date",
        "title": "First date in New York",
        "request": ("a first date in New York, I love "
                    "Kind of Blue (Miles Davis)"),
        "demonstrates": ["one seed, many domains", "date scenario"],
    },
    {
        "id": "gift-amelie",
        "title": "Birthday gift shortlist",
        "request": ("a birthday gift shortlist for a friend who "
                    "loves Amelie"),
        "demonstrates": ["gift scenario", "brands and books from film taste"],
    },
    {
        "id": "seed-lists",
        "title": "Pasted taste lists",
        "request": ("a slow weekend in Kyoto \u2014 watchlist: Spirited Away; "
                    "playlist: Kind of Blue, Bon Iver; reading list: "
                    "Norwegian Wood"),
        "demonstrates": ["labelled seed lists", "list provenance"],
    },
    {
        "id": "no-jazz",
        "title": "Exclusions respected",
        "request": ("a slow weekend in Kyoto, I love Spirited Away, "
                    "but no jazz"),
        "demonstrates": ["negative preferences",
                         "excluded items stay evidence-backed"],
    },
    {
        "id": "multi-city",
        "title": "Two-city trip, one taste",
        "request": ("a slow weekend trip from Kyoto to Paris, I love "
                    "Spirited Away and Amelie"),
        "demonstrates": ["multi-city legs", "per-leg taste continuity"],
    },
    {
        "id": "dated-multi-city",
        "title": "Dated two-city trip",
        "request": ("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
                    "I love Spirited Away"),
        "demonstrates": ["date-aware legs", "transfer feasibility",
                         "day-by-day pacing"],
        "reference_date": "2026-10-11",
    },
)


def list_examples() -> list[dict]:
    """Deep copies of the catalogue, in display order."""
    return copy.deepcopy(list(EXAMPLES))


def get_example(example_id: str) -> dict | None:
    """One example by id (a deep copy), or None."""
    for example in EXAMPLES:
        if example["id"] == example_id:
            return copy.deepcopy(example)
    return None
