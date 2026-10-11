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
                             [--branch NAME=branch.json ...]
                             (with --branch: a branch-timeline export,
                             markdown|text only)
  python -m tastepilot export --bundle bundle.json
                             [--format markdown|text|ics|ics-days]
                             [--out file]
                             (a multi-timeline bundle export: the
                             file holds {"timelines": [...]} or a
                             bare list; markdown/text render one
                             grid document and need no dates)
  python -m tastepilot swap --plan plan.json --primary NAME
                             --alternative NAME [--out swapped.json]
  python -m tastepilot undo --plan plan.json [--out undone.json]
  python -m tastepilot history --plan plan.json
  python -m tastepilot branches --plan plan.json --spec branches.json
                             [--out-dir branch-dir]
  python -m tastepilot diff --base plan-a.json --other plan-b.json
                             [--out diff.json]
  python -m tastepilot suggest --plan plan.json [--limit 5]
                             [--learn-from past-plan.json]
                             [--profile-store profiles.json]
                             [--out suggestions.json]
  python -m tastepilot profile --store profiles.json
                             (--learn-from past-plan.json [...] |
                              --show | --clear)
                             [--owner-token TOKEN]
  python -m tastepilot serve [--host 127.0.0.1] [--port 8765]
                             [--mode mock|auto] [--llm template|nebius]
                             [--share-file shares.json]
                             [--share-ttl-days 7]
                             [--profile-file profiles.json]
                             [--feed-file feeds.json]
                             [--feed-ttl-days 30]
                             [--multi-feed-file multifeeds.json]
                             [--multi-feed-ttl-days 30]
                             [--rate-limit 30]
                             [--rate-window-seconds 60]

