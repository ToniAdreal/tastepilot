"""Core data types shared across TastePilot."""
from __future__ import annotations

from dataclasses import dataclass, field

# Internal taste domains -> Qloo filter.type URNs (per the Qloo developer
# guide: insights take filter.type as a urn:entity:* value).
DOMAIN_URNS: dict[str, str] = {
    "film": "urn:entity:movie",
    "music": "urn:entity:artist",
    "book": "urn:entity:book",
    "place": "urn:entity:place",
    "food": "urn:entity:place",
    "brand": "urn:entity:brand",
    "destination": "urn:entity:destination",
}
URN_DOMAINS: dict[str, str] = {v: k for k, v in DOMAIN_URNS.items()}


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    domain: str  # one of DOMAIN_URNS keys
    tags: tuple[str, ...] = ()
    location: str | None = None

    @property
    def urn_type(self) -> str:
        return DOMAIN_URNS[self.domain]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "domain": self.domain,
                "urn_type": self.urn_type, "tags": list(self.tags),
                "location": self.location}


@dataclass(frozen=True)
class ScoredEntity:
    entity: Entity
    affinity: float  # 0..1, as returned by Qloo insights
    shared_tags: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"entity": self.entity.to_dict(), "affinity": self.affinity,
                "shared_tags": list(self.shared_tags)}


@dataclass
class QlooResponse:
    """One Qloo API call and its parsed result. The response_id is the
    trace anchor: recommendations cite it, the evidence log seals it."""
    response_id: str
    endpoint: str  # "search" | "insights"
    request: dict
    results: list[ScoredEntity] = field(default_factory=list)
    source: str = "mock"  # "mock" | "real"
    raw_count: int = 0

    def entity_ids(self) -> list[str]:
        return [r.entity.id for r in self.results]

    def to_dict(self) -> dict:
        return {"response_id": self.response_id, "endpoint": self.endpoint,
                "source": self.source, "request": self.request,
                "results": [r.to_dict() for r in self.results]}
