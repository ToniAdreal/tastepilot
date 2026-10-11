"""Take-away itinerary exports: Markdown, plain text, iCalendar.

Until now a plan lived only as JSON (for the checker, the verify
panel, and share links) or as the demo page. This module renders a
plan into the formats a person actually takes away — a Markdown
itinerary for notes/docs, a plain-text version for anywhere, and an
iCalendar (.ics) file with one all-day event per dated leg.

The export doctrine matches the rest of the project: **verification
is recomputed, never claimed.** Every export carries a verification
banner derived on the spot from the plan's sealed evidence record
(hash chain + deterministic checker); the record's own verdict
field is echoed only as an untrusted claim. A tampered plan exports
with a loud NOT VERIFIED banner and its violations listed — the
exporter never launders a failing plan into a clean itinerary, and
it never refuses to export one either: the honest banner is the
point, exactly as a shared tampered plan still FAILs by link.

Exports are deterministic: the same plan renders byte-identically
every time (no wall-clock timestamps anywhere; the iCalendar
DTSTAMP is derived from the leg's own start date, and each event's
UID is a hash of the plan content, the leg, and its dates).

iCalendar scope: one VEVENT per leg that has a start date (all-day,
DTEND exclusive per RFC 5545), summarising that leg's items and the
recomputed verdict. A plan with no dated legs has nothing to put on
a calendar and raises ``no_dated_legs`` instead of inventing dates.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from .checker import check_plan, plan_recommendations
from .timeline import branch_timeline

FORMATS = ("markdown", "text", "ics")
TIMELINE_FORMATS = ("markdown", "text", "ics")

_MEDIA = {
    "markdown": ("text/markdown; charset=utf-8", "md"),
    "text": ("text/plain; charset=utf-8", "txt"),
    "ics": ("text/calendar; charset=utf-8", "ics"),
}


def media_for(fmt: str) -> tuple[str, str]:
    """(content type, file extension) for an export format."""
    try:
        return _MEDIA[fmt]
    except KeyError:
        raise ValueError(f"unsupported: unknown export format {fmt!r}") \
            from None


def recomputed_verdict(plan) -> dict:
    """Recompute {passed, chain_valid, violations, record_verdict}
    from a plan's sealed evidence — the only verdict an export ever
    prints. Any malformed shape is simply NOT VERIFIED."""
    if not isinstance(plan, dict):
        return {"passed": False, "chain_valid": False,
                "violations": [{"item": None,
                                "reason": "missing_or_malformed_evidence"}],
                "record_verdict": None}
    record = plan.get("evidence")
    items = plan.get("items") or []
    if not isinstance(record, dict) or not isinstance(items, list) or \
            not isinstance(plan.get("alternatives") or [], list):
        return {"passed": False, "chain_valid": False,
                "violations": [{"item": None,
                                "reason": "missing_or_malformed_evidence"}],
                "record_verdict": record.get("verdict")
                if isinstance(record, dict) else None}
    try:
        # Alternatives are recommendations too: the banner's verdict
        # covers primary items and alternatives together.
        report = check_plan(plan_recommendations(plan), record)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return {"passed": False, "chain_valid": False,
                "violations": [{"item": None,
                                "reason": "missing_or_malformed_evidence"}],
                "record_verdict": record.get("verdict")}
    return {"passed": bool(report["passed"]),
            "chain_valid": bool(report["chain_valid"]),
            "violations": report["violations"],
            "record_verdict": record.get("verdict")}


def _one_line(value) -> str:
    """Collapse a value to a single line of plain text."""
    return " ".join(str(value if value is not None else "").split())


def _banner_lines(plan, verdict) -> list[str]:
    items = plan.get("items") or []
    if verdict["passed"]:
        head = (f"Verification: PASS — evidence chain valid; "
                f"{len(items)} recommendation(s), every one traced to "
                f"a Qloo response.")
    else:
        reasons = ", ".join(
            v["reason"] if v.get("item") is None
            else f"{v['item']}: {v['reason']}"
            for v in verdict["violations"]) or "unknown"
        head = (f"Verification: NOT VERIFIED — this plan FAILS its "
                f"evidence check ({reasons}). Do not trust its "
                f"recommendations.")
    claimed = verdict["record_verdict"]
    if claimed is not None:
        head += (f" (Record's own claimed verdict: {claimed} — "
                 f"untrusted, never used.)")
    return [head]


def _schedule_lines(plan) -> list[str]:
    lines = []
    for entry in plan.get("schedule") or []:
        if entry.get("start"):
            when = entry["start"]
            if entry.get("end"):
                when += f" to {entry['end']}"
            if entry.get("nights") is not None:
                when += f" ({entry['nights']} nights)"
            lines.append(f"{entry.get('location') or 'Anywhere'}: {when}")
    for warning in plan.get("date_warnings") or []:
        lines.append(f"Schedule warning [{warning.get('code')}]: "
                     f"{warning.get('detail')}")
    for transfer in plan.get("transfers") or []:
        lines.append(f"Transfer: {transfer.get('detail')} "
                     f"(status: {transfer.get('status')}, "
                     f"source: {transfer.get('source', 'offline_estimate')})")
    for warning in plan.get("travel_warnings") or []:
        lines.append(f"Travel warning [{warning.get('code')}]: "
                     f"{warning.get('detail')}")
    return lines


def _provenance_lines(plan) -> list[str]:
    lines = [f"Mode: {plan.get('mode', 'mock')}; "
             f"scenario: {plan.get('scenario', 'generic')}"]
    if plan.get("seeds"):
        lines.append("Seeds: " + ", ".join(_one_line(s)
                                           for s in plan["seeds"]))
    for seed_list in plan.get("seed_lists") or []:
        lines.append(f"Seed list ({seed_list.get('label')}): "
                     + ", ".join(_one_line(i)
                                 for i in seed_list.get("items", [])))
    for imp in plan.get("imports") or []:
        lines.append(f"Import: {imp.get('source_file')} "
                     f"({imp.get('format')}) — "
                     f"{len(imp.get('items', []))} seed(s)")
    for tt in plan.get("timetable") or []:
        lines.append(f"Timetable: {tt.get('source_file')} "
                     f"({tt.get('format')}) — {tt.get('entries')} "
                     f"service(s)")
    if plan.get("exclusions"):
        lines.append("Exclusions: " + ", ".join(
            _one_line(e) for e in plan["exclusions"]))
    explainer = plan.get("explainer") or {}
    if explainer:
        lines.append(f"Explanations by: {explainer.get('source', 'template')}"
                     + (f" ({explainer['model']})"
                        if explainer.get("model") else "")
                     + f"; {explainer.get('llm_written', 0)} LLM-written, "
                       f"{explainer.get('template_written', 0)} template, "
                       f"{explainer.get('fallback', 0)} fallback")
    return lines


def _swap_history_lines(plan) -> list[str]:
    """The sealed swap audit trail, recomputed from the evidence
    record (swap.swap_history) — a ``swaps``/``swap_history`` field
    stored in the plan JSON is never read back. A plan whose chain
    does not verify yields no lines: its banner already says NOT
    VERIFIED, and an audit trail read from a broken chain would be
    a claim, not an audit."""
    from .swap import swap_history
    try:
        events = swap_history(plan)
    except ValueError:
        return []
    lines = []
    for event in events:
        primary = event.get("primary") or {}
        alternative = event.get("alternative") or {}
        line = (f"#{event.get('index')} [{primary.get('domain', '')}] "
                f"{primary.get('name', '')} -> "
                f"{alternative.get('name', '')} "
                f"(swap {(event.get('swap_id') or '')[:8]}, sealed; "
                f"record before: "
                f"{(event.get('record_hash_before') or '')[:12]})")
        if event.get("is_undo"):
            line += f" — undo of swap {(event.get('undo_of') or '')[:8]}"
        lines.append(line)
    return lines


def _handoff_lines(plan) -> list[tuple[str, str]]:
    """(label, url) handoff links, recomputed from the plan itself
    (handoff.plan_handoffs) — a ``handoffs`` field stored in the
    plan JSON is never trusted or read back."""
    from .handoff import plan_handoffs
    handoffs = plan_handoffs(plan)
    lines: list[tuple[str, str]] = []
    for leg in handoffs["legs"]:
        where = leg.get("location") or "Anywhere"
        if leg.get("calendar_url"):
            lines.append((f"Add the {where} leg to Google Calendar",
                          leg["calendar_url"]))
        if leg.get("maps_url"):
            lines.append((f"Open {where} in Google Maps",
                          leg["maps_url"]))
        if leg.get("maps_apple_url"):
            lines.append((f"Open {where} in Apple Maps",
                          leg["maps_apple_url"]))
    for item in handoffs["items"]:
        lines.append((f"{item['name']} in Google Maps",
                      item["maps_url"]))
    return lines


def _itinerary_lines(plan) -> list[str]:
    """Day-by-day pacing lines, recomputed from the plan itself
    (pacing.plan_itinerary) — an ``itinerary`` field stored in
    the plan JSON is never trusted or read back. One line per
    day bucket: dated days name their date; an undated leg reads
    as a single unscheduled bucket. Empty when the plan has no
    items to pace."""
    from .pacing import plan_itinerary
    itinerary = plan_itinerary(plan)
    lines: list[str] = []
    for leg in itinerary["legs"]:
        where = leg.get("location") or "Anywhere"
        for day in leg["days"]:
            if not day["items"]:
                continue
            label = day["date"] or "unscheduled"
            names = ", ".join(i["name"] for i in day["items"])
            lines.append(f"{where} · {label}: {names}")
    return lines


def _leg_sections(plan) -> list[tuple[str, list[tuple[str, list[dict]]]]]:
    """[(leg heading, [(domain, items), ...]), ...] in plan order.

    Multi-leg plans use their per-leg groups; anything else falls
    back to the flat domain groups (a single section)."""
    legs = plan.get("legs") or []
    sections = []
    if len(legs) > 1:
        for leg in legs:
            heading = leg.get("location") or "Anywhere"
            if leg.get("start"):
                heading += f" ({leg['start']}"
                if leg.get("end"):
                    heading += f" to {leg['end']}"
                if leg.get("nights") is not None:
                    heading += f", {leg['nights']} nights"
                heading += ")"
            sections.append((heading, [(g["domain"], g["items"])
                                       for g in leg.get("groups", [])]))
        return sections
    groups = plan.get("groups") or []
    if not groups and plan.get("items"):
        by_domain: dict[str, list[dict]] = {}
        for item in plan["items"]:
            by_domain.setdefault(item.get("domain", "other"),
                                 []).append(item)
        groups = [{"domain": d, "items": items}
                  for d, items in by_domain.items()]
    heading = plan.get("location") or "Itinerary"
    return [(heading, [(g["domain"], g["items"]) for g in groups])]


def _item_lines(item) -> tuple[str, str, str]:
    name = _one_line(item.get("entity", {}).get("name", "Untitled"))
    affinity = item.get("affinity")
    head = name if affinity is None else f"{name} (affinity {affinity:.2f})"
    return head, _one_line(item.get("why", "")), \
        _one_line(item.get("evidence_ref", ""))


def render_markdown(plan: dict) -> str:
    verdict = recomputed_verdict(plan)
    out = ["# TastePilot itinerary", ""]
    if plan.get("request"):
        out += [f"> {_one_line(plan['request'])}", ""]
    out += [f"> {_banner_lines(plan, verdict)[0]}", ""]
    schedule = _schedule_lines(plan)
    if schedule:
        out += ["## Schedule and transfers", ""]
        out += [f"- {line}" for line in schedule] + [""]
    for heading, groups in _leg_sections(plan):
        out += [f"## {heading}", ""]
        for domain, items in groups:
            out += [f"### {domain}", ""]
            for item in items:
                head, why, ref = _item_lines(item)
                out.append(f"- **{head}**")
                if why:
                    out.append(f"  Why this fits: {why}")
                if ref:
                    out.append(f"  Evidence: `{ref}`")
            out.append("")
    rejected = plan.get("rejected_details") or []
    if rejected:
        out += ["## Rejected by the evidence checker", ""]
        out += [f"- {r.get('name')} ({r.get('domain')}): {r.get('reason')}"
                for r in rejected] + [""]
    excluded = plan.get("excluded") or []
    if excluded:
        out += ["## Excluded by your request", ""]
        out += [f"- {r.get('name')} ({r.get('domain')}): matched "
                f"exclusion '{r.get('matched')}' — evidence-backed "
                f"(verification: {r.get('verification')}), removed by "
                f"preference, not by the checker"
                for r in excluded] + [""]
    alternatives = plan.get("alternatives") or []
    if alternatives:
        out += ["## Alternatives (evidence-backed swap options)", ""]
        for item in alternatives:
            head, why, ref = _item_lines(item)
            out.append(f"- **{head}** ({item.get('domain', '')}"
                       f"{', ' + item['location'] if item.get('location') else ''})"
                       f" — alternative, checker: "
                       f"{(item.get('verification') or {}).get('reason', 'unverified')}")
            if why:
                out.append(f"  Why this fits: {why}")
            if ref:
                out.append(f"  Evidence: `{ref}`")
        out.append("")
    swap_history = _swap_history_lines(plan)
    if swap_history:
        out += ["## Swap history (sealed audit trail)", ""]
        out += [f"- {line}" for line in swap_history] + [""]
    itinerary = _itinerary_lines(plan)
    if itinerary:
        out += ["## Day-by-day itinerary", ""]
        out += [f"- {line}" for line in itinerary] + [""]
    handoffs = _handoff_lines(plan)
    if handoffs:
        out += ["## Handoffs (your own calendar / maps)", ""]
        out += [f"- [{label}]({url})" for label, url in handoffs] + [""]
    out += ["## Provenance", ""]
    out += [f"- {line}" for line in _provenance_lines(plan)]
    out += ["",
            "Re-verify this plan: `python3 -m tastepilot verify "
            "--record <plan.json>` — editing any affinity makes "
            "verification fail."]
    return "\n".join(out) + "\n"


def render_text(plan: dict) -> str:
    verdict = recomputed_verdict(plan)
    out = ["TastePilot itinerary", "=" * 22, ""]
    if plan.get("request"):
        out += [_one_line(plan["request"]), ""]
    out += [_banner_lines(plan, verdict)[0], ""]
    schedule = _schedule_lines(plan)
    if schedule:
        out += ["Schedule and transfers", "-" * 24]
        out += schedule + [""]
    for heading, groups in _leg_sections(plan):
        out += [heading, "-" * len(heading)]
        for domain, items in groups:
            out.append(domain.capitalize() + ":")
            for item in items:
                head, why, ref = _item_lines(item)
                out.append(f"  * {head}")
                if why:
                    out.append(f"    Why this fits: {why}")
                if ref:
                    out.append(f"    Evidence: {ref}")
            out.append("")
    rejected = plan.get("rejected_details") or []
    if rejected:
        out += ["Rejected by the evidence checker:"]
        out += [f"  * {r.get('name')} ({r.get('domain')}): "
                f"{r.get('reason')}" for r in rejected] + [""]
    excluded = plan.get("excluded") or []
    if excluded:
        out += ["Excluded by your request:"]
        out += [f"  * {r.get('name')} ({r.get('domain')}): matched "
                f"exclusion '{r.get('matched')}' — evidence-backed "
                f"(verification: {r.get('verification')}), removed by "
                f"preference, not by the checker"
                for r in excluded] + [""]
    alternatives = plan.get("alternatives") or []
    if alternatives:
        out += ["Alternatives (evidence-backed swap options):"]
        for item in alternatives:
            head, why, ref = _item_lines(item)
            out.append(f"  * {head} ({item.get('domain', '')}"
                       f"{', ' + item['location'] if item.get('location') else ''})"
                       f" — alternative, checker: "
                       f"{(item.get('verification') or {}).get('reason', 'unverified')}")
            if why:
                out.append(f"    Why this fits: {why}")
            if ref:
                out.append(f"    Evidence: {ref}")
        out.append("")
    swap_history = _swap_history_lines(plan)
    if swap_history:
        out += ["Swap history (sealed audit trail):"]
        out += [f"  * {line}" for line in swap_history] + [""]
    itinerary = _itinerary_lines(plan)
    if itinerary:
        out += ["Day-by-day itinerary:"]
        out += [f"  * {line}" for line in itinerary] + [""]
    handoffs = _handoff_lines(plan)
    if handoffs:
        out += ["Handoffs (your own calendar / maps):"]
        out += [f"  {label}: {url}" for label, url in handoffs] + [""]
    out += ["Provenance:"]
    out += [f"  {line}" for line in _provenance_lines(plan)]
    out += ["", "Re-verify this plan with: python3 -m tastepilot "
                "verify --record <plan.json>"]
    return "\n".join(out) + "\n"


def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> list[str]:
    """Fold a content line to <=75 octets (RFC 5545 §3.1) without
    splitting a UTF-8 sequence; continuations start with one space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    parts, limit = [], 75
    while raw:
        chunk = raw[:limit]
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        raw = raw[len(chunk):]
        limit = 74  # continuation lines spend one octet on the space
    return [parts[0]] + [" " + p for p in parts[1:]]


