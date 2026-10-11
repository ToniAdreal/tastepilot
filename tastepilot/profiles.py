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

SYNC_FORMAT = "tastepilot-profile-sync"
SYNC_VERSION = 1


def validate_sync_document(document) -> dict:
    """Shape-gate a profile sync document and return a normalised
    copy: ``{owner_fingerprint, updated_at, profile}`` with the
    profile run through validate_preference_profile (weights
    clamped to the learner's caps — a sync document can never
    smuggle in weights the learner itself could not produce).
    ``updated_at`` is kept as the document's ISO string and also
    parsed, so a malformed stamp is a categorised refusal
    (bad_sync_document), never a crash."""
    if not isinstance(document, dict) \
            or document.get("format") != SYNC_FORMAT \
            or document.get("version") != SYNC_VERSION:
        raise ValueError("bad_sync_document")
    fingerprint = document.get("owner_fingerprint")
    updated_at = document.get("updated_at")
    if not isinstance(fingerprint, str) or len(fingerprint) != 12 \
            or any(ch not in "0123456789abcdef" for ch in fingerprint) \
            or not isinstance(updated_at, str):
        raise ValueError("bad_sync_document")
    try:
        datetime.fromisoformat(updated_at)
    except ValueError:
        raise ValueError("bad_sync_document") from None
    profile = validate_preference_profile(document.get("profile"))
    return {"owner_fingerprint": fingerprint,
            "updated_at": updated_at, "profile": profile}


def _resolve_weights(kind: str, local: dict, remote: dict,
                     local_newer: bool, remote_newer: bool,
                     conflicts: list) -> dict:
    """Merge one weight map under the sync rules. A key on both
    sides with different values is a conflict: the newer
    document wins; on a timestamp tie the larger absolute weight
    wins; still tied (+x vs -x) the larger value wins. Every
    conflict is recorded — resolution is deterministic and
    order-independent, never silent."""
    merged = {}
    for key in sorted(set(local) | set(remote)):
        if key not in remote:
            merged[key] = local[key]
        elif key not in local:
            merged[key] = remote[key]
        elif local[key] == remote[key]:
            merged[key] = local[key]
        else:
            if local_newer:
                resolved, rule = local[key], "newer"
            elif remote_newer:
                resolved, rule = remote[key], "newer"
            elif abs(local[key]) != abs(remote[key]):
                resolved = local[key] \
                    if abs(local[key]) > abs(remote[key]) else remote[key]
                rule = "larger_absolute"
            else:
                resolved = max(local[key], remote[key])
                rule = "larger_value"
            merged[key] = resolved
            conflicts.append({"kind": kind, "key": key,
                              "local": local[key], "remote": remote[key],
                              "resolved": resolved, "rule": rule})
    return merged


def resolve_profile_sync(local_document, remote_document) -> dict:
    """Resolve two sync documents for the SAME owner into one
    profile, with every disagreement reported.

    Sync is not the additive lesson merge (merge_profiles): two
    devices that learned on their own and later sync hold
    overlapping history, so summing would double-count it.
    Instead weights follow _resolve_weights and the counts take
    the element-wise maximum (both sides already include the
    shared history). The two documents must name the same owner
    fingerprint (owner_mismatch otherwise). Returns
    ``{profile, conflicts}``; resolving a document with itself
    returns its own profile with no conflicts."""
    local = validate_sync_document(local_document)
    remote = validate_sync_document(remote_document)
    if local["owner_fingerprint"] != remote["owner_fingerprint"]:
        raise ValueError("owner_mismatch")
    local_time = datetime.fromisoformat(local["updated_at"])
    remote_time = datetime.fromisoformat(remote["updated_at"])
    conflicts: list = []
    merged = empty_profile()
    for key in ("plans_used", "plans_ignored", "kept_swaps",
                "undone_swaps"):
        merged[key] = max(local["profile"][key], remote["profile"][key])
    merged["tag_weights"] = _resolve_weights(
        "tag", local["profile"]["tag_weights"],
        remote["profile"]["tag_weights"],
        local_time > remote_time, remote_time > local_time, conflicts)
    merged["domain_weights"] = _resolve_weights(
        "domain", local["profile"]["domain_weights"],
        remote["profile"]["domain_weights"],
        local_time > remote_time, remote_time > local_time, conflicts)
    conflicts.sort(key=lambda c: (c["kind"], c["key"]))
    return {"profile": validate_preference_profile(merged),
            "conflicts": conflicts}


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

    def export_sync(self, owner_token) -> dict | None:
        """The owner's profile as a portable sync document
        (format/version/fingerprint/stamp + the profile), or
        None when the owner has none. The document names the
        owner only by fingerprint — the token never leaves."""
        digest = self._digest(owner_token)
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return None
            return {"format": SYNC_FORMAT, "version": SYNC_VERSION,
                    "owner_fingerprint": digest[:12],
                    "updated_at": _iso(entry["updated_at"]),
                    "profile": copy.deepcopy(entry["profile"])}

    def import_sync(self, owner_token, document) -> dict:
        """Merge a sync document (e.g. exported on another
        device) into the owner's stored profile under the sync
        conflict rules. The document must name this token's
        fingerprint (owner_mismatch). With no stored profile the
        document's profile is adopted as-is. Returns
        ``{profile, conflicts, meta}``."""
        digest = self._digest(owner_token)
        incoming = validate_sync_document(document)
        if incoming["owner_fingerprint"] != digest[:12]:
            raise ValueError("owner_mismatch")
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                merged, conflicts = incoming["profile"], []
            else:
                local_document = {
                    "format": SYNC_FORMAT, "version": SYNC_VERSION,
                    "owner_fingerprint": digest[:12],
                    "updated_at": _iso(entry["updated_at"]),
                    "profile": entry["profile"]}
                resolved = resolve_profile_sync(local_document, document)
                merged, conflicts = resolved["profile"], \
                    resolved["conflicts"]
            entry = {"profile": merged, "updated_at": self._clock()}
            self._entries[digest] = entry
            self._entries.move_to_end(digest)
            while len(self._entries) > self.max_profiles:
                self._entries.popitem(last=False)
            self._save()
            return {"profile": copy.deepcopy(merged),
                    "conflicts": conflicts,
                    "meta": self._meta(digest, entry)}
