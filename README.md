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
  parser.py    deterministic request parsing (scenario / locations / seeds,
               incl. richer seed capture from pasted labelled lists —
               watchlist / playlist / reading list, bulleted or delimited —
               multi-city legs: every city mentioned, in mention order —
               and date-aware legs: explicit / range / relative dates
               attached per leg, with ordering + pacing warnings;
               negative preferences: short cued clauses — "no jazz",
               "avoid ramen and wine", "allergic to shellfish" — are
               captured as exclusions the agent filters against)
  travel.py    inter-leg travel-time feasibility: offline door-to-door
               transfer estimates (haversine distance between leg
               cities; surface under 400 km, air above) checked
               against the schedule window each transfer is left —
               ok / tight / infeasible / unknown, warnings surfaced
               in the same report-never-repair shape as date warnings
  timetable.py timetable providers behind the feasibility check:
               scheduled services from a timetable file the user
               supplies (JSON/CSV: from, to, duration, mode, service —
               parsed offline, no API, no account), or from a
               timetable API endpoint the user names (same schema
               over HTTP, fetched once with bounded retries, only
               its host recorded), replace the offline estimate for
               the pairs they cover; uncovered pairs fall back to
               the estimate, and every transfer records its source
               (timetable_file / timetable_api / offline_estimate)
  qloo.py      Qloo adapter: real client (hackathon API, X-Api-Key; bounded
               retries on 429/5xx + network errors, every failure surfaces
               as QlooError; tolerant payload-envelope parsing) + mock client
  fixtures.py  deterministic mock taste graph (fictional demo data, labelled mock)
  agent.py     the agent loop: parse -> plan -> Qloo calls -> compose -> verify
               (multi-city: one leg per city, same resolved seeds for every
               leg — taste continuity — with per-leg location-filtered
               Qloo calls sealed separately)
  llm.py       explanation adapters: Nebius/Nemotron + deterministic template
  evidence.py  hash-chained, HMAC-signed evidence records (VerifyGate pattern)
  checker.py   deterministic evidence checker (the invariant enforcer)
  server.py    stdlib HTTP demo server (chat + grouped itinerary UI,
               per-item checker badges, explanation provenance,
               evidence-record viewer endpoint, browser plan-verification
               endpoint for shared plan JSON, shareable verification
               links (content-addressed plan store, verify-by-link,
               7-day expiry, DELETE revocation, optional file
               persistence across restarts, owner-managed links:
               an X-Owner-Token bearer credential — only its SHA-256
               hash is stored — lists and revokes just its owner's
               links via GET /api/shares; persistent preference
               profiles under the same token via GET/POST/DELETE
               /api/profile, folded into /api/suggest rankings for
               the token's owner; live calendar subscription feeds
               for branch timelines under stable /api/feeds/<id>.ics
               URLs — a TimelineFeedStore (bounded, 30-day expiry,
               DELETE revocation, owner-managed updates, optional
               file persistence) whose every fetch re-renders the
               stored timeline with verdicts recomputed);
               hardened: input caps, security headers, bounded caches,
               leak-free 500s)
  export.py    take-away itinerary exports (Markdown / plain text /
               iCalendar, one all-day event per dated leg): every
               export carries a verification banner recomputed from
               the sealed record — a tampered plan exports as NOT
               VERIFIED, never clean; rendering is deterministic;
               export_timeline renders a branch timeline (branch_timeline
               grid: one table/section per leg, one column per branch)
               into Markdown/text take-away exports under the same
               recomputed-verdict doctrine — a tampered branch is a
               FAIL column under a NOT VERIFIED banner; ics renders
               one event series per branch (see the Demo section),
               and a dateless timeline refuses with no_dated_legs
  handoff.py   native handoffs: one-tap links into the user's own
               apps — a Google Calendar template URL per dated leg
               (all-day, exclusive end; a dateless leg gets none —
               no invented dates) and a Google Maps search URL per
               leg and per recommendation, plus the platform-native
               forms of the same queries (Apple Maps URLs for
               iOS/macOS, geo: URIs for Android maps intents) and a
               Web Share API payload ({title, text}) for the OS
               share sheet whose verification line is recomputed
               from the sealed evidence; links are recomputed
               from the plan's schedule + items on every surface,
               never read back from a stored plan field; no account,
               no credential, no network — the user's own browser
               completes the action
  swap.py      one-tap swapping of a primary item with a verified
               alternative (same domain, same leg): preconditions
               recomputed (chain + full-plan checker + both items),
               checker re-run over the swapped plan, and the evidence
               record re-sealed around a swap_event entry (original
               Qloo calls replayed byte-identically); swap_history
               recomputes the audit trail from the sealed events
               alone, and undo_last_swap reverses the newest one
               through the same preconditions, sealing the reversal
               as an undo_of event; explore_branches forks one base
               plan into named swap paths (each applied
               independently, re-sealed, and compared by verdicts
               recomputed from its own record); suggest_branches
               ranks every verified primary/alternative pair by
               affinity delta and taste coverage and proposes
               branch specs (singles plus a combined guided tour)
               that explore_branches accepts unchanged;
               learn_preferences derives the ranking's learned
               adjustment from the sealed swap history (kept swaps
               positive, undone swaps negative, capped weights);
               validate_preference_profile + merge_profiles gate
               and combine profiles under the same caps
  profiles.py  Persistent preference profiles: an owner-token-keyed
               store (only the token's SHA-256 hash is kept) holding
               the learned profile across plans and sessions —
               writable ONLY by deriving from sealed swap histories
               (update_from_plans; caller-supplied weights are never
               stored), additive merges under the learner's caps,
               bounded, clearable by its owner, and file-persistent
               across restarts when a path is given
               (TASTEPILOT_PROFILE_FILE), exactly like the share store
  diff.py      plan comparison: two plans diffed item by item
               (keyed by entity + domain + leg, so a move between
               legs reads as remove + add) — added/removed/kept
               items, affinity changes on kept slots, alternatives
               and schedule changes — with BOTH plans' verdicts
               recomputed from their own sealed records; a stored
               checker field is never read on either side, and swap
               counts come from the sealed swap history, never the
               stored mirror
  timeline.py  visual branch timeline data: recommendation slots
               grouped by leg (trip mention order), one column per
               branch beside the base, every cell kept / added /
               removed / re-scored / absent with its affinity —
               built on diff.py, so every column's verdict is
               recomputed from its own sealed record and a
               tampered branch is a FAIL column in the grid
  smoke.py     real-mode smoke check: one search + one insights call,
               payload-shape validation, both sealed + chain re-verified
  imports.py   external taste-file imports: Spotify playlist-export JSON,
               Letterboxd / Goodreads CSV, generic CSV/JSON/text lists —
               parsed offline (no account login, no network) into seeds
               with format + item provenance; size/item caps, categorised
               errors, basename-only recording; the same schema can also
               be fetched over HTTP from a user-supplied taste-feed
               endpoint (fetch-once, bounded retries, host-only
               provenance, formats stamped *_api_*)
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
* A request naming several cities becomes a multi-city trip: each
  city is a leg, planned in mention order with the same resolved seeds
  (your taste carries across legs) but its own location-filtered Qloo
  calls — so the Kyoto leg cites Kyoto-filtered responses and the Paris
  leg cites Paris-filtered ones, and a location-agnostic pick (a record
  you would love anywhere) may appear once per leg, each time traced to
  that leg's own response. The plan reports `locations` and `legs`
  (per-leg domain groups alongside the flat `groups`), every item
  carries its leg in `location`, and the UI renders one section per leg
  with a `Legs: Kyoto → Paris` status line.
* Legs are date-aware: dates in the request — explicit (`Nov 3`,
  `2026-11-03`, `3 November 2026`), ranges (`from Nov 3 to Nov 5`,
  `Nov 3-5`), durations (`Nov 3 for 3 nights`), or relative (`tomorrow`,
  `in 5 days`, `next Friday`, all resolved against a reference date) —
  attach to the leg whose city was most recently mentioned, and the
  plan reports a `schedule` (per-leg `start`/`end`/`nights`) plus
  `date_warnings` when legs are out of order, overlap, or end before
  they start. Warnings are surfaced, never silently repaired, and they
  never block the plan. Dates are composition constraints only — the
  Qloo API has no date filter, so no date is ever sent to it; the
  evidence invariant is untouched.
* Legs also pass a travel-time feasibility check: each inter-leg
  transfer gets an offline door-to-door estimate (haversine distance
  between the leg cities — surface travel under 400 km, air travel
  with a flat airport overhead above that) compared against the
  window the schedule leaves it (a next-day start leaves 24 h, a
  same-day start 12 h, a start before the previous leg ends 0 h).
  The plan reports `transfers` (estimate, window, and an
  `ok`/`tight`/`infeasible`/`unknown` status — unknown when a leg
  has no dates or a city has no coordinates, never guessed) plus
  `travel_warnings` for tight or infeasible transfers, in the same
  shape as `date_warnings`. Like dates, estimates are composition
  constraints only: reported, never silently repaired, never
  blocking, and never sent to Qloo.
* A transfer can be judged on a scheduled service instead of the
  estimate: `plan --timetable-file <file>` parses a timetable file
  the user supplies — a JSON or CSV list of the services they are
  actually considering (`from`, `to`, `duration_hours` or
  `duration_minutes`, optional `mode` and `service`) — entirely
  offline, with no timetable API, account, or network call. Pairs
  the file covers are judged on the scheduled duration (a service
  listed in one direction is used in reverse and flagged as such);
  pairs it does not cover keep the offline estimate. Every transfer
  records its `source` (`timetable_file` or `offline_estimate`) and
  its detail line says "scheduled" or "door-to-door" accordingly —
  an estimate is never presented as a schedule. The plan reports
  the timetable files it used (`timetable`: source-file basename —
  never a path — format, and entry count), the CLI prints one
  `timetable:` line per file with the matched-transfer count, and
  malformed files fail cleanly with a categorised error
  (`bad_json`, `no_items`, `too_large`, …) and exit code 2. Files
  are capped at 1 MiB and 500 entries. Scheduled durations are
  composition constraints only, exactly like estimates: never sent
  to Qloo, never in the evidence log.
* The same scheduled services can come from a live endpoint
  instead of a file: `plan --timetable-url <url>` fetches a
  timetable API endpoint serving the identical JSON/CSV schema
  over HTTP — one GET, no account, no key, bounded retries on
  transient failures (429/5xx and network errors, mirroring the
  real Qloo client), then served from memory exactly like a file.
  Transfers it covers record `source=timetable_api`. Only the
  endpoint's host is ever recorded as provenance (`timetable`:
  host, `timetable_api_json`/`timetable_api_csv` format, entry
  count): the full URL — whose query string could carry a token —
  appears in no plan, summary line, or error message, and fetch
  failures are categorised (`http_error`, `network_error`,
  `unsupported`, plus the file parse categories) with exit code 2,
  never a traceback.