def render_ics(plan: dict) -> str:
    verdict = recomputed_verdict(plan)
    dated = [e for e in plan.get("schedule") or [] if e.get("start")]
    if not dated:
        raise ValueError("no_dated_legs: this plan has no leg with a "
                         "start date to put on a calendar")
    # The UID fingerprint deliberately excludes the evidence record:
    # it carries a wall-clock created_at, so hashing the whole plan
    # would change UIDs between two regenerations of the same plan.
    # Content (request + item ids/affinities) is what identifies it.
    fingerprint = {
        "request": plan.get("request"),
        "items": [[i.get("entity", {}).get("id"), i.get("domain"),
                   i.get("location"), i.get("affinity")]
                  for i in plan.get("items") or []],
    }
    canonical = json.dumps(fingerprint, sort_keys=True,
                           ensure_ascii=False,
                           separators=(",", ":")).encode()
    plan_hash = hashlib.sha256(canonical).hexdigest()[:24]
    items = plan.get("items") or []
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//TastePilot//Itinerary Export//EN",
             "CALSCALE:GREGORIAN"]
    for entry in dated:
        location = entry.get("location") or "Anywhere"
        start = date.fromisoformat(entry["start"])
        end_raw = entry.get("end") or entry["start"]
        end = date.fromisoformat(end_raw) + timedelta(days=1)
        uid_src = f"{plan_hash}:{location}:{entry['start']}:{end_raw}"
        uid = hashlib.sha256(uid_src.encode()).hexdigest()[:32]
        leg_items = [i for i in items
                     if (i.get("location") or None) ==
                     (entry.get("location") or None)] or items
        desc = [_banner_lines(plan, verdict)[0], "", "Recommendations:"]
        for item in leg_items:
            head, _, _ = _item_lines(item)
            desc.append(f"- {head} [{item.get('domain', '')}]")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@tastepilot",
            f"DTSTAMP:{start.strftime('%Y%m%d')}T000000Z",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
            "SUMMARY:" + _ics_escape(f"TastePilot: {location}"),
            "DESCRIPTION:" + _ics_escape("\n".join(desc)),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_ics_fold(line))
    return "\r\n".join(folded) + "\r\n"


