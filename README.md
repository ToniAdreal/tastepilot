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
  parser.py    deterministic request parsing (scenario / location / seeds,
               incl. richer seed capture from pasted labelled lists —
               watchlist / playlist / reading list, bulleted or delimited)
  qloo.py      Qloo adapter: real client (hackathon API, X-Api-Key; bounded
               retries on 429/5xx + network errors, every failure surfaces
               as QlooError; tolerant payload-envelope parsing) + mock client
  fixtures.py  deterministic mock taste graph (fictional demo data, labelled mock)
  agent.py     the agent loop: parse -> plan -> Qloo calls -> compose -> verify
  llm.py       explanation adapters: Nebius/Nemotron + deterministic template
  evidence.py  hash-chained, HMAC-signed evidence records (VerifyGate pattern)
  checker.py   deterministic evidence checker (the invariant enforcer)
  server.py    stdlib HTTP demo server (chat + grouped itinerary UI,
               per-item checker badges, explanation provenance,
               evidence-record viewer endpoint, browser plan-verification
               endpoint for shared plan JSON, shareable verification
               links (content-addressed plan store, verify-by-link,
               7-day expiry, DELETE revocation, optional file
               persistence across restarts);
               hardened: input caps, security headers, bounded caches,
               leak-free 500s)
  smoke.py     real-mode smoke check: one search + one insights call,
               payload-shape validation, both sealed + chain re-verified
  cli.py       plan / smoke / verify / serve commands
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
python3 -m tastepilot smoke --mode real   # first: one search + one insights
                                          # call, shape-checked and sealed
python3 -m tastepilot plan "a first date in New York, we love Kind of Blue" --mode real --llm nebius
```

The smoke check is the key-arrival ritual: it validates the live payload
shape (non-empty results, entity ids/names present, sane affinities),
seals both calls into the hash-chained evidence log, and re-verifies the
chain before reporting pass/fail — so a payload-shape drift is diagnosed
per step instead of surfacing as a failed plan. `--out smoke.json` saves
the full report including the sealed evidence record. It also runs
against the mock client (`--mode mock`) for zero-key testing.

`--mode auto` picks real when a key exists and mock otherwise. The key is
only ever sent as the `X-Api-Key` header; it is never logged or printed.

The real client is built to survive a flaky hackathon endpoint: transient
failures (HTTP 429/5xx, timeouts, connection resets) are retried with
backoff, auth errors fail immediately, and every failure surfaces as a
`QlooError` — so the smoke check reports it per step and `plan` exits
cleanly with code 2 instead of dumping a traceback. Result parsing
tolerates the API's payload envelopes (flat or nested entity rows,
`properties` fields, string or missing affinities) without masking
genuine shape drift, which the smoke check still flags field by field.

## Hosted demo

The demo deploys as a single dependency-free process — see
[DEPLOY.md](DEPLOY.md) for the Docker image (non-root, healthchecked,
`PORT`-aware), the PaaS `Procfile` start command, the environment
variables, and a post-deploy smoke checklist. A public demo runs in
Judge Mode (mock Qloo, zero keys) unless the operator supplies
`QLOO_API_KEY` through the platform's secret store; the key is never
baked into the image or the repo.

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
* Seeds can arrive as a pasted list, not just prose: a request like
  *"a weekend in Kyoto. Watchlist: Spirited Away, An Obscure Reel.
  Playlist: Kind of Blue; Bon Iver"* contributes every list item as a
  seed — catalogue titles under their canonical name, unknown titles
  verbatim to Qloo search (in real mode they are not limited to the
  demo catalogue). The plan reports the lists it captured
  (`seed_lists`: label + items) and the UI status line names them.
  Delimiters are commas, semicolons, bullets, and newlines only —
  `and` never splits, so "Klara and the Sun" stays one seed.
* Anyone holding a plan JSON can re-verify it in the browser: the UI's
  "Verify a shared plan" panel posts it to `POST /api/verify`, which
  recomputes the hash chain and every per-item checker verdict from the
  sealed record alone (the record's own verdict field is reported back
  as an untrusted claim, never used). Submitted records are untrusted
  input — size-capped, shape-validated, and malformed records get a 400,
  never a crash.
* Plans also travel as shareable verification links: `POST /api/share`
  stores a plan under a content-addressed id (SHA-256 of its canonical
  JSON — storing the same plan twice is idempotent), and opening
  `/?share=<id>` fetches it back (`GET /api/share/<id>`) with its
  verification recomputed (`GET /api/share/<id>/verify`). The store is
  bounded (128 plans, oldest evicted — an evicted link simply 404s),
  and storing a plan grants it no trust: a tampered stored plan still
  FAILs by link, with its claimed verdict echoed as a claim only.
  Links also expire (7 days from first storage; re-storing the same
  plan does not extend that) and can be revoked by anyone holding
  them (`DELETE /api/share/<id>` → the link 404s for everyone). With
  `serve --share-file <path>` (or the `TASTEPILOT_SHARE_FILE` env var)
  the store persists to a JSON file written atomically on every
  mutation, so links survive a restart; a missing or corrupt file
  simply starts an empty store. `GET /api/health` reports the share
  store's mode, count, and TTL, and `--share-ttl-days` tunes the
  lifetime.

## Hackathon submission

[SUBMISSION.md](SUBMISSION.md) holds the Devpost submission text
(project story, build notes, challenges, what's next) with the
real-mode example, repo URL, and demo URL as marked slots to fill from
the final runs before submitting. CI (`.github/workflows/tests.yml`)
runs the full suite plus a `verify` pass over every shipped sample on
Python 3.11 and 3.12.

## Topics (for the public repo)

`qloo` `taste-ai` `agentic-ai` `recommender-systems` `evidence-verification`
`python` `hackathon` `devpost` `nebius` `nemotron`

## License

MIT — see [LICENSE](LICENSE).
