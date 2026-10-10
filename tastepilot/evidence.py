"""Hash-chained evidence log (design ported from VerifyGate).

Every Qloo call behind a plan is sealed as a chained entry: each entry's
hash covers the previous hash plus the entry's canonical content, and the
finished record is HMAC-signed. Recomputing the chain detects any edit —
a recommendation, an affinity score, or a verdict — after the fact.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from . import __version__

DEFAULT_DEV_KEY = "tastepilot-dev-key-NOT-FOR-PRODUCTION"
GENESIS = "tastepilot/genesis/v1"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def signing_key() -> str:
    return os.environ.get("TASTEPILOT_SIGNING_KEY", DEFAULT_DEV_KEY)


class EvidenceLog:
    """Append-only builder for one plan's evidence record."""

    def __init__(self, request_text: str, mode: str):
        self._entries: list[dict] = []
        self._prev = _sha(GENESIS)
        header = {"request_text": request_text, "mode": mode}
        self._prev = _sha(self._prev + _canonical(header))
        self._entries.append({"kind": "header", "hash": self._prev, **header})

    def add_qloo_call(self, response_dict: dict) -> str:
        payload = {"response": response_dict}
        self._prev = _sha(self._prev + _canonical(payload))
        self._entries.append({"kind": "qloo_call", "hash": self._prev, **payload})
        return self._prev

    def add_llm_call(self, call: dict) -> None:
        payload = {"call": call}
        self._prev = _sha(self._prev + _canonical(payload))
        self._entries.append({"kind": "llm_call", "hash": self._prev, **payload})

    def seal(self, verdict: str) -> dict:
        record_hash = _sha(self._prev + _canonical({"verdict": verdict}))
        signature = hmac.new(signing_key().encode("utf-8"),
                             record_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"tool": "tastepilot", "version": __version__,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "verdict": verdict, "entries": self._entries,
                "record_hash": record_hash, "signature": signature,
                "signing_key_is_default_dev_key": signing_key() == DEFAULT_DEV_KEY}


def verify_chain(record: dict) -> bool:
    """Recompute the hash chain + signature; True iff the record is intact."""
    try:
        entries = record["entries"]
        prev = _sha(GENESIS)
        header = {k: entries[0][k] for k in ("request_text", "mode")}
        prev = _sha(prev + _canonical(header))
        if entries[0]["hash"] != prev:
            return False
        for entry in entries[1:]:
            payload = {k: v for k, v in entry.items() if k not in ("kind", "hash")}
            prev = _sha(prev + _canonical(payload))
            if entry["hash"] != prev:
                return False
        record_hash = _sha(prev + _canonical({"verdict": record["verdict"]}))
        if record_hash != record["record_hash"]:
            return False
        expected = hmac.new(signing_key().encode("utf-8"),
                            record_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, record["signature"])
    except (KeyError, IndexError, TypeError, AttributeError):
        # Records also arrive from untrusted callers (the browser verify
        # endpoint, shared plan files): any malformed shape is simply an
        # invalid chain, never an exception worth a traceback.
        return False


def qloo_responses(record: dict) -> dict[str, dict]:
    """Index a record's Qloo responses by response_id (checker input)."""
    out: dict[str, dict] = {}
    entries = record.get("entries", []) if isinstance(record, dict) else []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "qloo_call":
            continue
        resp = entry.get("response")
        if isinstance(resp, dict) and resp.get("response_id"):
            out[resp["response_id"]] = resp
    return out