def _timeline_cell_text(cell) -> str:
    status = cell.get("status", "absent")
    affinity = cell.get("affinity")
    if affinity is None:
        return status
    text = f"{status} ({float(affinity):.2f})"
    if status == "rescored" and cell.get("delta") is not None:
        text = f"rescored ({float(affinity):.2f}, {float(cell['delta']):+.2f})"
    return text


def _timeline_banner(doc) -> str:
    columns = doc.get("columns") or []
    if doc.get("summary", {}).get("all_passed"):
        return (f"Verification: PASS — every one of the "
                f"{len(columns)} timeline column(s) recomputes as "
                f"PASS from its own sealed evidence record.")
    failing = ", ".join(c.get("name", "?") for c in columns
                        if not c.get("passed")) or "unknown"
    return (f"Verification: NOT VERIFIED — timeline column(s) "
            f"FAIL their recomputed evidence check: {failing}. "
            f"Do not trust those columns' recommendations.")


def _timeline_doc(base, branches):
    return branch_timeline(base, branches)


def render_timeline_markdown(base, branches) -> str:
    """Render a branch timeline (base + branches) as a Markdown
    take-away: one table per leg, slots as rows, branches as
    columns, every column headed by its recomputed verdict."""
    doc = _timeline_doc(base, branches)
    out = ["# TastePilot branch timeline", ""]
    if isinstance(base, dict) and base.get("request"):
        out += [f"> {_one_line(base['request'])}", ""]
    out += [f"> {_timeline_banner(doc)}", ""]
    out += ["Columns: " + " · ".join(
        f"{c['name']} ({'PASS' if c['passed'] else 'FAIL'}, "
        f"{c['item_count']} item(s), {c['swap_count']} swap(s))"
        for c in doc["columns"]), ""]
    for leg in doc["legs"]:
        where = leg.get("location") or "Anywhere"
        out += [f"## Leg: {where}", ""]
        header = ["Recommendation", "Domain"] + \
            [_one_line(c["name"]).replace("|", "\\|")
             for c in doc["columns"]]
        out.append("| " + " | ".join(header) + " |")
        out.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in leg["rows"]:
            cells = [_one_line(row.get("name") or "").replace("|", "\\|"),
                     _one_line(row.get("domain") or "")]
            cells += [_timeline_cell_text(c) for c in row["cells"]]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    out += ["Re-verify each plan in this timeline with: "
            "`python3 -m tastepilot verify --record <plan.json>` — "
            "a column whose plan was edited FAILs here, never "
            "renders clean."]
    return "\n".join(out) + "\n"


