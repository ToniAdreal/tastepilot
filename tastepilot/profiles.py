"""Persistent preference profiles: the learned suggestion
ranking's profile, carried across plans and sessions.

Run 31's learned ranking derived its preference profile per
request, from the sealed swap histories of the plans supplied
with that request — close the browser and the learning was
gone. This module persists it, in the same owner-token form
the share store already uses (no account system, no sign-up,
no password anywhere):

  * A profile is keyed by the SHA-256 hash of the caller's
    owner token — the token itself is never stored, exactly as
    in server.ShareStore, so a leaked profile file exposes no
    credential. Responses name a token only by its fingerprint
    (the first 12 hex chars of its hash).
  * A profile can only ever be WRITTEN from sealed evidence:
    ``update_from_plans`` derives the lesson with
    swap.learn_preferences (a plan whose chain does not verify
    is ignored and counted, never believed) and merges it into
    the stored profile with swap.merge_profiles under the same
    weight caps. There is no endpoint or method that stores
    caller-supplied weights — the persisted artefact is as
    tamper-evident in provenance as the plans it came from.
  * Persistence mirrors the share store: in-memory by default;
    with a file path every mutation rewrites that JSON file
    atomically (temp file + os.replace, parent dirs created),
    a missing/corrupt file starts an empty store instead of
    crashing, malformed rows are skipped (a row whose profile
    fails validation is dropped, never repaired into weights),
    and a failed write degrades to in-memory for that mutation.
  * The store is bounded (MAX_PROFILES owners, least-recently
    updated evicted first) so a hosted demo cannot grow it
    without limit. Profiles do not expire on their own —
    unlike share links they are the owner's own artefact —
    but an owner can clear theirs at any time (clear()).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .swap import (empty_profile, learn_preferences, merge_profiles,
                   validate_preference_profile)

MAX_PROFILES = 256            # owners with a stored profile
MAX_PROFILE_HISTORY_PLANS = 8  # plans per profile update
MIN_OWNER_TOKEN_CHARS = 16    # mirrors server's owner-token rule
MAX_OWNER_TOKEN_CHARS = 200
PROFILE_FILE_ENV_VAR = "TASTEPILOT_PROFILE_FILE"


def owner_token_hash(token) -> str | None:
    """SHA-256 hash of an owner token, or None for no/invalid
    token. The same rule as server.owner_token_hash (16–200
    chars, no whitespace); duplicated here so this module does
    not import the HTTP layer."""
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not MIN_OWNER_TOKEN_CHARS <= len(token) <= MAX_OWNER_TOKEN_CHARS:
        return None
    if any(ch.isspace() for ch in token):
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class ProfileStore:
    """Owner-keyed store of persistent preference profiles.

    Entries are keyed by owner-token hash and carry the merged
    profile plus an ``updated_at`` stamp from an injectable
    clock. Payloads are deep-copied in and out so callers can
    never mutate stored state."""

    def __init__(self, max_profiles: int = MAX_PROFILES,
                 path: str | Path | None = None, clock=time.time):
        if max_profiles < 1:
            raise ValueError("max_profiles must be >= 1")
        self.max_profiles = max_profiles
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        # owner_hash -> {"profile": dict, "updated_at": float},
        # least-recently updated first.
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data["profiles"]
            if not isinstance(rows, list):
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return  # missing/corrupt file: start empty, never crash
        for row in rows:
            if not isinstance(row, dict):
                continue
            owner_hash = row.get("owner_hash")
            updated_at = row.get("updated_at")
            if not isinstance(owner_hash, str) or len(owner_hash) != 64 \
                    or not isinstance(updated_at, (int, float)):
                continue  # malformed row: skip it, never guess
            try:
                profile = validate_preference_profile(row.get("profile"))
            except ValueError:
                continue  # weights that fail the gate are dropped
            self._entries[owner_hash] = {
                "profile": profile, "updated_at": float(updated_at)}
            self._entries.move_to_end(owner_hash)
        while len(self._entries) > self.max_profiles:
            self._entries.popitem(last=False)

    def _save(self) -> None:
        """Persist atomically. Caller holds the lock. A write
        failure degrades to in-memory for this mutation instead
        of failing the request — the demo stays up."""
        if self.path is None:
            return
        data = {"version": 1, "profiles": [
            {"owner_hash": owner_hash, **entry}
            for owner_hash, entry in self._entries.items()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _digest(owner_token) -> str:
        digest = owner_token_hash(owner_token)
        if digest is None:
            raise ValueError("invalid_owner_token")
        return digest

    def _meta(self, owner_hash: str, entry: dict | None) -> dict:
        return {"owner_fingerprint": owner_hash[:12],
                "exists": entry is not None,
                "updated_at": _iso(entry["updated_at"])
                if entry is not None else None}

    # -- public API --------------------------------------------------------

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def info(self) -> dict:
        with self._lock:
            return {"persistent": self.persistent,
                    "count": len(self._entries),
                    "max": self.max_profiles}

    def get(self, owner_token) -> dict | None:
        """The stored profile for this token (a deep copy), or
        None when the owner has none. Raises ValueError
        (invalid_owner_token) for a malformed token."""
        digest = self._digest(owner_token)
        with self._lock:
            entry = self._entries.get(digest)
            return None if entry is None \
                else copy.deepcopy(entry["profile"])

    def meta(self, owner_token) -> dict:
        digest = self._digest(owner_token)
        with self._lock:
            return self._meta(digest, self._entries.get(digest))

    def update_from_plans(self, owner_token, plans: list) -> dict:
        """Derive a lesson from plans' sealed swap histories and
        merge it into the owner's stored profile.

        ``plans`` must be a non-empty list of at most
        MAX_PROFILE_HISTORY_PLANS entries (bad_history /
        too_many_history_plans, mirroring the suggest gates);
        non-dict entries are counted as ignored by the learner,
        exactly like chain-broken ones. Returns
        ``{profile, derived, meta}``: the stored profile after
        the merge, the profile this update derived, and the
        owner's link metadata. The merge is additive under the
        learner's own caps, so re-saving the same plan's lesson
        twice can never push a weight past the cap."""
        if not isinstance(plans, list) or not plans:
            raise ValueError("bad_history")
        if len(plans) > MAX_PROFILE_HISTORY_PLANS:
            raise ValueError("too_many_history_plans")
        digest = self._digest(owner_token)
        derived = learn_preferences(plans)
        with self._lock:
            entry = self._entries.get(digest)
            base = entry["profile"] if entry is not None \
                else empty_profile()
            merged = merge_profiles(base, derived)
            entry = {"profile": merged, "updated_at": self._clock()}
            self._entries[digest] = entry
            self._entries.move_to_end(digest)
            while len(self._entries) > self.max_profiles:
                self._entries.popitem(last=False)
            self._save()
            return {"profile": copy.deepcopy(merged),
                    "derived": derived,
                    "meta": self._meta(digest, entry)}

    def clear(self, owner_token) -> bool:
        """Drop the owner's profile. True when one existed."""
        digest = self._digest(owner_token)
        with self._lock:
            if digest not in self._entries:
                return False
            del self._entries[digest]
            self._save()
            return True