For hosted deployments the port defaults to the platform's ``PORT``
environment variable when ``--port`` is not given (see DEPLOY.md).
Share links are in-memory unless ``--share-file`` (or the
``TASTEPILOT_SHARE_FILE`` env var) names a JSON file they persist to;
every link expires after ``--share-ttl-days`` and can be revoked with
``DELETE /api/share/<id>``. Persistent preference profiles live in a
ProfileStore file (``--profile-file`` / ``TASTEPILOT_PROFILE_FILE``,
else in-memory): an owner's profile is keyed by the hash of their
owner token (``--owner-token`` or ``TASTEPILOT_OWNER_TOKEN``; the
token itself is never stored) and is written only from plans'
sealed swap histories — the ``profile`` command saves the lesson
of one or more saved plans into the store, and ``suggest
--profile-store`` folds the stored profile into the ranking.
Timeline calendar feeds are in-memory unless ``--feed-file`` (or
the ``TASTEPILOT_FEED_FILE`` env var) names a JSON file they
persist to; every feed expires after ``--feed-ttl-days`` unless
its owner updates it, and can be revoked with
``DELETE /api/feeds/<id>``. Multi-timeline feeds (one subscription
carrying many timelines, versioned with a change log and an ETag
for poll-based change notification) follow the same pattern via
``--multi-feed-file`` / ``TASTEPILOT_MULTI_FEED_FILE`` and
``--multi-feed-ttl-days``, revoked with
``DELETE /api/multi-feeds/<id>``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import TastePilotAgent
from .checker import check_plan, plan_recommendations
from .evidence import verify_chain
from .export import (FORMATS as EXPORT_FORMATS, export_multi_timeline,
                     export_plan, export_timeline)
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
    e.add_argument("--plan",
                   help="a saved plan JSON (e.g. from plan --out)")
    e.add_argument("--bundle",
                   help="a saved multi-timeline bundle JSON "
                        "({\"timelines\": [...]} or a bare list) to "
                        "export instead of a single plan: "
                        "markdown|text render one grid document "
                        "(a section per timeline, no dates needed), "
                        "ics one event series per column per "
                        "timeline, ics-days one paced event per "
                        "dated day per column per timeline; every "
                        "column's verdict is recomputed")
    e.add_argument("--format", default="markdown",
                   choices=list(EXPORT_FORMATS),
                   help="itinerary format (ics needs a dated leg; "
                        "ics-days emits one event per paced day)")
    e.add_argument("--out", help="write the export here instead of stdout")
    e.add_argument("--branch", action="append", default=[],
                   metavar="NAME=PLAN_JSON",
                   help="export a branch timeline instead of a single "
                        "plan: the --plan plan is the base and each "
                        "--branch names a branch plan file "
                        "(repeatable, max 6; markdown|text|ics|ics-days "
                        "— ics emits one event series per branch and "
                        "needs a dated leg, ics-days emits one paced "
                        "event per dated day per branch and needs a "
                        "dated day); every column's verdict is "
                        "recomputed")
    bd = sub.add_parser("bundle")
    bd.add_argument("--plan", required=True,
                    help="a saved plan JSON (e.g. from plan --out) to "
                         "pack into an audit bundle: a deterministic "
                         "ZIP carrying the plan, its sealed evidence "
                         "record, a verification report recomputed at "
                         "packing time, the take-away itineraries, "
                         "and a hashed manifest")
    bd.add_argument("--out", required=True,
                    help="write the audit bundle (.zip) here; a plan "
                         "that fails recomputation still packs (its "
                         "report reads NOT VERIFIED) but exits 1")
    vb = sub.add_parser("verify-bundle")
    vb.add_argument("--bundle", required=True, metavar="ZIP",
                    help="an audit bundle to re-audit offline: every "
                         "file is checked against the manifest's "
                         "hashes and the packed plan's verdict is "
                         "recomputed from its sealed record (the "
                         "packed verification report is never "
                         "trusted); exit 0 only when both pass")
    w = sub.add_parser("swap")
    w.add_argument("--plan", required=True,
                   help="a saved plan JSON (e.g. from plan --out)")
    w.add_argument("--primary", required=True,
                   help="name of the primary item to swap out")
    w.add_argument("--alternative", required=True,
                   help="name of the verified alternative to swap in")
    w.add_argument("--out",
                   help="write the swapped plan here (default: stdout)")
    u = sub.add_parser("undo")
    u.add_argument("--plan", required=True,
                   help="a saved plan JSON with at least one sealed swap")
    u.add_argument("--out",
                   help="write the undone plan here (default: stdout)")
    h = sub.add_parser("history")
    h.add_argument("--plan", required=True,
                   help="a saved plan JSON; its swap audit trail is "
                        "recomputed from the sealed evidence record")
    b = sub.add_parser("branches")
    b.add_argument("--plan", required=True,
                   help="a saved plan JSON to fork branches from")
    b.add_argument("--spec", required=True,
                   help="branch spec JSON: a list of {name, swaps} "
                        "(or {\"branches\": [...]}) where each swap is "
                        "{primary, alternative}; every branch is "
                        "applied to the base independently and its "
                        "verdict recomputed from its re-sealed record")
    b.add_argument("--out-dir",
                   help="write each successful branch plan here as "
                        "<branch-name>.json")
    d = sub.add_parser("diff")
    d.add_argument("--base", required=True,
                   help="the plan to compare from (a saved plan JSON)")
    d.add_argument("--other", required=True,
                   help="the plan to compare to (a saved plan JSON); "
                        "both plans' verdicts are recomputed from "
                        "their sealed records, never read from their "
                        "stored checker fields")
    d.add_argument("--out",
                   help="write the full comparison JSON here")
    t = sub.add_parser("timeline")
    t.add_argument("--base", required=True,
                   help="the base plan JSON the branches fork from")
    t.add_argument("--branch", action="append", default=[],
                   metavar="NAME=PLAN_JSON",
                   help="a branch plan file under a display name "
                        "(repeatable, max 6); every column's verdict "
                        "is recomputed from its own sealed record")
    t.add_argument("--out",
                   help="write the full timeline JSON here")
    g = sub.add_parser("suggest")
    g.add_argument("--plan", required=True,
                   help="a saved plan JSON to suggest swap branches for")
    g.add_argument("--limit", type=int, default=5,
                   help="max suggestions (1-6, default 5); the combined "
                        "guided-tour branch counts toward the limit")
    g.add_argument("--learn-from", action="append", default=[],
                   metavar="PLAN_JSON",
                   help="a saved plan whose sealed swap history the "
                        "ranking learns from (repeatable, max 4); the "
                        "--plan plan's own sealed swaps are always "
                        "learned from")
    g.add_argument("--profile-store", default=None, metavar="FILE",
                   help="a persistent preference-profile store file "
                        "(see the profile command): the stored "
                        "profile for --owner-token is merged into "
                        "the ranking under the same caps")
    g.add_argument("--owner-token", default=None,
                   help="owner token naming whose stored profile "
                        "applies (default: $TASTEPILOT_OWNER_TOKEN); "
                        "only its hash is used to key the store")
    g.add_argument("--out",
                   help="write the suggestions JSON here — its "
                        "\"branches\" list is a spec the branches "
                        "command accepts unchanged")
    pf = sub.add_parser("profile")
    pf.add_argument("--store", required=True, metavar="FILE",
                    help="the persistent preference-profile store "
                         "file (created on first save)")
    pf.add_argument("--learn-from", action="append", default=[],
                    metavar="PLAN_JSON",
                    help="derive the lesson from this saved plan's "
                         "sealed swap history and merge it into "
                         "the stored profile (repeatable, max 8)")
    pf.add_argument("--show", action="store_true",
                    help="print the stored profile as JSON")
    pf.add_argument("--export", action="store_true",
                    help="print the stored profile as a portable "
                         "sync document (carry it to another store "
                         "and --import-file it there)")
    pf.add_argument("--import-file", default=None, metavar="SYNC_JSON",
                    help="merge a sync document exported elsewhere "
                         "into the stored profile under the sync "
                         "conflict rules (conflicts are reported, "
                         "never silently resolved)")
    pf.add_argument("--clear", action="store_true",
                    help="delete the stored profile")
    pf.add_argument("--owner-token", default=None,
                    help="owner token the profile belongs to "
                         "(default: $TASTEPILOT_OWNER_TOKEN); only "
                         "its hash keys the store")
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
    s.add_argument("--profile-file", default=None,
                   help="persist preference profiles to this JSON "
                        "file (default: $TASTEPILOT_PROFILE_FILE, "
                        "else in-memory)")
    s.add_argument("--feed-file", default=None,
                   help="persist timeline calendar feeds to this "
                        "JSON file (default: $TASTEPILOT_FEED_FILE, "
                        "else in-memory)")
    s.add_argument("--feed-ttl-days", type=float, default=30.0,
                   help="timeline feed lifetime in days (default 30)")
    s.add_argument("--multi-feed-file", default=None,
                   help="persist multi-timeline feeds to this "
                        "JSON file (default: "
                        "$TASTEPILOT_MULTI_FEED_FILE, else in-memory)")
    s.add_argument("--multi-feed-ttl-days", type=float, default=30.0,
                   help="multi-timeline feed lifetime in days "
                        "(default 30)")
    s.add_argument("--rate-limit", type=int, default=None,
                   help="max POST requests per client per rate "
                        "window (default: $TASTEPILOT_RATE_LIMIT "
                        "or 30)")
    s.add_argument("--rate-window-seconds", type=float, default=60.0,
                   help="rate-limit window in seconds (default 60)")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import serve
        if args.rate_limit is not None and args.rate_limit <= 0:
            print("error: --rate-limit must be > 0", file=sys.stderr)
            return 2
        if args.rate_window_seconds <= 0:
            print("error: --rate-window-seconds must be > 0",
                  file=sys.stderr)
            return 2
        if args.rate_limit is None:
            raw_rate = os.environ.get("TASTEPILOT_RATE_LIMIT")
            if raw_rate:
                try:
                    env_rate_ok = int(raw_rate) >= 1
                except ValueError:
                    env_rate_ok = False
                if not env_rate_ok:
                    print("error: TASTEPILOT_RATE_LIMIT must be a "
                          "positive integer", file=sys.stderr)
                    return 2
        if args.share_ttl_days <= 0:
            print("error: --share-ttl-days must be > 0", file=sys.stderr)
            return 2
        if args.feed_ttl_days <= 0:
            print("error: --feed-ttl-days must be > 0", file=sys.stderr)
            return 2
        if args.multi_feed_ttl_days <= 0:
            print("error: --multi-feed-ttl-days must be > 0",
                  file=sys.stderr)
            return 2
        try:
            agent = _agent_for(args.mode, args.llm)
        except QlooError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        port = args.port if args.port is not None \
            else int(os.environ.get("PORT", "8765"))
        serve(agent, port=port, host=args.host, share_file=args.share_file,
              share_ttl_seconds=args.share_ttl_days * 24 * 3600,
              profile_file=args.profile_file,
              feed_file=args.feed_file,
              feed_ttl_seconds=args.feed_ttl_days * 24 * 3600,
              multi_feed_file=args.multi_feed_file,
              multi_feed_ttl_seconds=args.multi_feed_ttl_days * 24 * 3600,
              rate_limit=args.rate_limit,
              rate_window_seconds=args.rate_window_seconds)
        return 0
    if args.cmd == "profile":
        from .profiles import ProfileStore, owner_token_hash
        token = args.owner_token or os.environ.get(
            "TASTEPILOT_OWNER_TOKEN")
        if not token or owner_token_hash(token) is None:
            print("error: profile failed: invalid_owner_token "
                  "(pass --owner-token or set TASTEPILOT_OWNER_TOKEN)",
                  file=sys.stderr)
            return 2
        store = ProfileStore(path=args.store)
        fingerprint = owner_token_hash(token)[:12]
        actions = bool(args.learn_from) + args.show + args.clear \
            + args.export + bool(args.import_file)
        if actions != 1:
            print("error: profile failed: choose exactly one of "
                  "--learn-from, --show, --export, --import-file, "
                  "or --clear", file=sys.stderr)
            return 2
        if args.export:
            document = store.export_sync(token)
            if document is None:
                print("error: profile failed: no_profile",
                      file=sys.stderr)
                return 1
            print(json.dumps(document, indent=1, ensure_ascii=False))
            return 0
        if args.import_file:
            try:
                document = json.loads(
                    Path(args.import_file).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: profile failed: cannot read sync "
                      f"document ({type(exc).__name__})",
                      file=sys.stderr)
                return 2
            try:
                result = store.import_sync(token, document)
            except ValueError as exc:  # categorised, no traceback
                print(f"error: profile failed: {exc}", file=sys.stderr)
                return 2
            merged = result["profile"]
            print(f"profile: owner={fingerprint} "
                  f"conflicts={len(result['conflicts'])} "
                  f"kept_swaps={merged['kept_swaps']} "
                  f"undone_swaps={merged['undone_swaps']} "
                  f"tag_weights={merged['tag_weights']} "
                  f"domain_weights={merged['domain_weights']} "
                  f"store={args.store}")
            for conflict in result["conflicts"]:
                print(f"conflict: {conflict['kind']} "
                      f"{conflict['key']}: local={conflict['local']} "
                      f"remote={conflict['remote']} "
                      f"resolved={conflict['resolved']} "
                      f"rule={conflict['rule']}")
            return 0
        if args.clear:
            cleared = store.clear(token)
            print(f"profile: owner={fingerprint} cleared={cleared} "
                  f"store={args.store}")
            return 0 if cleared else 1
        if args.show:
            profile = store.get(token)
            if profile is None:
                print("error: profile failed: no_profile",
                      file=sys.stderr)
                return 1
            print(json.dumps(profile, indent=1, ensure_ascii=False))
            return 0
        plans = []
        for history_path in args.learn_from:
            try:
                past = json.loads(
                    Path(history_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: profile failed: cannot read plan "
                      f"({type(exc).__name__})", file=sys.stderr)
                return 2
            if not isinstance(past, dict):
                print("error: profile failed: bad_history",
                      file=sys.stderr)
                return 2
            plans.append(past)
        try:
            result = store.update_from_plans(token, plans)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: profile failed: {exc}", file=sys.stderr)
            return 2
        profile = result["profile"]
        derived = result["derived"]
        print(f"profile: owner={fingerprint} "
              f"kept_swaps={profile['kept_swaps']} "
              f"undone_swaps={profile['undone_swaps']} "
              f"plans_used={profile['plans_used']} "
              f"plans_ignored={profile['plans_ignored']} "
              f"derived_kept={derived['kept_swaps']} "
              f"derived_ignored={derived['plans_ignored']} "
              f"tag_weights={profile['tag_weights']} "
              f"domain_weights={profile['domain_weights']} "
              f"store={args.store}")
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
        if args.bundle and args.plan:
            print("error: export failed: --plan and --bundle are "
                  "mutually exclusive", file=sys.stderr)
            return 2
        if args.bundle and args.branch:
            print("error: export failed: --branch applies to --plan, "
                  "not --bundle", file=sys.stderr)
            return 2
        if args.bundle:
            try:
                bundle = json.loads(
                    Path(args.bundle).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: export failed: cannot read bundle "
                      f"({type(exc).__name__})", file=sys.stderr)
                return 2
            timelines = bundle.get("timelines") \
                if isinstance(bundle, dict) else bundle
            if not isinstance(timelines, list):
                print("error: export failed: bundle must hold a "
                      "timelines list", file=sys.stderr)
                return 2
            try:
                content, verified, summaries = export_multi_timeline(
                    timelines, args.format)
            except ValueError as exc:
                print(f"error: export failed: {exc}", file=sys.stderr)
                return 2
            except (KeyError, TypeError, AttributeError, IndexError):
                print("error: export failed: bad_plan", file=sys.stderr)
                return 2
            if args.out:
                Path(args.out).write_text(content, encoding="utf-8")
                print(f"export format={args.format} bundle=True "
                      f"timelines={len(summaries)} "
                      f"verified={verified} out={args.out}")
            else:
                sys.stdout.write(content)
            return 0 if verified else 1
        if not args.plan:
            print("error: export failed: --plan or --bundle is "
                  "required", file=sys.stderr)
            return 2
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
        if args.branch:
            branches = []
            for spec in args.branch:
                name, sep, path = spec.partition("=")
                if not sep or not name.strip() or not path:
                    print("error: export failed: bad_branch_spec",
                          file=sys.stderr)
                    return 2
                try:
                    branch_plan = json.loads(
                        Path(path).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"error: export failed: cannot read branch "
                          f"({type(exc).__name__})", file=sys.stderr)
                    return 2
                branches.append({"name": name.strip(),
                                 "plan": branch_plan})
            try:
                content, verified = export_timeline(
                    plan, branches, args.format)
            except ValueError as exc:
                print(f"error: export failed: {exc}", file=sys.stderr)
                return 2
            if args.out:
                Path(args.out).write_text(content, encoding="utf-8")
                print(f"export format={args.format} timeline=True "
                      f"verified={verified} out={args.out}")
            else:
                sys.stdout.write(content)
            return 0 if verified else 1
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
    if args.cmd == "bundle":
        from .bundle import build_bundle
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: bundle failed: cannot read plan "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: bundle failed: bad_plan", file=sys.stderr)
            return 2
        try:
            data, verified = build_bundle(plan)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: bundle failed: {exc}", file=sys.stderr)
            return 2
        Path(args.out).write_bytes(data)
        print(f"bundle: verified={verified} bytes={len(data)} "
              f"out={args.out}")
        # Mirror the export contract: an unverified plan still packs
        # (with its NOT VERIFIED report) but exits 1.
        return 0 if verified else 1
    if args.cmd == "verify-bundle":
        from .bundle import verify_bundle
        try:
            data = Path(args.bundle).read_bytes()
        except OSError as exc:
            print(f"error: bundle verify failed: cannot read bundle "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        try:
            report = verify_bundle(data)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: bundle verify failed: {exc}",
                  file=sys.stderr)
            return 2
        for violation in report["violations"]:
            print(f"  violation: item={violation['item']} "
                  f"reason={violation['reason']}")
        print(f"bundle verify: ok={report['ok']} "
              f"manifest_valid={report['manifest_valid']} "
              f"passed={report['passed']} "
              f"chain_valid={report['chain_valid']} "
              f"files={len(report['files'])}")
        return 0 if report["ok"] else 1
    if args.cmd == "swap":
        from .swap import swap_plan
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: swap failed: cannot read plan "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: swap failed: bad_plan", file=sys.stderr)
            return 2
        try:
            result = swap_plan(plan, args.primary, args.alternative)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: swap failed: {exc}", file=sys.stderr)
            return 2
        event = result["swaps"][-1]
        if args.out:
            Path(args.out).write_text(
                json.dumps(result, indent=1, ensure_ascii=False),
                encoding="utf-8")
            print(f"swap: [{event['primary']['domain']}] "
                  f"{event['primary']['name']} -> "
                  f"{event['alternative']['name']} "
                  f"checker_passed={result['checker']['passed']} "
                  f"chain_valid={result['checker']['chain_valid']} "
                  f"swaps={len(result['swaps'])} out={args.out}")
        else:
            print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0 if result["checker"]["passed"] else 1
    if args.cmd == "undo":
        from .swap import undo_last_swap
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: undo failed: cannot read plan "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: undo failed: bad_plan", file=sys.stderr)
            return 2
        try:
            result = undo_last_swap(plan)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: undo failed: {exc}", file=sys.stderr)
            return 2
        event = result["swaps"][-1]
        if args.out:
            Path(args.out).write_text(
                json.dumps(result, indent=1, ensure_ascii=False),
                encoding="utf-8")
            print(f"undo: reversed swap {event['undo_of'][:8]}: "
                  f"[{event['primary']['domain']}] "
                  f"{event['alternative']['name']} is primary again "
                  f"(was {event['primary']['name']}) "
                  f"checker_passed={result['checker']['passed']} "
                  f"chain_valid={result['checker']['chain_valid']} "
                  f"swaps={len(result['swaps'])} out={args.out}")
        else:
            print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0 if result["checker"]["passed"] else 1
    if args.cmd == "history":
        from .swap import swap_history
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: swap history failed: cannot read plan "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: swap history failed: bad_plan", file=sys.stderr)
            return 2
        try:
            events = swap_history(plan)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: swap history failed: {exc}", file=sys.stderr)
            # A broken chain is a verdict (exit 1, like verify);
            # every other refusal is a caller error (exit 2).
            return 1 if str(exc) == "evidence_chain_invalid" else 2
        for event in events:
            primary, alternative = event["primary"], event["alternative"]
            print(f"  swap #{event['index']} "
                  f"[{primary['domain']}] {primary['name']} -> "
                  f"{alternative['name']} swap_id={event['swap_id']}"
                  + (f" undo_of={event['undo_of']}"
                     if event["is_undo"] else ""))
        print(f"swap history: {len(events)} sealed swap(s), "
              f"chain_valid=True")
        return 0
    if args.cmd == "branches":
        import re as _re
        from .swap import explore_branches
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: branches failed: cannot read input "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: branches failed: bad_plan", file=sys.stderr)
            return 2
        branches = spec.get("branches") if isinstance(spec, dict) else spec
        try:
            result = explore_branches(plan, branches)
        except ValueError as exc:  # spec-shape refusal, no traceback
            print(f"error: branches failed: {exc}", file=sys.stderr)
            return 2
        base = result["base"]
        print(f"base: items={len(base['items'])} "
              f"swaps={base['swap_count']} passed={base['passed']} "
              f"chain_valid={base['chain_valid']}")
        out_dir = Path(args.out_dir) if args.out_dir else None
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
        all_ok = True
        for branch in result["branches"]:
            if not branch["ok"]:
                all_ok = False
                print(f"  branch '{branch['name']}': FAILED "
                      f"error={branch['error']}")
                continue
            line = (f"  branch '{branch['name']}': ok "
                    f"swaps_applied={branch['swaps_applied']} "
                    f"swaps={branch['swap_count']} "
                    f"items={len(branch['items'])} "
                    f"passed={branch['passed']} "
                    f"chain_valid={branch['chain_valid']}")
            if out_dir is not None:
                slug = _re.sub(r"[^A-Za-z0-9._-]+", "-",
                               branch["name"]).strip("-") or "branch"
                target = out_dir / f"{slug}.json"
                target.write_text(
                    json.dumps(branch["plan"], indent=1,
                               ensure_ascii=False), encoding="utf-8")
                line += f" out={target}"
            print(line)
        # Like verify: every branch applying is success (0); any
        # branch refusal is a reported verdict (1), not a crash.
        return 0 if all_ok else 1
    if args.cmd == "diff":
        from .diff import diff_plans
        try:
            base = json.loads(Path(args.base).read_text(encoding="utf-8"))
            other = json.loads(Path(args.other).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: diff failed: cannot read input "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        try:
            result = diff_plans(base, other)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: diff failed: {exc}", file=sys.stderr)
            return 2
        summary = result["summary"]
        print(f"diff: base_items={result['base']['item_count']} "
              f"other_items={result['other']['item_count']} "
              f"added={summary['added']} removed={summary['removed']} "
              f"kept={summary['kept']} "
              f"affinity_changes={summary['affinity_changes']} "
              f"identical={summary['identical']}")
        for side in ("base", "other"):
            verdict = result[side]
            print(f"  {side}: passed={verdict['passed']} "
                  f"chain_valid={verdict['chain_valid']} "
                  f"swaps={verdict['swap_count']}")
        for item in result["added"]:
            print(f"  + [{item['domain']}] {item['name']} "
                  f"({item['location']}) affinity {item['affinity']}")
        for item in result["removed"]:
            print(f"  - [{item['domain']}] {item['name']} "
                  f"({item['location']}) affinity {item['affinity']}")
        for change in result["affinity_changes"]:
            print(f"  ~ [{change['domain']}] {change['name']} "
                  f"affinity {change['base_affinity']} -> "
                  f"{change['other_affinity']}")
        for change in result["schedule_changes"]:
            print(f"  schedule {change['location']}: "
                  f"{change['base']['start']}..{change['base']['end']} "
                  f"-> {change['other']['start']}.."
                  f"{change['other']['end']}")
        if args.out:
            Path(args.out).write_text(
                json.dumps(result, indent=1, ensure_ascii=False),
                encoding="utf-8")
            print(f"diff written to {args.out}")
        # Like verify: both plans passing is success (0); either
        # side failing recomputation is a reported verdict (1).
        return 0 if result["base"]["passed"] and \
            result["other"]["passed"] else 1
    if args.cmd == "timeline":
        from .timeline import branch_timeline
        try:
            base = json.loads(Path(args.base).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: timeline failed: cannot read base "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        branches = []
        for spec in args.branch:
            name, sep, path = spec.partition("=")
            if not sep or not name.strip() or not path:
                print("error: timeline failed: bad_branch_spec",
                      file=sys.stderr)
                return 2
            try:
                plan = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: timeline failed: cannot read branch "
                      f"({type(exc).__name__})", file=sys.stderr)
                return 2
            branches.append({"name": name.strip(), "plan": plan})
        try:
            result = branch_timeline(base, branches)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: timeline failed: {exc}", file=sys.stderr)
            return 2
        columns = result["columns"]
        print(f"timeline: branches={result['summary']['branches']} "
              f"legs={result['summary']['legs']} "
              f"rows={result['summary']['rows']} "
              f"all_passed={result['summary']['all_passed']}")
        print("  columns: " + " | ".join(
            f"{c['name']} ({'PASS' if c['passed'] else 'FAIL'})"
            for c in columns))
        marks = {"kept": "=", "added": "+", "removed": "-",
                 "rescored": "~", "absent": "."}
        for leg in result["legs"]:
            print(f"  leg {leg['location']}:")
            for row in leg["rows"]:
                cells = " | ".join(
                    f"{marks[c['status']]}"
                    f"{'' if c['affinity'] is None else c['affinity']}"
                    for c in row["cells"])
                print(f"    [{row['domain']}] {row['name']}: {cells}")
        if args.out:
            Path(args.out).write_text(
                json.dumps(result, indent=1, ensure_ascii=False),
                encoding="utf-8")
            print(f"timeline written to {args.out}")
        # Like verify: every column passing is success (0); any
        # column failing recomputation is a reported verdict (1).
        return 0 if result["summary"]["all_passed"] else 1
    if args.cmd == "suggest":
        from .swap import suggest_branches
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: suggest failed: cannot read plan "
                  f"({type(exc).__name__})", file=sys.stderr)
            return 2
        if not isinstance(plan, dict):
            print("error: suggest failed: bad_plan", file=sys.stderr)
            return 2
        history_plans = []
        for history_path in args.learn_from:
            try:
                past = json.loads(
                    Path(history_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: suggest failed: cannot read history "
                      f"plan ({type(exc).__name__})", file=sys.stderr)
                return 2
            if not isinstance(past, dict):
                print("error: suggest failed: bad_history",
                      file=sys.stderr)
                return 2
            history_plans.append(past)
        stored_profile = None
        if args.profile_store:
            from .profiles import ProfileStore, owner_token_hash
            token = args.owner_token or os.environ.get(
                "TASTEPILOT_OWNER_TOKEN")
            if not token or owner_token_hash(token) is None:
                print("error: suggest failed: invalid_owner_token "
                      "(pass --owner-token or set "
                      "TASTEPILOT_OWNER_TOKEN)", file=sys.stderr)
                return 2
            stored_profile = ProfileStore(
                path=args.profile_store).get(token)
            if stored_profile is None:
                print("error: suggest failed: no_profile",
                      file=sys.stderr)
                return 2
        try:
            result = suggest_branches(
                plan, limit=args.limit,
                history_plans=history_plans or None,
                stored_profile=stored_profile)
        except ValueError as exc:  # categorised refusal, no traceback
            print(f"error: suggest failed: {exc}", file=sys.stderr)
            # A plan that does not verify is a verdict (exit 1, like
            # verify/history); every other refusal is a caller error.
            return 1 if str(exc) in ("evidence_chain_invalid",
                                     "plan_not_verified",
                                     "bad_profile",
                                     "bad_profile_weights",
                                     "bad_profile_counts") else 2
        if args.out:
            Path(args.out).write_text(
                json.dumps(result, indent=1, ensure_ascii=False),
                encoding="utf-8")
        print(f"suggest: candidates={len(result['candidates'])} "
              f"suggestions={len(result['suggestions'])} "
              f"passed={result['base']['passed']} "
              f"chain_valid={result['base']['chain_valid']}"
              + (f" out={args.out}" if args.out else ""))
        learning = result["learning"]
        if learning["kept_swaps"] or learning["undone_swaps"] \
                or learning["plans_ignored"] \
                or learning.get("stored_applied"):
            print(f"  learning: kept_swaps={learning['kept_swaps']} "
                  f"undone_swaps={learning['undone_swaps']} "
                  f"plans_used={learning['plans_used']} "
                  f"plans_ignored={learning['plans_ignored']} "
                  f"stored_applied={learning.get('stored_applied', False)} "
                  f"tag_weights={learning['tag_weights']} "
                  f"domain_weights={learning['domain_weights']}")
        for i, sug in enumerate(result["suggestions"], 1):
            print(f"  suggestion #{i} '{sug['name']}': "
                  f"score={sug['score']:.2f} "
                  f"delta={sug['affinity_delta']:+.2f} "
                  f"coverage_gain={sug['coverage_gain']} "
                  f"— {sug['rationale']}")
        return 0
    if args.cmd == "verify":
        data = json.loads(Path(args.record).read_text(encoding="utf-8"))
        record = data.get("evidence", data)
        # Alternatives are recommendations too: verify covers both.
        report = check_plan(plan_recommendations(data), record)
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
          f"alternatives={len(result.get('alternatives', []))} "
          f"checker_passed={result['checker']['passed']} "
          f"chain_valid={result['checker']['chain_valid']}")
    for alt in result.get("alternatives", []):
        print(f"  alternative: [{alt['domain']}] "
              f"{alt['entity']['name']} (affinity {alt['affinity']:.2f}) "
              f"— checker: "
              f"{(alt.get('verification') or {}).get('reason', 'unverified')}")
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
    for leg in (result.get("itinerary") or {}).get("legs", []):
        where = leg.get("location") or "anywhere"
        for day in leg.get("days", []):
            if not day.get("items"):
                continue
            label = day.get("date") or "unscheduled"
            names = ", ".join(i["name"] for i in day["items"])
            print(f"  itinerary: {where} · {label}: {names}")
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
