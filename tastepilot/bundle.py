"""Audit bundles: one downloadable archive carrying a plan's whole proof.

Until now a plan's proof travelled in pieces: the plan JSON (for the
checker, the verify panel, and share links), the take-away itinerary
exports, and the sealed evidence record embedded inside the plan.
A judge, a collaborator, or the user themselves who wanted to keep —
or hand over — the complete, self-contained proof of one plan had
to assemble those pieces by hand and hope nothing was dropped.

An audit bundle is that assembly, produced in one step: a
deterministic ZIP archive containing the plan exactly as supplied
(``plan.json``, the file the ``verify`` command consumes), the
sealed evidence record on its own (``evidence.json``), a
verification report recomputed at packing time
(``verification.json``), the human take-away itineraries
(``itinerary.md`` / ``itinerary.txt``, and ``itinerary.ics`` when
the plan has a dated leg), and a manifest (``manifest.json``)
naming the bundle format and the SHA-256 + byte size of every other
file in the archive.

The bundle doctrine matches the rest of the project:
**verification is recomputed, never claimed.** The verification
report inside the bundle is derived on the spot from the plan's
sealed evidence (hash chain + deterministic checker, primary items
and alternatives together); the record's own verdict is echoed
only as an untrusted claim. A tampered plan still bundles — its
report reads NOT VERIFIED and its itineraries carry the NOT
VERIFIED banner — because the honest archive is the point, exactly
as a tampered plan still exports. And the bundle is itself
auditable offline: :func:`verify_bundle` re-opens an archive,
checks every file against the manifest's hashes (a ``plan.json``
edited after packing fails the manifest check even if the edited
plan would verify on its own), and recomputes the packed plan's
verdict from its sealed record, reporting both results.

Bundles are deterministic: the same plan packs byte-identically
every time — fixed ZIP entry timestamps, a fixed entry order, and
no wall-clock anywhere (the manifest carries content hashes, never
a packing time) — so two bundles of the same plan compare equal
byte for byte.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from .checker import item_reports
from .evidence import qloo_responses
from .export import recomputed_verdict, render_ics, render_markdown, \
    render_text

BUNDLE_FORMAT = "tastepilot-audit-bundle"
BUNDLE_VERSION = 1

# The DOS epoch: the earliest timestamp a ZIP entry can carry, used
# for every entry so packing never embeds a wall-clock moment.
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _canonical_json(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False,
                       indent=1) + "\n").encode("utf-8")


def bundle_verification(plan) -> dict:
    """The verification report packed into a bundle: the recomputed
    verdict (primary items + alternatives), the per-item checker
    reports behind it, and the sealed-response count. Any malformed
    shape is simply NOT VERIFIED, with empty report lists."""
    verdict = recomputed_verdict(plan)
    report = {"passed": verdict["passed"],
              "chain_valid": verdict["chain_valid"],
              "violations": verdict["violations"],
              "record_verdict_claimed": verdict["record_verdict"],
              "items": [], "alternatives": [],
              "item_count": 0, "alternative_count": 0,
              "sealed_responses": 0}
    if not isinstance(plan, dict):
        return report
    record = plan.get("evidence")
    items = plan.get("items")
    alternatives = plan.get("alternatives")
    if not isinstance(record, dict) or not isinstance(items, list) or \
            not isinstance(alternatives, list):
        return report
    try:
        report["items"] = item_reports(items, record)
        report["alternatives"] = item_reports(alternatives, record)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return report
    report["item_count"] = len(items)
    report["alternative_count"] = len(alternatives)
    report["sealed_responses"] = len(qloo_responses(record))
    return report


def build_bundle(plan) -> tuple[bytes, bool]:
    """Pack ``plan`` into an audit bundle.

    Returns ``(zip_bytes, verified)`` where ``verified`` is the
    recomputed verdict at packing time. Raises a categorised
    ``ValueError("bad_plan")`` for a non-object plan — every other
    plan, verified or not, packs."""
    if not isinstance(plan, dict):
        raise ValueError("bad_plan")
    verification = bundle_verification(plan)
    record = plan.get("evidence")
    files: list[tuple[str, bytes]] = [
        ("plan.json", _canonical_json(plan)),
        ("evidence.json", _canonical_json(record)
         if isinstance(record, dict) else b"{}\n"),
        ("verification.json", _canonical_json(verification)),
        ("itinerary.md", render_markdown(plan).encode("utf-8")),
        ("itinerary.txt", render_text(plan).encode("utf-8")),
    ]
    try:
        files.append(("itinerary.ics",
                      render_ics(plan).encode("utf-8")))
    except ValueError:
        # No dated leg: the calendar is omitted rather than assigned
        # invented dates, and the manifest says so.
        pass
    manifest = {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION,
                "verified": verification["passed"],
                "chain_valid": verification["chain_valid"],
                "has_calendar": any(name == "itinerary.ics"
                                    for name, _ in files),
                "files": [{"name": name,
                           "sha256": hashlib.sha256(data).hexdigest(),
                           "bytes": len(data)}
                          for name, data in files]}
    files.append(("manifest.json", _canonical_json(manifest)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue(), verification["passed"]


def verify_bundle(data) -> dict:
    """Re-audit an audit bundle offline.

    Returns ``{"ok", "manifest_valid", "passed", "chain_valid",
    "violations", "files"}``: ``manifest_valid`` is True iff the
    archive opens, its manifest is a well-formed bundle manifest,
    and every listed file is present with a matching SHA-256 and
    byte size (no unlisted files either); ``passed`` /
    ``chain_valid`` / ``violations`` are the packed plan's verdict
    recomputed from its sealed record — never read from the packed
    ``verification.json``, which is itself only a claim once the
    archive leaves TastePilot's hands. A non-ZIP or truncated input
    is a categorised ``ValueError("bad_bundle")``, never a
    traceback."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("bad_bundle")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError,
            UnicodeDecodeError, RuntimeError, EOFError, OSError):
        raise ValueError("bad_bundle") from None
    if not isinstance(manifest, dict) or \
            manifest.get("format") != BUNDLE_FORMAT or \
            not isinstance(manifest.get("files"), list):
        raise ValueError("bad_bundle")
    listed = {}
    manifest_valid = True
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or \
                not isinstance(entry.get("name"), str) or \
                not isinstance(entry.get("sha256"), str) or \
                not isinstance(entry.get("bytes"), int):
            raise ValueError("bad_bundle")
        listed[entry["name"]] = entry
    if set(names) != set(listed) | {"manifest.json"}:
        manifest_valid = False
    contents = {}
    for name, entry in listed.items():
        try:
            blob = archive.read(name)
        except (KeyError, zipfile.BadZipFile, RuntimeError, EOFError,
                OSError):
            manifest_valid = False
            continue
        contents[name] = blob
        if len(blob) != entry["bytes"] or \
                hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            manifest_valid = False
    try:
        plan = json.loads(contents["plan.json"])
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("bad_bundle") from None
    if not isinstance(plan, dict):
        raise ValueError("bad_bundle")
    verdict = recomputed_verdict(plan)
    return {"ok": manifest_valid and verdict["passed"],
            "manifest_valid": manifest_valid,
            "passed": verdict["passed"],
            "chain_valid": verdict["chain_valid"],
            "violations": verdict["violations"],
            "files": sorted(contents)}
