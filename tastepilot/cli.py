"""TastePilot CLI.

  python -m tastepilot plan "<request>" [--mode mock|real|auto] [--out plan.json]
  python -m tastepilot smoke [--mode real|auto|mock] [--query Q] [--domain D]
                             [--out smoke.json]
  python -m tastepilot verify --record plan.json
  python -m tastepilot serve [--host 127.0.0.1] [--port 8765]
                             [--mode mock|auto] [--llm template|nebius]

For hosted deployments the port defaults to the platform's ``PORT``
environment variable when ``--port`` is not given (see DEPLOY.md).
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
from .llm import NebiusExplainer, TemplateExplainer
from .models import DOMAIN_URNS
from .qloo import QlooError, make_client
from .smoke import DEFAULT_DOMAIN, DEFAULT_QUERY, run_smoke


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
    p.add_argument("--out")
    k = sub.add_parser("smoke")
    k.add_argument("--mode", default="real", choices=["mock", "real", "auto"])
    k.add_argument("--query", default=DEFAULT_QUERY)
    k.add_argument("--domain", default=DEFAULT_DOMAIN,
                   choices=sorted(DOMAIN_URNS))
    k.add_argument("--out")
    v = sub.add_parser("verify")
    v.add_argument("--record", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1",
                   help="bind host (hosted deployments: 0.0.0.0)")
    s.add_argument("--port", type=int, default=None,
                   help="bind port (default: $PORT or 8765)")
    s.add_argument("--mode", default="mock", choices=["mock", "auto"])
    s.add_argument("--llm", default="template", choices=["template", "nebius"])
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import serve
        try:
            agent = _agent_for(args.mode, args.llm)
        except QlooError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        port = args.port if args.port is not None \
            else int(os.environ.get("PORT", "8765"))
        serve(agent, port=port, host=args.host)
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
    result = agent.run(args.text)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"scenario={result['scenario']} mode={result['mode']} "
          f"items={len(result['items'])} rejected={len(result['rejected'])} "
          f"checker_passed={result['checker']['passed']} "
          f"chain_valid={result['checker']['chain_valid']}")
    for item in result["items"]:
        print(f"  [{item['domain']}] {item['entity']['name']} "
              f"(affinity {item['affinity']:.2f}) — {item['why']}")
    return 0 if result["checker"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