* Seeds can also arrive as an exported taste file: `plan --import-file
  <file>` parses a file the user downloaded from their own accounts —
  a Spotify playlist export (JSON), a Letterboxd watchlist or a
  Goodreads library (CSV), or a generic CSV/JSON/text list — entirely
  offline, with no account login and no network call. Every imported
  title becomes a seed through the same sealed search → insights →
  checker path as a typed seed (an import names a title; it grants it
  no trust), catalogue titles canonicalise exactly as pasted-list
  titles do, and unknown titles pass verbatim to Qloo search. The plan
  reports the imports it used (`imports`: source-file basename —
  never a path — detected format, and per-item provenance with the
  creator and domain hint), the CLI prints one `import:` line per
  file, and malformed files fail cleanly with a categorised error
  (`bad_json`, `no_items`, `too_large`, …) and exit code 2. Files are
  capped at 1 MiB and 200 items.
* Seeds can also arrive from a taste-feed endpoint: `plan
  --import-url <url>` fetches a list serving the same export schema
  over HTTP (a Spotify-shaped JSON such as `spotify_api_json`, a
  Letterboxd CSV, or a plain text list) — fetched once with bounded
  retries on transient failures, with no key or credential ever
  sent, then parsed exactly like a file and fed through the same
  sealed seed path. Only the endpoint's host is ever recorded as
  provenance (`imports`: host, the API-stamped format, items): the
  full URL — whose query string could carry a token — appears in
  no plan, summary line, or error message, and fetch failures are
  categorised (`http_error`, `network_error`, `unsupported`, plus
  the file parse categories) with exit code 2, never a traceback.