def render_timeline_text(base, branches) -> str:
    """Plain-text branch timeline: no Markdown, one line per slot
    with every column's status on that line."""
    doc = _timeline_doc(base, branches)
    out = ["TastePilot branch timeline", "=" * 26, ""]
    if isinstance(base, dict) and base.get("request"):
        out += [_one_line(base["request"]), ""]
    out += [_timeline_banner(doc), ""]
    out += ["Columns: " + " | ".join(
        f"{c['name']} ({'PASS' if c['passed'] else 'FAIL'})"
        for c in doc["columns"]), ""]
    for leg in doc["legs"]:
        heading = f"Leg: {leg.get('location') or 'Anywhere'}"
        out += [heading, "-" * len(heading)]
        for row in leg["rows"]:
            cells = " | ".join(
                f"{c['name']}: {_timeline_cell_text(cell)}"
                for c, cell in zip(doc["columns"], row["cells"]))
            out.append(f"  * {_one_line(row.get('name') or '')} "
                       f"[{row.get('domain', '')}] — {cells}")
        out.append("")
    out += ["Re-verify each plan with: python3 -m tastepilot "
            "verify --record <plan.json>"]
    return "\n".join(out) + "\n"


def render_timeline_ics(base, branches) -> str:
    """Render a branch timeline as iCalendar: one event series per
    column (the base plan plus each branch) — for every dated leg
    in that column's own schedule, one all-day VEVENT whose summary
    names the branch and whose description carries that column's
    recomputed verdict and that leg's recommendations in that
    branch, so a recipient can compare swap paths on their own
    calendar (same dates, different items per branch).

    Doctrine matches every other export: each column's verdict is
    recomputed from its own sealed record — a tampered branch's
    events read NOT VERIFIED, never PASS. Deterministic: UIDs hash
    the column name + plan content (request + item ids/affinities,
    deliberately excluding the evidence record's wall-clock) + leg
    + dates, and DTSTAMP derives from the leg start, exactly as
    the single-plan ICS exporter. A timeline in which no column
    has any dated leg raises ``no_dated_legs`` instead of
    inventing dates; a column with no dated legs contributes no
    events while dated columns still render."""
    doc = _timeline_doc(base, branches)
    plans = [base] + [b["plan"] for b in branches]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//TastePilot//Timeline Export//EN",
             "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:TastePilot branch timeline"]
    events = 0
    for column, plan in zip(doc["columns"], plans):
        verdict = recomputed_verdict(plan)
        dated = [e for e in plan.get("schedule") or [] if e.get("start")]
        if not dated:
            continue
        fingerprint = {
            "branch": column["name"],
            "request": plan.get("request"),
            "items": [[i.get("entity", {}).get("id"), i.get("domain"),
                       i.get("location"), i.get("affinity")]
                      for i in plan.get("items") or []],
        }
        canonical = json.dumps(fingerprint, sort_keys=True,
                               ensure_ascii=False,
                               separators=(",", ":")).encode()
        plan_hash = hashlib.sha256(canonical).hexdigest()[:24]
        items = plan.get("items") or []
        for entry in dated:
            location = entry.get("location") or "Anywhere"
            start = date.fromisoformat(entry["start"])
            end_raw = entry.get("end") or entry["start"]
            end = date.fromisoformat(end_raw) + timedelta(days=1)
            uid_src = (f"{plan_hash}:{location}:{entry['start']}:"
                       f"{end_raw}")
            uid = hashlib.sha256(uid_src.encode()).hexdigest()[:32]
            leg_items = [i for i in items
                         if (i.get("location") or None) ==
                         (entry.get("location") or None)] or items
            desc = [f"Branch: {column['name']} "
                    f"({'PASS' if verdict['passed'] else 'FAIL'} — "
                    f"recomputed from this plan's sealed evidence)",
                    _banner_lines(plan, verdict)[0], "",
                    "Recommendations:"]
            for item in leg_items:
                head, _, _ = _item_lines(item)
                desc.append(f"- {head} [{item.get('domain', '')}]")
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}@tastepilot-timeline",
                f"DTSTAMP:{start.strftime('%Y%m%d')}T000000Z",
                f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
                "SUMMARY:" + _ics_escape(
                    f"TastePilot [{column['name']}]: {location}"),
                "DESCRIPTION:" + _ics_escape("\n".join(desc)),
                "END:VEVENT",
            ]
            events += 1
    if not events:
        raise ValueError("no_dated_legs: no timeline column has a leg "
                         "with a start date to put on a calendar")
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_ics_fold(line))
    return "\r\n".join(folded) + "\r\n"


