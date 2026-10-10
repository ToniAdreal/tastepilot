"""TastePilot agent loop: parse -> plan -> Qloo tool calls -> compose
-> evidence-verify. Hand-rolled orchestration: transparent and testable.

Invariant: an item enters the final plan only if the deterministic
evidence checker can trace it to a sealed Qloo insights response.
"""
from __future__ import annotations

from . import checker
from .evidence import EvidenceLog
from .llm import TemplateExplainer
from .parser import DEFAULT_SEEDS, parse_request
from .qloo import MockQlooClient

ITEMS_PER_DOMAIN = 2
MAX_TEXT_CHARS = 2_000  # defense in depth: the server enforces the same cap


class TastePilotAgent:
    def __init__(self, qloo_client=None, explainer=None):
        self.qloo = qloo_client or MockQlooClient()
        self.explainer = explainer or TemplateExplainer()
        self.mode = getattr(self.qloo, "source", "mock")

    @property
    def explainer_info(self) -> dict:
        """Which explainer is live in this agent (demo/health surface)."""
        return {"source": getattr(self.explainer, "source", "template"),
                "model": getattr(self.explainer, "model", None)}

    def _resolve_seeds(self, parsed, log: EvidenceLog,
                       extra_names: list[str] | None = None) -> tuple[list[str], list[str]]:
        """Resolve named seeds to entity ids via Qloo search (logged)."""
        names = list(parsed.seed_names)
        for name in extra_names or []:
            if name not in names and name not in (parsed.locations or []):
                names.append(name)
        names = names or DEFAULT_SEEDS
        ids: list[str] = []
        resolved_names: list[str] = []
        for name in names:
            resp = self.qloo.search(name, limit=5)
            log.add_qloo_call(resp.to_dict())
            exact = next((r for r in resp.results
                          if r.entity.name.lower() == name.lower()), None)
            pick = exact or (resp.results[0] if resp.results else None)
            if pick and pick.entity.id not in ids:
                ids.append(pick.entity.id)
                resolved_names.append(pick.entity.name)
        return ids, resolved_names

    def run(self, text: str, reference_date=None, imported=None,
            timetables=None) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("text too long")
        # reference_date anchors relative dates ("tomorrow", "next
        # Friday") and year-less explicit dates; None = today. Tests
        # pass a fixed date so plans stay deterministic.
        # Timetable files (see timetable.py): scheduled durations from
        # files the user supplied replace the offline transfer
        # estimates for the city pairs they cover — composition
        # constraints only, never sent to Qloo. Provenance (basename +
        # format + entry count, never a path) is carried into the plan.
        from .timetable import provider_for
        timetable_prov: list[dict] = []
        for tt in timetables or []:
            timetable_prov.append({
                "source_file": tt.get("source_file", "timetable"),
                "format": tt.get("format", "unknown"),
                "entries": len(tt.get("entries", []))})
        parsed = parse_request(
            text, reference_date=reference_date,
            timetable_provider=provider_for(timetables) if timetables
            else None)
        # External taste-file imports (see imports.py): each imported
        # title becomes a seed through the SAME sealed search ->
        # insights -> checker path as a typed seed — an import grants
        # a title no trust, it only names it. Provenance (basename +
        # format + items, never a path) is carried into the plan.
        imports_prov: list[dict] = []
        extra_names: list[str] = []
        for imp in imported or []:
            imports_prov.append({
                "source_file": imp.get("source_file", "import"),
                "format": imp.get("format", "unknown"),
                "items": imp.get("items", [])})
            extra_names.extend(imp.get("seed_names", []))
        log = EvidenceLog(request_text=text, mode=self.mode)
        seed_ids, seed_names = self._resolve_seeds(parsed, log,
                                                   extra_names=extra_names)
        items: list[dict] = []
        llm_calls: list[dict] = []
        # Multi-city composition: one leg per mentioned city (mention
        # order), planned with the SAME resolved seed ids — the user's
        # taste carries across legs by construction, while each leg's
        # Qloo calls are location-filtered and sealed separately, so a
        # leg's items cite that leg's own responses. A request with no
        # city is a single location-less leg.
        leg_locations = parsed.locations or [None]
        if seed_ids:
            for leg_location in leg_locations:
                for domain in parsed.domains:
                    resp = self.qloo.insights(seed_ids, domain,
                                               limit=ITEMS_PER_DOMAIN + 2,
                                               location=leg_location)
                    log.add_qloo_call(resp.to_dict())
                    for scored in resp.results[:ITEMS_PER_DOMAIN]:
                        item = {"entity": scored.entity.to_dict(),
                                "domain": domain,
                                "location": leg_location,
                                "affinity": scored.affinity,
                                "shared_tags": list(scored.shared_tags),
                                "evidence_ref": resp.response_id}
                        why, meta = self.explainer.explain(seed_names, item)
                        item["why"] = why
                        item["explanation_source"] = meta.get("source")
                        # Full provenance, not just the source string: the UI
                        # and judges must see which model worded each "why"
                        # line — and, on fallback, that Nebius was attempted
                        # and why the deterministic template took over.
                        item["explanation"] = {
                            k: meta[k] for k in ("source", "model", "fallback_from",
                                                 "fallback_reason", "usage")
                            if meta.get(k) is not None}
                        if meta.get("source") != "template" or meta.get("fallback_from"):
                            llm_calls.append({"purpose": "explain",
                                              "item": scored.entity.name,
                                              "domain": domain,
                                              "location": leg_location, **meta})
                        items.append(item)
        for call in llm_calls:
            log.add_llm_call(call)
        # Seal with a provisional verdict, verify, then re-seal honestly:
        # the checker runs against the sealed record's Qloo responses.
        record = log.seal("UNVERIFIED")
        admissible, rejected, report = checker.enforce(items, record)
        verdict = "PASS" if report["passed"] and admissible else "FAIL"
        record = log.seal(verdict)
        # Re-run the checker against the final sealed record for the report.
        _, _, final_report = checker.enforce(admissible, record)
        rejected_report = checker.check_plan(items, record) if items else final_report
        # Per-item verification against the final record, so the UI can
        # show — for every card — exactly which Qloo response backs it.
        reports = checker.item_reports(items, record)
        # Keyed by evidence ref as well as entity: in a multi-city plan
        # the same entity legitimately appears once per leg, citing a
        # different (per-leg) Qloo response each time.
        by_key = {(r["entity_id"], r["item"],
                   (r["evidence"] or {}).get("response_id")): r
                  for r in reports}
        for item in admissible:
            rep = by_key.get((item["entity"]["id"], item["entity"]["name"],
                              item["evidence_ref"]))
            item["verification"] = rep
        rejected_details = [
            {"name": r["item"], "domain": r["domain"], "reason": r["reason"]}
            for r in reports if not r["admissible"]]
        # Plan timeline: items grouped by domain, in the parser's domain
        # order, so the UI renders sections instead of one flat list.
        groups = []
        for domain in parsed.domains:
            members = [i for i in admissible if i["domain"] == domain]
            if members:
                groups.append({"domain": domain, "items": members})
        # The same grouping per leg, in mention order, for multi-city
        # plans (a single-city plan has exactly one leg). A leg with no
        # admitted items is omitted, mirroring how empty domain groups
        # are omitted above.
        # Date info per leg location, from the parser's leg_dates
        # (aligned with parsed.locations; a location-less plan has a
        # single None entry). Dates are composition constraints only:
        # they never reach a Qloo call, so the evidence invariant is
        # untouched — but ordering/pacing problems are surfaced as
        # date_warnings rather than silently repaired.
        dates_by_location = {d["location"]: d for d in parsed.leg_dates}
        legs = []
        for leg_location in leg_locations:
            leg_groups = []
            for domain in parsed.domains:
                members = [i for i in admissible
                           if i["domain"] == domain
                           and i["location"] == leg_location]
                if members:
                    leg_groups.append({"domain": domain, "items": members})
            if leg_groups:
                leg = {"location": leg_location, "groups": leg_groups}
                info = dates_by_location.get(leg_location) or {}
                for key in ("start", "end", "nights"):
                    leg[key] = info.get(key)
                legs.append(leg)
        # Plan-level explainer summary over the admitted items only:
        # how many "why" lines the live LLM actually wrote, how many the
        # template wrote, and how many are recorded Nebius fallbacks.
        explainer_summary = {
            **self.explainer_info,
            "items_explained": len(admissible),
            "llm_written": sum(1 for i in admissible
                               if i["explanation"].get("source") != "template"),
            "template_written": sum(
                1 for i in admissible
                if i["explanation"].get("source") == "template"
                and not i["explanation"].get("fallback_from")),
            "fallback": sum(1 for i in admissible
                            if i["explanation"].get("fallback_from")),
        }
        return {
            "request": text,
            "scenario": parsed.scenario,
            "location": parsed.location,
            "locations": parsed.locations,
            "domains": parsed.domains,
            "seeds": seed_names,
            "seed_lists": parsed.seed_lists,
            "imports": imports_prov,
            "schedule": parsed.leg_dates,
            "date_warnings": parsed.date_warnings,
            "timetable": timetable_prov,
            "transfers": parsed.transfers,
            "travel_warnings": parsed.travel_warnings,
            "mode": self.mode,
            "explainer": explainer_summary,
            "items": admissible,
            "groups": groups,
            "legs": legs,
            "rejected": [i["entity"]["name"] for i in rejected],
            "rejected_details": rejected_details,
            "checker": {"passed": final_report["passed"] and bool(admissible),
                        "chain_valid": final_report["chain_valid"],
                        "violations": rejected_report["violations"],
                        "items": [by_key[(i["entity"]["id"],
                                           i["entity"]["name"],
                                           i["evidence_ref"])]
                                  for i in admissible]},
            "evidence": record,
        }