* Taste and timetable files can also be attached in the browser: the
  demo page's file inputs read the file in the browser and send its
  contents with the plan request (`POST /api/plan` `imports` /
  `timetables`), where the server parses them with the same byte
  parsers as the CLI file options — for that plan only, with no
  upload store and nothing persisted. Provenance in the plan is
  unchanged (basename, format, items); malformed attachments are
  categorised 400s that never echo the submitted content. A plain
  plan body keeps the 16 KiB cap; only a body carrying attachments
  may reach the 512 KiB plan cap, and at most 4 files of each kind
  attach per plan.
* Seeds can arrive as a pasted list, not just prose: a request like
  *"a weekend in Kyoto. Watchlist: Spirited Away, An Obscure Reel.
  Playlist: Kind of Blue; Bon Iver"* contributes every list item as a
  seed — catalogue titles under their canonical name, unknown titles
  verbatim to Qloo search (in real mode they are not limited to the
  demo catalogue). The plan reports the lists it captured
  (`seed_lists`: label + items) and the UI status line names them.
  Delimiters are commas, semicolons, bullets, and newlines only —
  `and` never splits, so "Klara and the Sun" stays one seed.
* A request can also say what to leave out: a short cued clause —
  "no jazz", "avoid ramen and wine", "allergic to shellfish",
  "skip the museums", "no books" — is captured deterministically as
  an exclusion and applied by the agent as a filter AFTER the
  evidence checker, by exact name/tag/domain match (naive singulars
  count, so "books" finds the book domain). An excluded item is
  reported separately (`excluded`, with the phrase it matched and
  its evidence verification, which stays `ok`): it was removed by
  preference, not rejected as unbacked — the two lists never mix,
  and exclusions are never sent to Qloo. Prose that merely contains
  a cue word is guarded out ("don't want to miss the jazz bars"
  wants the jazz bars; "never been to Kyoto" is biography).