MAX_MULTI_TIMELINES = 6


def validate_multi_timelines(timelines) -> None:
    """Shape-gate a multi-timeline bundle; categorised ValueError.

    A bundle is a list of 1..MAX_MULTI_TIMELINES timelines, each
    {"name": str (1-60 chars, unique), "base": {...plan...},
    "branches": [{"name", "plan"}, ...]} — exactly the /api/timeline
    payload shape, named. Each timeline's own shape is proved by
    branch_timeline when the bundle renders; this gate checks the
    bundle level only (count, names, container shapes)."""
    if not isinstance(timelines, list) or not timelines:
        raise ValueError("bad_timelines")
    if len(timelines) > MAX_MULTI_TIMELINES:
        raise ValueError("too_many_timelines")
    names = []
    for timeline in timelines:
        if not isinstance(timeline, dict):
            raise ValueError("bad_timelines")
        name = timeline.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("bad_timelines")
        if name.strip() in names:
            raise ValueError("duplicate_timeline_name")
        names.append(name.strip())
        if not isinstance(timeline.get("base"), dict) or \
                not isinstance(timeline.get("branches"), list):
            raise ValueError("bad_timelines")


def render_multi_timeline_ics(timelines) -> tuple[str, list[dict]]:
    """Render a bundle of branch timelines as ONE iCalendar file:
    for every timeline, one event series per column (base + each
    branch), exactly as render_timeline_ics renders a single
    timeline — except summaries name timeline and branch
    ("TastePilot [Trip / Museum]: Kyoto") and UIDs hash the
    timeline name too, so the same leg in two timelines never
    collides. Returns (content, summaries) where summaries carry
    each timeline's recomputed all_passed + event count.

    Every verdict is recomputed per plan from its own sealed
    record; a tampered branch's series reads NOT VERIFIED. A
    timeline with no dated leg contributes no events; a bundle
    with no dated leg anywhere raises no_dated_legs instead of
    inventing dates. Deterministic, like every export."""
    validate_multi_timelines(timelines)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//TastePilot//Multi Timeline Export//EN",
             "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:TastePilot multi-timeline bundle"]
    summaries: list[dict] = []
    total_events = 0
    for timeline in timelines:
        tname = timeline["name"].strip()
        base, branches = timeline["base"], timeline["branches"]
        doc = _timeline_doc(base, branches)  # validates + verdicts
        plans = [base] + [b["plan"] for b in branches]
        events = 0
        for column, plan in zip(doc["columns"], plans):
            verdict = recomputed_verdict(plan)
            dated = [e for e in plan.get("schedule") or []
                     if e.get("start")]
            if not dated:
                continue
            fingerprint = {
                "timeline": tname, "branch": column["name"],
                "request": plan.get("request"),
                "items": [[i.get("entity", {}).get("id"),
                           i.get("domain"), i.get("location"),
                           i.get("affinity")]
                          for i in plan.get("items") or []],
            }
            canonical = json.dumps(
                fingerprint, sort_keys=True, ensure_ascii=False,
                separators=(",", ":")).encode()
            plan_hash = hashlib.sha256(canonical).hexdigest()[:24]
            items = plan.get("items") or []
            for entry in dated:
                location = entry.get("location") or "Anywhere"
                start = date.fromisoformat(entry["start"])
                end_raw = entry.get("end") or entry["start"]
                end = date.fromisoformat(end_raw) + timedelta(days=1)
                uid_src = (f"{plan_hash}:{location}:"
                           f"{entry['start']}:{end_raw}")
                uid = hashlib.sha256(uid_src.encode()).hexdigest()[:32]
                leg_items = [i for i in items
                             if (i.get("location") or None) ==
                             (entry.get("location") or None)] or items
                desc = [f"Timeline: {tname}",
                        f"Branch: {column['name']} "
                        f"({'PASS' if verdict['passed'] else 'FAIL'} — "
                        f"recomputed from this plan's sealed evidence)",
                        _banner_lines(plan, verdict)[0], "",
                        "Recommendations:"]
                for item in leg_items:
                    head, _, _ = _item_lines(item)
                    desc.append(f"- {head} [{item.get('domain', '')}]")
                lines += [
                    "BEGIN:VEVENT",
                    f"UID:{uid}@tastepilot-multi-timeline",
                    f"DTSTAMP:{start.strftime('%Y%m%d')}T000000Z",
                    f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                    f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
                    "SUMMARY:" + _ics_escape(
                        f"TastePilot [{tname} / {column['name']}]: "
                        f"{location}"),
                    "DESCRIPTION:" + _ics_escape("\n".join(desc)),
                    "END:VEVENT",
                ]
                events += 1
        summaries.append({
            "name": tname,
            "all_passed": bool(doc["summary"]["all_passed"]),
            "events": events,
            "columns": [c["name"] for c in doc["columns"]]})
        total_events += events
    if not total_events:
        raise ValueError("no_dated_legs: no timeline in the bundle "
                         "has a leg with a start date to put on "
                         "a calendar")
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_ics_fold(line))
    return "\r\n".join(folded) + "\r\n", summaries


