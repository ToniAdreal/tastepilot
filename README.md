# TastePilot — a taste-aware planning agent

> Built by Toni Adreal (solo) for the Qloo Agentic Hackathon on Devpost,
> with AI assistance (agent-orchestrated implementation; the concept,
> evidence invariant, and verification design are the entrant's).

## Problem

Generic AI planners hallucinate: they invent restaurants, venues, and
"taste matches" that no data source ever supported. A recommendation you
cannot trace is a recommendation you cannot trust.

## Solution

TastePilot turns a natural-language plan — *"a slow weekend in Kyoto"*,
*"a first date in NYC"*, *"a birthday gift shortlist"* — into a plan where
**every single recommendation must trace to a real Qloo API response**:

1. **Parse** — a deterministic parser extracts the scenario, location, and
   the seed entities the user names.
2. **Plan** — the scenario decomposes into taste domains (film, music,
   food, places, books, brands, destinations).
3. **Qloo tool calls** — seeds are resolved via Qloo `GET /search`, then
   cross-domain recommendations come from Qloo `GET /v2/insights`
   (`filter.type` + `signal.interests.entities`). Remove Qloo and there
   is nothing left to recommend from — it is load-bearing by design.
4. **Compose** — each item gets a "why this fits your taste" line grounded
   in the Qloo correlation that produced it (Nemotron via Nebius Token
   Factory when available; a deterministic template otherwise).
5. **Evidence-verify** — every Qloo call is sealed in a hash-chained,
   HMAC-signed evidence log. A deterministic checker then admits an item
   only if its cited response exists in the log, is an insights call,
   contains the entity, and matches the affinity exactly. Anything else
   is rejected as a hallucination — the LLM never gets a vote.

## Architecture

```
tastepilot/
  parser.py    deterministic request parsing (scenario / location / seeds)
  qloo.py      Qloo adapter: real client (hackathon API, X-Api-Key) + mock client
  fixtures.py  deterministic mock taste graph (fictional demo data, labelled mock)
  agent.py     the agent loop: parse -> plan -> Qloo calls -> compose -> verify
  llm.py       explanation adapters: Nebius/Nemotron + deterministic template
  evidence.py  hash-chained, HMAC-signed evidence records (VerifyGate pattern)
  checker.py   deterministic evidence checker (the invariant enforcer)
  server.py    stdlib HTTP demo server (chat + grouped itinerary UI,
               per-item checker badges, explanation provenance,
               evidence-record viewer endpoint)
  cli.py       plan / verify / serve commands
tests/         unit + end-to-end tests, all green in mock (Judge) mode
samples/       sample plans with their sealed evidence records
```

Standard library only — no dependencies to install.

## Quick start (Judge Mode, zero keys)

```bash
python3 -m pytest -q                       # full test suite
python3 -m tastepilot plan "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
python3 -m tastepilot serve --port 8765    # demo UI at http://127.0.0.1:8765
python3 -m tastepilot verify --record samples/plan_weekend_kyoto.json
```

Live LLM explanations in the demo: add `--llm nebius` to `plan` or
`serve` and the "why this fits" lines are written by Nemotron via Nebius
Token Factory. Every item carries its explanation provenance (source,
model, token usage — or the recorded reason when Nebius was unavailable
and the deterministic template took over), the plan reports an explainer
summary, and `GET /api/health` names the live explainer. Without the
flag the demo stays in deterministic template mode.

## Real mode

When the Qloo hackathon key is present (`QLOO_API_KEY` in the environment
or in a gitignored `.env` next to this README, `chmod 600`), the same loop
runs against `https://hackathon.api.qloo.com`:

```bash
python3 -m tastepilot plan "a first date in New York, we love Kind of Blue" --mode real --llm nebius
```

`--mode auto` picks real when a key exists and mock otherwise. The key is
only ever sent as the `X-Api-Key` header; it is never logged or printed.

## Demo

* `samples/plan_weekend_kyoto.json` — a full mock-mode plan: items, the
  sealed evidence record, and the checker report. Run the `verify`
  command above against it; then edit any affinity in the file and watch
  verification fail — that is the tamper-evidence working.
* The web UI groups the plan into a per-domain timeline; each card shows
  its affinity, its evidence reference, and its own per-item checker
  report (admissible + the exact reason, with the affinity found in the
  cited Qloo response). An evidence-record viewer lists every sealed
  Qloo call behind the plan and opens any of them
  (`GET /api/evidence/<response_id>`) — request, results, record hash,
  and chain validity included.

## Topics (for the public repo)

`qloo` `taste-ai` `agentic-ai` `recommender-systems` `evidence-verification`
`python` `hackathon` `devpost` `nebius` `nemotron`

## License

MIT — see [LICENSE](LICENSE).