* Every plan also carries evidence-backed alternatives: each
  insights call asks Qloo for two more results than the plan shows,
  and the overflow is offered as swap options (`alternatives`) for
  the same domain and leg — the Kyoto weekend plan, for example,
  shows the Kyoto National Museum and a kaiseki tasting menu as
  alternatives beside its primary picks. An alternative is held to
  exactly the primary standard: it cites the same sealed insights
  response it came from, it is checker-verified against the final
  record before it is kept (a non-admissible one is dropped, never
  shown), user exclusions filter it just as they filter primaries,
  and every verification surface covers it — tampering with an
  alternative's affinity makes CLI `verify`, the server's verify
  report, the export banner, and the share payload all fail, just
  as tampering with a primary item does. Alternatives are never
  primary items: they stay out of `items`, the domain groups, the
  per-leg groups, and the handoffs. A domain whose Qloo response
  holds no overflow simply has no alternatives.
* An alternative can be swapped in with one tap (the alternative
  card's "Swap in for …" button, `POST /api/swap`, or
  `tastepilot swap --plan plan.json --primary NAME --alternative
  NAME`): the primary and the alternative exchange places — same
  domain and same leg only — under the plan's own rules. The swap
  module recomputes the preconditions first (the sealed chain must
  verify, the whole plan must pass the checker, both items must
  trace to their responses; a tampered plan is refused, never
  re-sealed), re-runs the checker over the swapped plan, and then
  re-seals the evidence record around the exchange: the original
  Qloo calls are replayed byte-identically, a `swap_event` entry
  naming both items and the previous record hash is appended to
  the chain, and the record is re-signed with the recomputed
  verdict — so `verify` attests the original calls and every swap
  at once, and swapping back is just another sealed event.
