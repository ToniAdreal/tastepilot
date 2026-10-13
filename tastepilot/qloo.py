"""Qloo API adapter: one interface, a real client and a mock client.

The interface mirrors the hackathon Qloo API (base
https://hackathon.api.qloo.com, header ``X-Api-Key``):

* ``search(query)``      -> GET  /search?query=...&limit=...
* ``insights(seeds, target_domain)`` -> GET /v2/insights with
  ``filter.type=<urn>`` and ``signal.interests.entities=<ids>``

The real client activates only when ``QLOO_API_KEY`` is present (env or
app/.env). The mock client serves the same interface from deterministic
fixtures so the whole agent loop — Judge Mode included — runs zero-key.
Every call returns a :class:`QlooResponse` whose ``response_id`` is a
content hash of the canonical request: the trace anchor for evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import fixtures
from .models import (DOMAIN_URNS, URN_DOMAINS, Entity, QlooResponse,
                     ScoredEntity, tag_names)

HACKATHON_BASE = "https://hackathon.api.qloo.com"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def response_id_for(endpoint: str, request: dict, source: str,
                    results: list | None = None) -> str:
    """Trace anchor for one response. Includes the result entity ids:
    two calls with identical request parameters (place vs food share
    the urn:entity:place filter) must still seal as distinct responses."""
    payload = {"endpoint": endpoint, "request": request,
               "result_ids": [r.entity.id for r in results or []]}
    digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    return f"{source}:{endpoint}:{digest[:16]}"


def load_env_key(env_path: str | Path | None = None) -> str | None:
    """Read QLOO_API_KEY from the environment or an app/.env file.

    The key value is never logged or returned anywhere else; callers get
    only the string to place in the X-Api-Key header.
    """
    if os.environ.get("QLOO_API_KEY"):
        return os.environ["QLOO_API_KEY"]
    path = Path(env_path) if env_path else Path(__file__).resolve().parent.parent / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("QLOO_API_KEY=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return None


class QlooError(RuntimeError):
    pass


class MockQlooClient:
    """Deterministic fixture-backed client (Judge Mode)."""

    source = "mock"

    def search(self, query: str, limit: int = 10) -> QlooResponse:
        request = {"query": query, "limit": limit}
        q = query.strip().lower()
        scored: list[ScoredEntity] = []
        for ent in fixtures.ENTITIES:
            score = 0.0
            if q and q in ent.name.lower():
                score = 1.0
            elif q and any(q in t for t in ent.tags):
                score = 0.7
            elif q and any(tok in ent.name.lower() for tok in q.split() if len(tok) > 2):
                score = 0.5
            if score:
                scored.append(ScoredEntity(ent, score))
        scored.sort(key=lambda s: (-s.affinity, s.entity.name))
        results = scored[:limit]
        return QlooResponse(response_id_for("search", request, self.source, results),
                            "search", request, results, self.source, len(results))

    def insights(self, seed_ids: list[str], target_domain: str,
                 limit: int = 5, location: str | None = None) -> QlooResponse:
        request = {"filter.type": DOMAIN_URNS[target_domain],
                   "signal.interests.entities": ",".join(seed_ids),
                   "take": limit, "location": location}
        best: dict[str, tuple[float, Entity]] = {}
        seeds = [fixtures.BY_ID[s] for s in seed_ids if s in fixtures.BY_ID]
        for seed in seeds:
            for target_id, affinity in fixtures.EDGES.get(seed.id, []):
                target = fixtures.BY_ID[target_id]
                if target.domain != target_domain or target.id in seed_ids:
                    continue
                if location and target.location and target.location.lower() != location.lower():
                    continue
                if target_id not in best or affinity > best[target_id][0]:
                    best[target_id] = (affinity, target)
        # Same-domain fallbacks: entities sharing tags with any seed, so a
        # plan never comes back empty just because no explicit edge exists.
        if not best:
            seed_tags = set().union(*(set(s.tags) for s in seeds)) if seeds else set()
            for ent in fixtures.ENTITIES:
                if location and ent.location and ent.location.lower() != location.lower():
                    continue
                if ent.domain == target_domain and ent.id not in seed_ids:
                    overlap = len(seed_tags & set(ent.tags))
                    if overlap:
                        best[ent.id] = (0.5 + 0.05 * overlap, ent)
        results = []
        for target_id, (affinity, target) in best.items():
            shared: tuple[str, ...] = ()
            for seed in seeds:
                shared = fixtures.shared_tags(seed, target)
                if shared:
                    break
            results.append(ScoredEntity(target, round(affinity, 4), shared))
        results.sort(key=lambda s: (-s.affinity, s.entity.name))
        results = results[:limit]
        return QlooResponse(response_id_for("insights", request, self.source, results),
                            "insights", request, results, self.source, len(results))


#: HTTP statuses worth retrying: rate limiting and transient server-side
#: failures. Auth/validation errors (400/401/403/404) fail immediately —
#: retrying them cannot succeed and only delays the diagnosis.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


class RealQlooClient:
    """Live hackathon API client (urllib, no SDK). Transport is injectable
    for tests; the API key is only ever placed in the X-Api-Key header.

    Resilience: transient failures — HTTP 429/5xx and transport-level
    network errors (timeout, DNS, connection reset, surfaced by urllib as
    ``URLError``/``OSError``) — are retried up to ``max_attempts`` total
    attempts with exponential backoff through the injectable ``sleeper``
    (tests record the delays instead of sleeping). Every failure mode
    ultimately raises :class:`QlooError` — never a raw urllib exception —
    so the smoke check and the agent loop can report it uniformly, and
    no error message ever contains the API key (it lives only in the
    header, and errors carry just the path, status, or exception type).
    """

    source = "real"

    def __init__(self, api_key: str, base_url: str = HACKATHON_BASE, transport=None,
                 max_attempts: int = 3, sleeper=None):
        if not api_key:
            raise QlooError("QLOO_API_KEY is required for the real client")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._transport = transport  # callable(method, url, headers) -> (status, dict)
        self.max_attempts = max(1, max_attempts)
        self._sleeper = sleeper or time.sleep

    def _backoff(self, attempt: int) -> None:
        self._sleeper(min(0.5 * (2 ** attempt), 4.0))

    def _get(self, path: str, params: dict) -> dict:
        url = self.base_url + path + "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
        headers = {"X-Api-Key": self._api_key, "accept": "application/json"}
        last_error: QlooError | None = None
        for attempt in range(self.max_attempts):
            try:
                if self._transport is not None:
                    status, payload = self._transport("GET", url, headers)
                else:
                    req = urllib.request.Request(url, headers=headers, method="GET")
                    try:
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            status = resp.status
                            payload = json.loads(resp.read().decode("utf-8"))
                    except urllib.error.HTTPError as e:  # live path
                        status, payload = e.code, {}
            except (urllib.error.URLError, OSError) as exc:
                # Transient network failure (URLError covers timeouts and
                # DNS; ConnectionError/TimeoutError are OSError subclasses).
                # The message names only the path and the exception type —
                # a URLError's own text can embed the full request URL.
                last_error = QlooError(
                    f"Qloo API network error ({type(exc).__name__}) on {path}")
                if attempt + 1 < self.max_attempts:
                    self._backoff(attempt)
                    continue
                raise last_error from exc
            if status == 200:
                return payload
            if status in TRANSIENT_STATUSES and attempt + 1 < self.max_attempts:
                self._backoff(attempt)
                continue
            raise QlooError(f"Qloo API HTTP {status} on {path}")
        raise last_error or QlooError(f"Qloo API request failed on {path}")

    @staticmethod
    def _rows(payload: dict) -> list:
        """Result rows from any documented payload envelope: a flat
        ``results``/``entities``/``data`` list, or ``results`` as an
        object wrapping its ``entities``."""
        if not isinstance(payload, dict):
            return []
        rows = payload.get("results")
        if isinstance(rows, dict):
            rows = rows.get("entities") or rows.get("results") or []
        if not isinstance(rows, list):
            rows = payload.get("entities") or payload.get("data") or []
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _affinity(row: dict) -> float:
        """Affinity from ``affinity`` then ``score``; string numbers are
        coerced, and a missing/unparseable value falls back to 0.5 rather
        than crashing the parse — the smoke check's shape validation is
        what flags systematic drift, per-field."""
        for key in ("affinity", "score"):
            value = row.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.5

    @classmethod
    def _parse_entities(cls, payload: dict, domain_hint: str | None = None) -> list[ScoredEntity]:
        out: list[ScoredEntity] = []
        for row in cls._rows(payload):
            if not isinstance(row, dict):
                continue
            nested = row.get("entity")
            nested = nested if isinstance(nested, dict) else {}
            # Entity fields may sit at the top level or nested under an
            # "entity" object (with the score at the top level); fields
            # may also live under "properties". Top level wins on ties.
            merged = {**nested,
                      **{k: v for k, v in row.items() if k != "entity"}}
            props = merged.get("properties") or {}
            urn = merged.get("type") or merged.get("entity_type") or ""
            domain = URN_DOMAINS.get(urn, domain_hint or "place")
            # Deliberately faithful fallbacks: an explicitly empty name
            # stays empty and a missing id stays missing, so the smoke
            # check's shape validation can still flag that exact drift
            # instead of the parser silently repairing it.
            raw_name = merged.get("name", props.get("name"))
            name = "unknown" if raw_name is None else str(raw_name)
            ent = Entity(id=str(merged.get("entity_id") or merged.get("id")
                                or (raw_name or "")),
                         name=name,
                         domain=domain,
                         tags=tuple(tag_names(
                             merged.get("tags") or props.get("tags") or ())),
                         location=props.get("location") or merged.get("location"))
            out.append(ScoredEntity(ent, cls._affinity(merged)))
        return out

    def search(self, query: str, limit: int = 10) -> QlooResponse:
        request = {"query": query, "limit": limit}
        payload = self._get("/search", request)
        results = self._parse_entities(payload)[:limit]
        return QlooResponse(response_id_for("search", request, self.source, results),
                            "search", request, results, self.source, len(results))

    def insights(self, seed_ids: list[str], target_domain: str,
                 limit: int = 5, location: str | None = None) -> QlooResponse:
        request = {"filter.type": DOMAIN_URNS[target_domain],
                   "signal.interests.entities": ",".join(seed_ids),
                   "take": limit}
        if location:
            request["filter.location.query"] = location
        payload = self._get("/v2/insights", request)
        results = self._parse_entities(payload, target_domain)[:limit]
        return QlooResponse(response_id_for("insights", request, self.source, results),
                            "insights", request, results, self.source, len(results))


def make_client(mode: str = "mock", env_path=None):
    """Factory: mode 'real' requires a key; 'auto' picks real when a key
    exists and mock otherwise. Never raises for a missing key in auto."""
    if mode == "mock":
        return MockQlooClient()
    key = load_env_key(env_path)
    if mode == "real":
        if not key:
            raise QlooError("QLOO_API_KEY not found (env or app/.env); use mock mode")
        return RealQlooClient(key)
    if mode == "auto":
        return RealQlooClient(key) if key else MockQlooClient()
    raise QlooError(f"unknown mode: {mode}")