def export_multi_timeline_ics(timelines) -> tuple[str, bool, list[dict]]:
    """Render a multi-timeline bundle as one iCalendar file;
    returns (content, all_passed, summaries) — all_passed is every
    timeline's every column recomputing as PASS. A tampered branch
    still renders, its series NOT VERIFIED — never clean."""
    content, summaries = render_multi_timeline_ics(timelines)
    return content, all(s["all_passed"] for s in summaries), summaries


_TIMELINE_RENDERERS = {"markdown": render_timeline_markdown,
                       "text": render_timeline_text,
                       "ics": render_timeline_ics}


def export_timeline(base, branches, fmt: str) -> tuple[str, bool]:
    """Render a branch timeline in *fmt* (markdown/text grids, or
    ics: one event series per branch); returns (content,
    all_passed) where all_passed is every column's recomputed
    verdict. A tampered branch still renders, as a FAIL column
    (markdown/text) or a NOT VERIFIED event series (ics) — never
    clean. Raises ValueError with a category for an unsupported
    format, a malformed base/branch (from branch_timeline), or
    (ics) a timeline with no dated leg in any column."""
    if fmt not in TIMELINE_FORMATS:
        raise ValueError(
            f"unsupported: timeline export format {fmt!r} "
            f"(markdown, text or ics only)")
    doc = _timeline_doc(base, branches)
    content = _TIMELINE_RENDERERS[fmt](base, branches)
    return content, bool(doc["summary"]["all_passed"])


_RENDERERS = {"markdown": render_markdown, "text": render_text,
              "ics": render_ics}


def export_plan(plan: dict, fmt: str) -> tuple[str, bool]:
    """Render *plan* in *fmt*; returns (content, verified).

    ``verified`` is the recomputed verdict — False contents still
    render, carrying their NOT VERIFIED banner. Raises ValueError
    with a category for an unknown format or (ics) a plan with no
    dated legs."""
    media_for(fmt)  # validates the format name first
    return _RENDERERS[fmt](plan), recomputed_verdict(plan)["passed"]