* The swap history is an audit trail, not a display field: every
  surface (the UI's swap-history block, the verify report, the
  Markdown/text exports, `tastepilot history`) recomputes it from
  the sealed `swap_event` entries via `swap_history` — editing a
  plan's stored `swaps` list changes nothing anywhere, and a plan
  whose chain fails verification reports no history at all (the
  verify report marks its claimed swaps as unverified instead).
  The newest swap can be walked back with the UI's "Undo last
  swap" button, `POST /api/undo`, or `tastepilot undo`: the
  reversal target is read from the sealed history, the exchange
  runs through every swap precondition again (a tampered plan is
  refused, never re-sealed), and the reversal is itself sealed as
  a swap event whose `undo_of` names the swap it reversed — so
  the chain attests swaps and undos alike, the undone event is
  never deleted, and undoing an undo redoes the swap.
* Swap paths can be explored side by side instead of committed to
  one at a time: a branch spec (`{name, swaps}` list — the UI's
  "Explore branches from alternatives" button, `POST
  /api/branches`, or `tastepilot branches --plan plan.json --spec
  branches.json [--out-dir dir]`) forks the base plan into named
  branches via `explore_branches`, each branch's swaps applied to
  the base independently through the full swap path (preconditions
  re-checked, record re-sealed per swap). The comparison reports
  every branch's recomputed verdict — checker passed, chain valid,
  sealed swap count, resulting items — and a branch whose path
  does not apply (a domain mismatch, a tampered base) is reported
  with its categorised refusal beside the successful branches
  instead of aborting the exploration. Each branch line also
  carries its plan comparison against the base — `+added`,
  `−removed`, `~rescored` — recomputed server-side by
  `POST /api/diff` (`tastepilot diff --base a.json --other
  b.json` is the CLI form): the two plans are diffed item by item
  and BOTH sides' verdicts are recomputed from their own sealed
  records, so a tampered branch diffs as NOT VERIFIED on its side
  of the comparison. The branch explorer's "View branch timeline"
  button lays the successful branches over the trip's legs as a
  table (`POST /api/timeline`; `tastepilot timeline --base a.json
  --branch Name=b.json` is the CLI form): one row per
  recommendation slot grouped by leg, one column per branch, each
  cell marked kept / added / removed / re-scored with its
  affinity, and each column headed by its recomputed verdict — a
  tampered branch is a FAIL column in the grid. Branches never share
  state and the base is never mutated; a branch plan is an
  ordinary swapped plan (its `branch` field is reader-facing
  provenance, never trusted by verification), so it verifies,
  exports, shares, swaps further, and undoes like any other.
