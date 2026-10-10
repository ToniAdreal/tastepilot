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

    def _resolve_seeds(self, parsed, log: EvidenceLog) -> tuple[list[str], list[str]]:
        """Resolve named seeds to entity ids via Qloo search (logged)."""
        names = parsed.seed_names or DEFAULT_SEEDS
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

    def run(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("text too long")
        parsed = parse_request(text)
        log = EvidenceLog(request_text=text, mode=self.mode)
        seed_ids, seed_names = self._resolve_seeds(parsed, log)
        items: list[dict] = []
        llm_calls: list[dict] = []
        if seed_ids:
            for domain in parsed.domains:
                resp = self.qloo.insights(seed_ids, domain,
                                           limit=ITEMS_PER_DOMAIN + 2,
                                           location=parsed.location)
                log.add_qloo_call(resp.to_dict())
                for scored in resp.results[:ITEMS_PER_DOMAIN]:
                    item = {"entity": scored.entity.to_dict(),
                            "domain": domain,
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
                                          "domain": domain, **meta})
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
        by_key = {(r["entity_id"], r["item"]): r for r in reports}
        for item in admissible:
            rep = by_key.get((item["entity"]["id"], item["entity"]["name"]))
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
            "domains": parsed.domains,
            "seeds": seed_names,
            "mode": self.mode,
            "explainer": explainer_summary,
            "items": admissible,
            "groups": groups,
            "rejected": [i["entity"]["name"] for i in rejected],
            "rejected_details": rejected_details,
            "checker": {"passed": final_report["passed"] and bool(admissible),
                        "chain_valid": final_report["chain_valid"],
                        "violations": rejected_report["violations"],
                        "items": [by_key[(i["entity"]["id"],
                                           i["entity"]["name"])]
                                  for i in admissible]},
            "evidence": record,
        }
