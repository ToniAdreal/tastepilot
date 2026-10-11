"""TastePilot CLI.

  python -m tastepilot plan "<request>" [--mode mock|real|auto] [--out plan.json]
                             [--import-file taste-export.csv|json|txt ...]
                             [--import-url https://host/taste.json ...]
                             [--timetable-file services.json|csv ...]
                             [--timetable-url https://host/services.json ...]
  python -m tastepilot smoke [--mode real|auto|mock] [--query Q] [--domain D]
                             [--out smoke.json]
  python -m tastepilot verify --record plan.json
  python -m tastepilot export --plan plan.json
                             [--format markdown|text|ics] [--out file]
  python -m tastepilot serve [--host 127.0.0.1] [--port 8765]
                             [--mode mock|auto] [--llm template|nebius]
                             [--share-file shares.json]
                             [--share-ttl-days 7]

For hosted deployments the port defaults to the platform's ``PORT``
environment variable when ``--port`` is not given (see DEPLOY.md).
Share links are in-memory unless ``--share-file`` (or the
``TASTEPILOT_SHARE_FILE`` env var) names a JSON file they persist to;
every link expires after ``--share-ttl-days`` and can be revoked with
``DELETE /api/share/<id>``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import TastePilotAgent
from .checker import check_plan
from .evidence import verify_chain
from .export import FORMATS as EXPORT_FORMATS, export_plan
from .imports import fetch_import, load_import
from .llm import NebiusExplainer, TemplateExplainer
from .models import DOMAIN_URNS
from .qloo import QlooError, make_client
from .smoke import DEFAULT_DOMAIN, DEFAULT_QUERY, run_smoke
from .timetable import fetch_timetable, load_timetable


def _agent_for(mode: str, llm: str) -> TastePilotAgent:
    client = make_client(mode)
    explainer = NebiusExplainer() if llm == "nebius" else TemplateExplainer()
    return TastePilotAgent(client, explainer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tastepilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("text")
    p.add_argument("--mode", default="mock", choices=["mock", "real", "auto"])
    p.add_argument("--llm", default="template", choices=["template", "nebius"])
    p.add_argument("--import-file", action="append", default=[],
                   dest="import_files", metavar="FILE",
                   help="import seeds from an exported taste file "
                        "(Spotify JSON / Letterboxd or Goodreads CSV / "
                        "generic CSV, JSON, or text list); repeatable")
    p.add_argument("--import-url", action="append", default=[],
                   dest="import_urls", metavar="URL",
                   help="seeds from a taste-feed API endpoint serving "
                        "the same export schema (JSON/CSV/text) over "
                        "HTTP; fetched once (bounded retries), only "
                        "its host is recorded; repeatable")
    p.add_argument("--timetable-file", action="append", default=[],
                   dest="timetable_files", metavar="FILE",
                   help="scheduled inter-leg services from a timetable "
                        "file (JSON or CSV: from, to, duration); pairs "
                        "it covers are judged on the scheduled duration "
                        "instead of the offline estimate; repeatable")
    p.add_argument("--timetable-url", action="append", default=[],
                   dest="timetable_urls", metavar="URL",
                   help="scheduled inter-leg services from a timetable "
                        "API endpoint serving the same JSON/CSV schema "
                        "over HTTP; fetched once (bounded retries), "
                        "only its host is recorded; repeatable")
    p.add_argument("--out")
    k = sub.add_parser("smoke")
    k.add_argument("--mode", default="real", choices=["mock", "real", "auto"])
    k.add_argument("--query", default=DEFAULT_QUERY)
    k.add_argument("--domain", default=DEFAULT_DOMAIN,
                   choices=sorted(DOMAIN_URNS))
    k.add_argument("--out")
    v = sub.add_parser("verify")
    v.add_argument("--record", required=True)
    e = sub.add_parser("export")
    e.add_argument("--plan", required=True,
                   help="a saved plan JSON (e.g. from plan --out)")
    e.add_argument("--format", default="markdown",
                   choices=list(EXPORT_FORMATS),
                   help="itinerary format (ics needs a dated leg)")
    e.add_argument("--out", help="write the export here instead of stdout")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1",
                   help="bind host (hosted deployments: 0.0.0.0)")
    s.add_argument("--port", type=int, default=None,
                   help="bind port (default: $PORT or 8765)")
    s.add_argument("--mode", default="mock", choices=["mock", "auto"])
    s.add_argument("--llm", default="template", choices=["template", "nebius"])
    s.add_argument("--share-file", default=None,
                   help="persist share links to this JSON file "
                        "(default: $TASTEPILOT_SHARE_FILE, else in-memory)")
    s.add_argument("--share-ttl-days", type=float, default=7.0,
                   help="share-link lifetime in days (default 7)")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import serve
        if args.share_ttl_days <= 0:
            print("error: --share-ttl-days must be > 0", file=sys.stderr)
            return 2
        try:
            agent = _agent_for(args.mode, args.llm)
        except QlooError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        port = args.port if args.port is not None \
            else int(os.environ.get("PORT", "8765"))
        serve(agent, port=port, host=args.host, share_file=args.share_file,
              share_ttl_seconds=args.share_ttl_days * 24 * 3600)
        return 0
    if args.cmd == "smoke":
        try:
            client = make_client(args.mode)
        except QlooError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        report = run_smoke(client, query=args.query, target_domain=args.domain)
        if args.out:
            Path(args.out).write_text(
                json.dumps(report, indent=1, ensure_ascii=False),
                encoding="utf-8")
        for step in report["steps"]:
            shape = step.get("shape") or {}
            count = shape.get("result_count", "-")
            print(f"  [{step['name']}] ok={step['ok']} "
                  f"reason={step['reason']} results={count}")
        print(f"smoke source={report['source']} "
              f"passed={report['passed']} "
              f"chain_valid={report['evidence']['chain_valid']} "
              f"sealed={report['evidence']['sealed_responses']}")
        return 0 if report["passed"] else 1
    if args.cmd == "export":
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: export failed: cannot read plan "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: export failed: plan must be a JSON object",
                  file=sys.stderr)
            return 2
        try:
            content, verified = export_plan(plan, args.format)
        except ValueError as exc:
            print(f"error: export failed: {exc}", file=sys.stderr)
            return 2
        if args.out:
            Path(args.out).write_text(content, encoding="utf-8")
            print(f"export format={args.format} verified={verified} "
                  f"out={args.out}")
        else:
            sys.stdout.write(content)
        # Mirror the plan/verify contract: an unverified plan still
        # exports (with its NOT VERIFIED banner) but exits 1.
        return 0 if verified else 1
    if args.cmd == "verify":
        data = json.loads(Path(args.record).read_text(encoding="utf-8"))
        record = data.get("evidence", data)
        report = check_plan(data.get("items", []), record)
        ok = verify_chain(record) and report["passed"]
        print(json.dumps({"chain_valid": verify_chain(record), **report}, indent=1))
        return 0 if ok else 1
    try:
        agent = _agent_for(args.mode, args.llm)
    except QlooError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    imported = []
    for import_path in getattr(args, "import_files", []) or []:
        try:
            imported.append(load_import(import_path))
        except ValueError as exc:
            print(f"error: import failed: {exc}", file=sys.stderr)
            return 2
    for import_url in getattr(args, "import_urls", []) or []:
        try:
            imported.append(fetch_import(import_url))
        except ValueError as exc:
            print(f"error: import failed: {exc}", file=sys.stderr)
            return 2
    timetables = []
    for timetable_path in getattr(args, "timetable_files", []) or []:
        try:
            timetables.append(load_timetable(timetable_path))
        except ValueError as exc:
            print(f"error: timetable failed: {exc}", file=sys.stderr)
            return 2
    for timetable_url in getattr(args, "timetable_urls", []) or []:
        try:
            timetables.append(fetch_timetable(timetable_url))
        except ValueError as exc:
            print(f"error: timetable failed: {exc}", file=sys.stderr)
            return 2
    try:
        result = agent.run(args.text, imported=imported,
                           timetables=timetables)
    except QlooError as exc:
        # A live-API failure mid-plan (after the client's retries) is a
        # clean error + exit 2, never a traceback — same contract as the
        # smoke and serve paths.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"scenario={result['scenario']} mode={result['mode']} "
          f"items={len(result['items'])} rejected={len(result['rejected'])} "
          f"excluded={len(result.get('excluded', []))} "
          f"checker_passed={result['checker']['passed']} "
          f"chain_valid={result['checker']['chain_valid']}")
    for ex in result.get("excluded", []):
        print(f"  excluded: {ex['name']} ({ex['domain']}) — matched "
              f"exclusion '{ex['matched']}' "
              f"(evidence verification: {ex.get('verification')})")
    for imp in result.get("imports", []):
        print(f"  import: {imp['source_file']} ({imp['format']}) "
              f"— {len(imp['items'])} seed(s)")
    for tt in result.get("timetable", []):
        tt_source = "timetable_api" \
            if str(tt.get("format", "")).startswith("timetable_api") \
            else "timetable_file"
        matched = sum(1 for t in result.get("transfers", [])
                      if t.get("source") == tt_source)
        print(f"  timetable: {tt['source_file']} ({tt['format']}) "
              f"— {tt['entries']} service(s), {matched} matched")
    for entry in result.get("schedule", []):
        if entry.get("start"):
            print(f"  schedule: {entry['location'] or 'anywhere'} "
                  f"{entry['start']}"
                  f"{' – ' + entry['end'] if entry.get('end') else ''}"
                  f"{' (' + str(entry['nights']) + ' nights)' if entry.get('nights') is not None else ''}")
    for warning in result.get("date_warnings", []):
        print(f"  schedule warning [{warning['code']}] "
              f"{warning['location'] or 'anywhere'}: {warning['detail']}")
    for transfer in result.get("transfers", []):
        print(f"  transfer: {transfer['detail']} "
              f"· status={transfer['status']} "
              f"· source={transfer.get('source', 'offline_estimate')}")
    for warning in result.get("travel_warnings", []):
        print(f"  travel warning [{warning['code']}] "
              f"{warning['location'] or 'anywhere'}: {warning['detail']}")
    for item in result["items"]:
        print(f"  [{item['domain']}] {item['entity']['name']} "
              f"(affinity {item['affinity']:.2f}) — {item['why']}")
    for leg in (result.get("handoffs") or {}).get("legs", []):
        where = leg.get("location") or "anywhere"
        if leg.get("calendar_url"):
            print(f"  handoff: add {where} leg to Google Calendar: "
                  f"{leg['calendar_url']}")
        if leg.get("maps_url"):
            print(f"  handoff: open {where} in Google Maps: "
                  f"{leg['maps_url']}")
        if leg.get("maps_apple_url"):
            print(f"  handoff: open {where} in Apple Maps: "
                  f"{leg['maps_apple_url']}")
    share = (result.get("handoffs") or {}).get("share") or {}
    if share:
        print(f"  share payload: {share.get('title', '')} "
              f"({len(share.get('text', ''))} chars, OS share sheet)")
    return 0 if result["checker"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