* The agent also proposes which branches are worth exploring:
  `suggest_branches` (the UI's "Suggest branches" button, `POST
  /api/suggest`, or `tastepilot suggest --plan plan.json [--limit
  5] [--out suggestions.json]`) verifies the base plan first,
  then ranks every admissible primary/alternative pair sharing a
  domain and a leg by a deterministic score — the alternative's
  affinity plus its affinity delta against the primary (a loss
  is charged in full) plus a small bonus per taste tag the
  alternative would introduce that no primary item carries
  (taste coverage). The output is the top single-swap branches
  plus, when at least two domains offer a swap, a combined
  "Guided tour: best swap in each domain" branch — each with
  its score, delta, new tags, and a plain-language rationale —
  as a `branches` spec that the branches surfaces accept
  unchanged (`suggest --out` writes a file `branches` can read
  directly). Suggestions propose only: nothing is applied,
  mutated, or re-sealed, and a tampered plan is refused with
  the swap path's categories instead of being suggested from.
* The ranking also learns from the user's own past swap choices:
  `learn_preferences` reads the sealed swap events of the base
  plan itself plus up to four history plans the caller supplies
  (`suggest --learn-from past.json`, `POST /api/suggest` with a
  `history` list, each shape-gated). A swap the user kept adds
  weight to the tags and domain of the alternative they chose;
  a swap they later undid subtracts the same (undoing an undo
  restores the positive signal) — tag weights step ±0.04 and
  cap at ±0.12, domain weights ±0.02 capping at ±0.06. Each
  candidate's score gains the bounded learned adjustment (the
  sum of its alternative's tag weights plus its domain weight),
  every candidate and suggestion reports its adjustment and the
  tags that produced it, and the whole profile is returned as
  the result's `learning` field. History is read only through
  the sealed record: a history plan whose chain does not verify
  is counted in `plans_ignored` and teaches nothing. With no
  swap history anywhere, every adjustment is exactly zero and
  the ranking is the fixed formula unchanged.
* That learning can persist across plans and sessions:
  Persistent preference profiles (`profiles.py`, the demo UI's
  "Your preference profile" panel, `GET/POST/DELETE
  /api/profile`, `tastepilot profile --store profiles.json
  --learn-from past.json`, `suggest --profile-store`) store the
  learned profile under the hash of the owner's token — the
  same per-browser credential that owns share links, so the
  profile follows the token between devices exactly as the
  share dashboard does. A profile is written only by deriving
  it from sealed swap histories (`learn_preferences` over the
  supplied plans) and merging additively under the learner's
  own caps — no surface accepts caller-supplied weights, and
  re-saving the same lesson can never push a weight past the
  cap. With `serve --profile-file <path>` (or the
  `TASTEPILOT_PROFILE_FILE` env var) profiles survive a server
  restart in an atomically-written JSON file holding only
  token hashes and derived weights; a corrupt file starts an
  empty store. When a suggestion request presents the owner's
  token, the stored profile merges into the ranking and the
  result's `learning` field reports `stored_applied`; the
  owner can read or clear their profile at any time, and one
  owner's profile never affects another's ranking.
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
* Share links can be owner-managed, with no account system: a client
  creating a link may present an `X-Owner-Token` bearer credential
  (the demo UI generates one per browser and keeps it in
  localStorage — a per-browser "account", no registration, no
  password). The server stores only the token's SHA-256 hash — a
  leaked share file exposes no owner credential — and the first
  token to store a plan owns it (re-storing never transfers
  ownership, exactly as it never extends expiry). `GET /api/shares`
  with that header lists only the owner's links, each with its item
  count, expiry, and recomputed checker verdict (a tampered owned
  plan still lists as FAIL), and revoking an owned link requires
  the owner's token (`DELETE` without it, or with another token,
  gets a 403 and changes nothing). Shares created without a token
  remain legacy links: link-holder revocation, and they appear in
  no owner's list. Ownership governs management only — every read
  still recomputes verification from the sealed record.
* The owner token is portable across browsers and devices — the
  cross-device sync story without an account system. Every owned
  surface (share creation, the dashboard list) names the token only
  by its **fingerprint**: the first 12 hex characters of its stored
  hash, enough for two browsers to confirm they hold the same token
  and useless as a credential (the full hash is never returned
  either). The demo UI shows the current browser's fingerprint,
  copies its token to the local clipboard on request, and can adopt
  a token pasted from another browser — validating it against
  `GET /api/shares` *before* replacing its own, so a malformed
  token never orphans the links this browser already owns. The
  adopted browser gets the identical dashboard: same links, same
  fingerprints, same list-and-revoke rights.
* A plan also leaves as a take-away itinerary: `tastepilot export
  --plan plan.json [--format markdown|text|ics] [--out file]`, the
  demo UI's export row and verify-panel button, and
  `POST /api/export?format=...` all render the same exporter.
  A branch timeline leaves the same way — a branch-timeline export:
  `tastepilot export --plan base.json --branch NAME=branch.json
  [--format markdown|text|ics]`, `POST /api/export-timeline?format=...`,
  and the branch explorer's "Download timeline" buttons render the
  timeline grid (slots grouped by leg, one column per branch,
  kept/added/removed/re-scored cells with affinities) as a Markdown
  table or plain-text grid, each column headed by its recomputed
  verdict; a tampered branch exports as a FAIL column under a NOT
  VERIFIED banner, and the CLI exits 1 while still writing it.
  The timeline also exports as iCalendar (`export_timeline` with
  format `ics`): one event series per branch — for every dated leg
  in that branch's own schedule, an all-day event whose summary
  names the branch and whose description carries that branch's
  recomputed verdict and that leg's recommendations in that
  branch, so a recipient can compare swap paths on their own
  calendar. A tampered branch's series reads NOT VERIFIED, UIDs
  and DTSTAMPs derive from branch name + plan content + leg dates
  (byte-identical re-exports), and a timeline with no dated leg in
  any column refuses with `no_dated_legs` instead of inventing
  dates — the single-plan ICS rule, per timeline.
  Markdown and plain-text exports carry the request, schedule and
  transfer warnings, per-leg domain sections (item, affinity, why,
  evidence reference), rejected items with their reasons, and a
  provenance block. The iCalendar export emits one all-day event
  per dated leg (DTEND exclusive; UID and DTSTAMP derived from the
  plan content and leg dates, so re-exporting the same plan is
  byte-identical) and refuses a dateless plan with a categorised
  `no_dated_legs` error instead of inventing dates. Every format
  carries a verification banner **recomputed** from the sealed
  record at export time: a tampered plan exports with a NOT
  VERIFIED banner and its violations — never a clean one — and the
  CLI mirrors the verify contract (unverified export still writes,
  exits 1).
* A branch timeline also lives as a calendar subscription:
  `POST /api/feeds` stores the timeline (gated exactly like the
  iCalendar export — a dateless timeline is refused
  `no_dated_legs`) under a stable, unguessable feed id, and
  `GET /api/feeds/<id>.ics` renders it fresh on every fetch, so a
  calendar app subscribed to that URL polls the current series
  instead of importing a one-off file. The feed's owner replaces
  the timeline in place (`POST /api/feeds/<id>` — how a branch
  re-sealed after another swap reaches subscribers; the update
  renews the 30-day expiry) or revokes it (`DELETE
  /api/feeds/<id>`); feeds created with an owner token answer
  update/revoke only to that token, exactly like owner-managed
  share links, and only the token's hash is stored. With
  `serve --feed-file <path>` (or the `TASTEPILOT_FEED_FILE` env
  var) feeds persist across restarts in an atomically-written
  JSON file; `GET /api/health` reports the feed store, and the
  timeline view's "Subscribe to timeline feed (iCalendar)"
  button creates or updates the feed for the timeline on screen.
  Every fetch recomputes the verdicts: a tampered branch's series
  reads NOT VERIFIED under the same URL, never PASS.
* A plan also hands off to the user's own apps in one tap: every
  plan carries `handoffs` — per leg, a Google Calendar template
  link (dated legs only; a dateless leg gets no calendar link
  rather than an invented date) and a Google Maps link, plus a
  Maps link per recommendation (its name qualified by its leg
  location). The demo UI renders them (leg-level calendar/maps
  links, a per-card "open in Maps" link), the CLI prints the leg
  links, and Markdown/text exports carry a Handoffs section.
  Links are recomputed from the plan's schedule and items wherever
  they render — a `handoffs` field stored in a shared plan JSON is
  never trusted — and opening one acts entirely in the user's own
  browser/account: TastePilot signs in nowhere and sends nothing.
* The handoffs also speak each platform's native dialect: every
  leg and recommendation carries, beside its Google Maps URL, an
  Apple Maps URL (`maps.apple.com`, which opens the native Maps
  app on iOS/macOS) and a `geo:` URI (the Android maps intent) for
  the identical recomputed query — the UI renders the Apple Maps
  link beside the Google one. And every plan carries a share-sheet
  payload (`handoffs.share`: a title naming the legs in order,
  plus text with the request, each leg's dates and recommendation
  names, and a verification line **recomputed from the sealed
  evidence** — a tampered plan's payload reads NOT VERIFIED). The
  UI's "Share via OS share sheet ↗" button hands that payload to
  the Web Share API (`navigator.share`), so the user's own apps
  receive it; where no share sheet exists it falls back to copying
  the same text to the clipboard. The payload is capped at 1,000
  characters with per-leg item lists truncated behind a "+N more"
  note, and the verdict line always survives the cap. Like every
  handoff, it is recomputed on every surface — a `share` object
  stored inside a plan JSON is never read back.

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
