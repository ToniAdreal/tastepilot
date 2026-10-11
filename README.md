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
               stored timeline with verdicts recomputed;
               multi-timeline subscription feeds under stable
               /api/multi-feeds/<id>.ics URLs — a
               MultiTimelineFeedStore carrying a bundle of named
               timelines under one id, versioned with a capped
               change log and an ETag for poll-based change
               notification (If-None-Match → 304 while unchanged),
               same expiry/revocation/ownership/persistence rules);
               push webhooks on multi-feeds (notify.py — see the
               Demo section)
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
               one event series per branch and ics-days one paced
               event per dated day per branch (see the Demo
               section), and a dateless timeline refuses with
               no_dated_legs;
               export_multi_timeline_ics renders a bundle of named
               timelines as one calendar (one series per timeline
               per branch) under the same doctrine, and
               export_multi_timeline_paced_ics renders the same
               bundle by day (one paced event per dated day per
               timeline per branch); export_multi_timeline_markdown
               and export_multi_timeline_text render the bundle as
               one take-away grid document (a section per timeline
               under a bundle banner recomputed over every column
               of every timeline; no dates needed, unlike the
               calendars), and export_multi_timeline dispatches
               all four bundle formats — while subscription feeds
               stay calendar-only (MULTI_FEED_FORMATS)
  bundle.py    audit bundles: one deterministic ZIP carrying a
               plan exactly as supplied, its sealed evidence record
               on its own, a verification report recomputed at
               packing time (per-item checker reports, primary
               items + alternatives), the take-away itineraries
               (the calendar included only when the plan has a
               dated leg), and a manifest hashing every other
               file; verify_bundle re-audits an archive offline —
               manifest hashes checked, the packed plan's verdict
               recomputed, the packed report never trusted
  notify.py    push change notifications for calendar
               subscriptions: subscriber-registered webhooks on
               multi-feeds — a URL is validated (http(s), no
               credentials in the URL), stored only inside the
               feed entry, and named everywhere else by host
               label only; delivery is a plain JSON POST with
               bounded retries on transient failures through an
               injectable transport, no credential ever sent;
               deliveries to a signed webhook carry an
               HMAC-SHA256 signature header the subscriber can
               verify (secret supplied at registration, or
               generated and shown once); every delivery outcome
               is a record the feed store keeps (capped per feed)
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
  pacing.py    day-by-day pacing: each leg's primary items
               distributed deterministically (plan order, consecutive
               chunks as even as possible) across the leg's dated
               days; an undated (or end-before-start) leg gets one
               unscheduled bucket — no dates are ever invented;
               every primary item is paced exactly once and
               alternatives are never paced; recomputed from the
               plan's schedule + items on every surface, never
               read back from a stored plan field
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
               (TASTEPILOT_PROFILE_FILE), exactly like the share store;
               a profile also travels between stores as a profile
               sync document (export_sync / import_sync) merged
               under deterministic conflict rules — newer document
               wins, counts take the maximum — with every conflict
               reported, never silently resolved
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
* That profile also syncs between devices and servers:
  profile sync (`GET /api/profile/export`, `POST
  /api/profile/import`, `tastepilot profile --export /
  --import-file`, the profile panel's export/import controls)
  carries the profile as a portable document — format, version,
  the owner's fingerprint (never the token), an updated-at
  stamp, and the profile itself, shape-gated and clamped to the
  learner's caps on entry. Merging two copies is deliberately
  NOT the additive lesson merge: two devices that learned
  independently hold overlapping history, so a weight both
  sides hold with different values is a conflict resolved by
  rule (the newer document wins; a timestamp tie goes to the
  larger absolute weight, then the larger value) and the
  counts take the element-wise maximum, so shared history is
  never double-counted. Every conflict is listed in the import
  result with both values, the resolved value, and the rule
  that decided it; a document naming another owner's
  fingerprint is refused (`owner_mismatch`).
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
  And a plan leaves as one self-contained proof — an audit bundle:
  `tastepilot bundle --plan plan.json --out bundle.zip`, the demo
  UI's "Download audit bundle (.zip)" button, and
  `POST /api/bundle` pack the plan exactly as supplied, its sealed
  evidence record on its own, a verification report recomputed at packing time,
  the Markdown/text itineraries (plus the calendar when the plan
  has a dated leg), and a manifest carrying the SHA-256 and byte
  size of every other file. Packing is deterministic (fixed ZIP
  timestamps, no wall-clock anywhere), so two bundles of the same
  plan are byte-identical. A tampered plan still packs — its
  report reads NOT VERIFIED — and exits 1 on the CLI.
  `tastepilot verify-bundle --bundle bundle.zip` re-audits an
  archive offline: every file is checked against the manifest's
  hashes (a `plan.json` edited after packing fails the manifest
  check even if the edited plan would verify on its own) and the
  packed plan's verdict is recomputed from its sealed record —
  the packed `verification.json` is never trusted, because once
  the archive leaves TastePilot's hands it is only a claim.
  A branch timeline leaves the same way — a branch-timeline export:
  `tastepilot export --plan base.json --branch NAME=branch.json
  [--format markdown|text|ics|ics-days]`, `POST /api/export-timeline?format=...`,
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
  The timeline also exports as a paced iCalendar
  (`export_timeline` with format `ics-days`): one event series per
  branch at day granularity — for every dated day of that
  branch's own day-by-day pacing, an all-day event naming its
  branch and the day and carrying only that day's recommendations
  in that branch, under that branch's recomputed verdict, so a
  recipient can compare swap paths day by day. A tampered
  branch's days read NOT VERIFIED, paced timeline UIDs are
  namespaced apart from both the leg-level timeline UIDs and the
  single-plan paced UIDs, and a timeline with no dated day holding
  recommendations in any column refuses with `no_dated_legs` —
  the single-plan paced rule, per timeline.
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
  reads NOT VERIFIED under the same URL, never PASS. The feed's
  calendar response carries an ETag (`feed_etag` — a hash of the
  stored timeline; a single feed has no version or change log, so
  the payload itself is the change signal): a poller presenting
  it in `If-None-Match` gets a cheap `304` until an in-place
  update changes the etag, and re-posting the identical timeline
  keeps it. The same feed URL with `?format=ics-days` (also
  returned as the feed's `paced_url`) serves the
  paced variant of that subscription — one paced event per
  dated day per branch, summaries naming branch and day —
  under its own ETag
  (the feed etag with a `-days` suffix), so the two variants
  never answer each other's conditional polls; a tampered
  branch's paced days read NOT VERIFIED, exactly like its
  leg-level series.
* Many timelines also share one subscription: `POST
  /api/multi-feeds` stores a bundle of named timelines
  (`{"timelines": [{"name", "base", "branches"}, ...]}`, each
  gated exactly like the iCalendar export — a bundle with no
  dated leg anywhere is refused `no_dated_legs`) under one
  stable feed id, and `GET /api/multi-feeds/<id>.ics` renders
  the whole bundle fresh as ONE calendar on every fetch — one
  event series per timeline per branch, summaries naming both
  (`TastePilot [Japan-France / Museum]: Kyoto`). The same feed
  URL with `?format=ics-days` (also returned as the feed's
  `paced_url`) renders the bundle by day instead — one paced
  event per dated day per timeline per branch, summaries
  naming timeline, branch, and day — under its own ETag (the
  feed etag with a `-days` suffix), so the two variants never
  answer each other's conditional polls. The bundle also
  downloads without a subscription: `POST
  /api/export-multi-timeline?format=markdown|text|ics|ics-days`
  with the same `{"timelines": [...]}` body renders it as a
  take-away file under the same gates — the calendars as
  `tastepilot-multi-timeline.ics`, and markdown/text as one
  grid document (`tastepilot-multi-timeline.md` / `.txt`): a
  section per timeline (its own banner, columns line, and
  per-leg tables) under a bundle banner recomputed over every
  column of every timeline, so a tampered branch deep in the
  bundle is a FAIL column under a NOT VERIFIED bundle banner
  naming its timeline and column. The document formats need
  no dates — an undated bundle downloads as a document even
  though both calendars refuse it — and the CLI renders the
  same documents with `tastepilot export --bundle bundle.json
  [--format markdown|text|ics|ics-days]` (exit 1 when any
  column fails, the document still written). The timeline
  view's "Download bundle (Markdown)" / "(Text)" /
  "(iCalendar)" / "(iCalendar by day)" buttons drive the
  endpoint. Change
  notification is poll-based and explicit: the feed carries a
  version (incremented by every in-place update via `POST
  /api/multi-feeds/<id>`) and a capped change log (`GET
  /api/multi-feeds/<id>/changes` — one entry per creation and
  update, with the timeline names and the verdict recomputed
  at write time), and the calendar response carries an ETag —
  a poller presenting it in `If-None-Match` gets a cheap `304`
  until an update changes the etag, so subscribers learn a
  feed changed without downloading it. Push is opt-in per feed:
  a subscriber registers a webhook (`POST
  /api/multi-feeds/<id>/webhooks {"url": ...}`, at most 4 per
  feed, owner rule as for the feed) — registration fires one
  `multi_feed.subscribed` test delivery, and every in-place
  update then POSTs a `multi_feed.updated` notification (new
  version, etag, timeline names, recomputed verdict) to every
  registered webhook with bounded retries on transient
  failures. The URL is stored only inside the feed entry (it
  is the delivery address) and named everywhere else by host
  label only, so a query string never reaches a response or a
  log; URLs carrying credentials are refused. Each delivery's
  outcome is recorded in the feed's capped delivery log (`GET
  /api/multi-feeds/<id>/notifications`), and a failed delivery
  never fails the update that triggered it. A failed delivery
  can be redelivered on demand: every record is stamped with a
  `delivery_id` and keeps the exact payload it attempted, and
  `POST /api/multi-feeds/<id>/notifications/<delivery_id>/
  redeliver` resends that payload to the webhook's currently
  registered URL, recording the resend as a new delivery whose
  `redelivery_of` names the original — the original record is
  never edited, so the log stays an audit trail. A delivered
  record, a payload-less legacy record, and a removed webhook
  are categorised refusals; the timeline view's "Redeliver
  failed pushes" button drives the same flow. All of a feed's
  failed pushes can also be resent in one call —
  `POST /api/multi-feeds/<id>/notifications/redeliver-failed`
  resends the latest attempt of each failed delivery chain
  and reports per-record outcomes; a failed record a later
  resend already superseded is reported under `skipped` and
  never resent (resending it would duplicate a push its
  chain has moved past), as are payload-less legacy records
  and records whose webhook was removed, each with its
  reason — and the button now drives this bulk endpoint
  instead of looping the single-record one. Deliveries to a
  signed webhook carry an HMAC-SHA256 signature header
  (`X-TastePilot-Signature: sha256=…` over the exact body
  bytes, plus `X-TastePilot-Event`) so the subscriber can
  verify a push genuinely came from TastePilot and was not
  altered — the subscriber supplies their signing secret at
  registration (`"secret": …`, validated, never echoed), or
  the server generates one and returns it in the registration
  response only, shown once; an explicit `"secret": null`
  registers an unsigned webhook. Delivery records carry only
  a `signed` flag — the secret never reaches a listing, a
  record, or a log, and a redelivery of a signed delivery is
  signed with the same secret. Signed deliveries are also
  replay-protected: beside the legacy body signature they
  carry `X-TastePilot-Timestamp` (the delivery's Unix time,
  stamped once per delivery, so bounded retries of one
  delivery share it) and `X-TastePilot-Timestamp-Signature`
  (`sha256=…`, HMAC-SHA256 over `"<timestamp>." + body`
  under the same secret). A subscriber who runs
  `verify_timestamped_signature` rejects a push whose
  timestamp is more than 300 s
  (`SIGNATURE_TOLERANCE_SECONDS`) from now in either
  direction — so a captured push replayed later is
  rejected even though its body signature still verifies,
  while the legacy header and `verify_signature` stay
  byte-unchanged for subscribers who have not adopted
  the timestamped check. A redelivery is a new delivery
  and is stamped fresh. Every delivery — signed or
  unsigned — also carries an idempotency key
  (`X-TastePilot-Idempotency-Key` on the wire,
  `idempotency_key` on its record): a deterministic
  digest of the webhook id and the exact payload bytes
  (`idempotency_key_for`). The key names the logical
  delivery, not the attempt — bounded retries of one
  delivery share it, and a redelivery of the same stored
  payload to the same webhook reuses the original's
  key — so a receiver that already applied a change can
  drop the duplicate instead of applying it twice; a
  different payload or a different webhook is a
  different key. The key is not a secret and
  authenticates nothing (that remains the signatures'
  job), and a skipped (paused) record carries the key
  its delivery would have carried, so the redelivery
  that later makes the missed change good matches it.
  A webhook whose endpoint
  keeps failing is paused, not hammered: the store counts
  consecutive failed deliveries per webhook (a delivered
  push resets the count; the count folds in when a delivery
  is recorded, so pings, update pushes, and redeliveries all
  count the same way), and at 3 the webhook is paused —
  later pushes to it are recorded as `skipped` deliveries
  (`webhook_paused`, zero attempts, payload still stored, so
  the missed change stays redeliverable), redelivery to it
  is refused as `webhook_paused`, and bulk redelivery skips
  it with that reason. The subscriber resumes it with
  `POST .../webhooks/<id>/resume` (or pauses by hand with
  `.../pause`) under the same owner rule; resuming clears
  the count. Webhook listings report `paused`,
  `consecutive_failures`, and the pause provenance
  (`paused_reason`, `paused_at`, `probe_eligible_at`).
  An auto-pause is a circuit breaker with a half-open
  probe: after a 1-hour cooldown
  (`PAUSE_COOLDOWN_SECONDS`), the next update push is
  attempted against the auto-paused webhook as a probe
  (its delivery record is stamped `probe`) — a delivered
  probe revives the webhook with its count cleared, a
  failed probe restarts the cooldown from that moment.
  A webhook paused by hand (reason `manual`) is never
  probed: a deliberate pause is an instruction, not a
  symptom, and only the subscriber's resume restarts it.
  A signing secret rotates in place:
  `POST .../webhooks/<id>/rotate-secret` (owner rule as
  for the feed) replaces the secret without removing and
  re-registering the webhook — no body generates a fresh
  secret, returned in that response only and shown once,
  exactly as at registration; a supplied `"secret"`
  installs the subscriber's own (validated, never
  echoed); an explicit null un-signs the webhook. The
  old secret stops verifying immediately (no grace
  window — a rotation usually means a leak), and
  rotation is a management act, not a delivery: the
  webhook's id, pause state, failure count, and
  registration moment all survive, and a paused webhook
  can be rotated (it stays paused). The timeline view's
  "Rotate webhook signing secrets" button rotates every
  signed webhook on the feed and shows each new secret
  once, in its output, for the subscriber to save.
  A webhook's endpoint rotates in place, too:
  `POST .../webhooks/<id>/rotate-url` (owner rule as
  for the feed) points the webhook at a new endpoint
  URL — `{"url": …}` is required and validated exactly
  as at registration (categorised errors, the URL never
  echoed) — without removing and re-registering it.
  The webhook id is recomputed from the new URL (the id
  is the URL's hash everywhere), the rotation moment is
  stamped (`url_rotated_at` in listings), and the
  signing secret, pause state, failure count, and
  registration moment all survive; a paused webhook can
  be rotated (it stays paused), and rotation itself
  delivers nothing — the next update push simply goes
  to the new endpoint, still signed with the surviving
  secret. Rotating onto a URL another webhook on the
  feed already holds is refused (`webhook_exists`):
  one endpoint, one registration. Rotating to the
  current URL is idempotent and stamps nothing.
  Delivery records are never edited, so records from
  before a rotation keep the old id and host label as
  the audit trail (and can no longer be redelivered —
  their id names no current registration). The timeline
  view's "Rotate webhook endpoints" button asks for a
  new URL per webhook and reports each rotation's new
  host, moment, and id. A webhook subscribes to events,
  not to everything: an optional `"events"` list at
  registration (any subset of `multi_feed.subscribed`,
  `multi_feed.updated`, `multi_feed.created`; no list
  means every event) sets its filter, and
  `POST .../webhooks/<id>/set-events` replaces it in
  place (owner rule as for the feed) — `{"events": [...]}`
  is validated exactly as at registration (categorised
  `bad_webhook_events` errors, the supplied values never
  echoed), an explicit `{"events": null}` resets the
  webhook to every event, and the change is stamped
  (`events_updated_at` in listings) while the webhook's
  id, URL, signing secret, pause state, failure count,
  and registration moment all survive; setting the
  current filter is idempotent and stamps nothing. A
  push whose event a webhook filtered out is not sent
  to it at all: no attempt, no delivery record, and no
  failure-count effect — opting out is not failing, and
  it is not being paused either (a paused webhook's
  wanted pushes are recorded as `skipped`). That is
  also why a webhook whose filter excludes
  `multi_feed.subscribed` receives no registration test
  delivery (its registration response carries
  `"delivery": null`). The filter governs automatic
  pushes only: an on-demand redelivery is the
  subscriber's explicit act and resends regardless. The
  timeline view's "Set webhook event filters" button
  asks for a comma-separated list per webhook (empty
  resets to every event) and reports each webhook's new
  subscription. A webhook can also be test-pushed on
  demand: `POST .../webhooks/<id>/test` (owner rule as
  for the feed, no body) fires one `multi_feed.test`
  delivery to that webhook alone, carrying the feed's
  current version, etag, timeline names, and verdict —
  so a subscriber who just rotated an endpoint, rotated
  a signing secret, or replaced an event filter can
  verify the endpoint works (and that the signature
  verifies) without forcing a feed update. The test is
  the subscriber's explicit act, so it is sent
  regardless of the webhook's event filter (like an
  on-demand redelivery); a paused webhook is refused
  (`webhook_paused`), and the delivery is recorded and
  counts toward the webhook's delivery health like any
  push. The timeline view's "Test webhooks" button
  test-pushes each webhook on the feed and reports each
  outcome. A subscriber can also see all of their
  webhooks at once: the subscriber directory
  (`GET /api/subscriber/webhooks`, owner token required)
  lists every webhook registered on a multi-feed the
  presenting token owns, across all of that owner's
  feeds — each row naming its feed (id, version,
  timeline names, expiry) beside the webhook's host
  label, signed/paused state, and event filter, plus
  its delivery tallies (delivered / failed / skipped
  and the most recent status) from the feed's log.
  The directory is read-only — management stays on the
  per-feed routes under the same owner rule — and
  webhooks on unowned feeds, or on another owner's
  feeds, never appear in it; listings carry host labels
  only, never a stored URL or secret. The demo page's
  "Your webhook subscriptions" panel renders it.
  Subscriptions themselves have the same cross-feed
  view: the subscriber feed directory
  (`GET /api/subscriber/feeds`, owner token required)
  lists every live calendar subscription the presenting
  token owns — single-timeline feeds and multi-timeline
  feeds in one response — each row with its stable feed
  URLs (leg-level and paced), expiry, branch/timeline
  names, and the verdict recomputed from its stored
  timeline at list time, so a feed whose branch no
  longer verifies lists as FAIL, never as the verdict
  it was created with. The directory is read-only —
  update and revocation stay on the per-feed routes
  under the same owner rule — and feeds owned by
  another token, or by no token, never appear in it;
  rows carry metadata only, never a stored payload.
  The demo page's "Your calendar subscriptions" panel
  renders it. And one view summarises all of it: the
  subscriber overview
  (`GET /api/subscriber/overview`, owner token required)
  aggregates the same rows the shares dashboard, the
  feed directory, and the webhook directory return —
  counts of share links, calendar feeds of both kinds,
  and webhooks, share and feed verdict tallies
  recomputed at list time, webhook delivery tallies,
  and whether a persistent preference profile exists
  for the token — beside those rows themselves, so the
  overview can never disagree with the directory it
  summarises. A token with nothing yet gets zero
  counts, not an error. The overview is read-only, and
  artefacts owned by another token, or by no token,
  never appear in it; no stored payload, webhook URL,
  signing secret, token, or full hash ever appears.
  The demo page's "Your subscriber overview" panel
  renders it.
  Webhooks are
  listed (`GET .../webhooks`, host labels only) and removed
  (`DELETE .../webhooks/<id>`) under the same owner rule, and
  persist with the feed in file mode. Bundles expire after 30 days
  (an update renews), can be revoked (`DELETE
  /api/multi-feeds/<id>`), follow the same owner-token rules
  as feeds, and persist across restarts with `serve
  --multi-feed-file <path>` (or the `TASTEPILOT_MULTI_FEED_FILE`
  env var); `GET /api/health` reports the multi-feed store,
  and the timeline view's bundle controls ("Add this timeline
  to a multi-timeline bundle" + "Create multi-timeline
  subscription") build and publish a bundle from the timelines
  explored on the page. A tampered branch in any timeline
  renders its series NOT VERIFIED under the same URL.
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

* Every plan also carries a day-by-day itinerary (`itinerary`,
  built by pacing.py): each dated leg's primary recommendations
  are paced across its days in plan order — consecutive chunks
  as even as possible, earlier days taking the remainder — so
  the trip reads day by day instead of only domain by domain.
  An undated leg (or one whose end precedes its start, which the
  schedule already warns about) is never assigned invented
  days: its items sit in a single unscheduled bucket. Every
  primary item is paced exactly once; alternatives (swap
  options) are not paced. The demo UI renders a "Day-by-day
  itinerary" section, the CLI prints one `itinerary:` line per
  day, and Markdown/text exports carry a Day-by-day itinerary
  section — all recomputed from the plan's schedule + items,
  never read back from a stored `itinerary` field. The same
  pacing drives a paced iCalendar export (format `ics-days`,
  alongside the leg-level `ics`): one all-day event per dated
  day carrying only that day's recommendations — empty days and
  undated legs get no event, a dateless plan fails with the same
  `no_dated_legs` category, and paced UIDs are namespaced apart
  from the leg calendar's so both files import side by side
  without collisions.

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
