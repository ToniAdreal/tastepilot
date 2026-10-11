# TastePilot — Devpost submission text (DRAFT)

> Draft for the Qloo Agentic Hackathon submission on Devpost.
> The main agent submits on Oct 29; bracketed **[FINALIZE: …]** slots are
> filled from the real-mode run and the hosted deployment before then.
> All copy is truthful about the build: this is Toni Adreal's solo entry,
> built with AI assistance (agent-orchestrated implementation; the
> concept, the evidence invariant, and the verification design are the
> entrant's), per the hackathon rules §8.2 role verification.

## Project name

TastePilot

## Tagline

A taste-aware planning agent where every recommendation must trace to a
real Qloo API response — no trace, no recommendation.

## Inspiration

AI planners hallucinate. Ask a generic assistant for "a slow weekend in
Kyoto" and it will invent charming ramen counters and jazz bars that no
data source ever supported — stated with total confidence. Taste is one
of the few domains with a real, structured source of truth behind it:
Qloo's taste graph knows that loving a film genuinely predicts the
restaurants, music, and places you will love. TastePilot was built to
make that connection trustworthy: the agent may propose, but a
deterministic evidence layer disposes.

## What it does

You state a plan in natural language — *"a slow weekend in Kyoto, I love
Spirited Away and Kind of Blue"*, *"a first date in NYC"*, *"a birthday
gift shortlist for someone who loves Amélie"*. TastePilot:

1. **Parses** the request deterministically — scenario, location, and
   the seed entities you named. Seeds can also arrive as a pasted
   list — a watchlist, a playlist, a reading list — and every item
   becomes a seed, including titles outside the demo catalogue, which
   are passed verbatim to Qloo search. Seeds can also arrive as an
   exported taste file the user downloaded from their own accounts —
   a Spotify playlist export, a Letterboxd watchlist CSV, a Goodreads
   library CSV (or a generic CSV/JSON/text list) — parsed entirely
   offline by `plan --import-file`, with the file's format and items
   recorded as provenance in the plan; no account login is involved.
   The same files can be attached directly on the demo page: the
   browser reads the file and sends its contents with the plan
   request, the server parses them with the same parsers for that
   plan only (no upload store, nothing persisted), and a timetable
   file attaches the same way for the feasibility check below.
   The same schema can also arrive from a taste-feed endpoint
   serving it over HTTP (`plan --import-url`): fetched once with
   bounded retries, no credential ever sent, with only the
   endpoint's host recorded as provenance and its format stamped
   as an API source — a fetched list is never presented as a
   downloaded file, or vice versa.
2. **Plans** by decomposing the scenario into taste domains (film,
   music, food, places, books, brands, destinations). A request naming
   several cities composes a multi-city trip: each city is a leg,
   planned in mention order with the same resolved seeds — your taste
   carries across legs — while each leg's Qloo calls are
   location-filtered and sealed separately, so a leg's items cite that
   leg's own responses. Legs are date-aware: explicit, range, duration,
   and relative dates attach to the leg whose city was most recently
   mentioned, the plan reports per-leg start/end/nights, and
   out-of-order, overlapping, or reversed legs are surfaced as
   schedule warnings — never silently repaired, never blocking.
   Each leg change also passes an inter-leg travel-time feasibility
   check: an offline door-to-door estimate (great-circle distance
   between the leg cities; surface travel under 400 km, air travel
   above) is compared against the window the schedule leaves for the
   transfer, and tight or physically impossible transfers surface
   as travel warnings in the same report-never-repair shape. The
   estimate source is pluggable: a timetable file the user supplies
   (the scheduled flights and trains they are actually considering,
   parsed entirely offline) replaces the estimate for the city pairs
   it covers — and so does a timetable API endpoint serving the same
   schema over HTTP, fetched once with bounded retries, with only
   its host recorded as provenance — and every transfer records
   which source judged it — a scheduled duration is never presented
   as an estimate, or vice versa.
3. **Calls Qloo** — seeds are resolved with `GET /search`, and
   cross-domain recommendations come from `GET /v2/insights`
   (`filter.type` + `signal.interests.entities`). Qloo is load-bearing:
   remove it and there is nothing left to recommend from.
4. **Composes** a grouped itinerary where each item carries a "why this
   fits your taste" line grounded in the Qloo correlation that produced
   it (Nemotron via Nebius Token Factory, with a deterministic template
   fallback; the LLM writes explanations only — it can never create a
   recommendation). A request can also state negative preferences —
   "no jazz", "avoid ramen and wine", "allergic to shellfish" — which
   are captured deterministically and applied as a filter after the
   evidence check: an excluded item is reported with the phrase it
   matched and its (passing) verification, removed by preference and
   never confused with a checker rejection. And each plan carries
   evidence-backed alternatives: every insights call asks Qloo for
   two more results than the plan shows, and the overflow is offered
   as a swap option for the same domain and leg — checker-verified
   against the sealed record before it is kept, filtered by the
   same exclusions, and covered by every verification surface, so
   tampering with an alternative fails verification exactly as
   tampering with a primary item does. An alternative is also a
   one-tap swap: its card's swap button exchanges it with the
   primary for its domain and leg, but only after the swap module
   recomputes the plan's preconditions (sealed chain valid, whole
   plan passing, both items traced) — a tampered plan is refused,
   never re-sealed — and the exchange itself is then sealed into
   the evidence record as a swap event, so the re-signed record
   attests the original Qloo calls and every swap at once. The
   swap history is an audit trail every surface recomputes from
   those sealed events — the UI's history block, the verify
   report, the itinerary exports, a `history` command — never
   from a plan's stored swap list, and the newest swap can be
   reversed with one Undo tap: the reversal is read from the sealed
   history, re-checked against every precondition, and sealed
   as a further event naming the swap it reversed, so the
   record attests swaps and their undos alike and nothing is
   ever deleted from the chain. And swap paths can be compared
   before committing to one: a branch spec forks the plan into
   named branches — one swap path each, applied to the base
   independently through the same re-check-and-reseal path —
   and the explorer (UI button, API, and CLI alike) lines the
   branches up by their recomputed verdicts, with a path that
   does not apply reported beside the successful branches
   rather than aborting the comparison; loading a branch gives
   an ordinary swapped plan that verifies, undoes, and swaps
   further like any other. And the agent guides which branches
   are worth exploring: guided branch suggestions rank every
   verified primary/alternative pair by affinity delta and
   taste coverage (tags the alternative would add that no
   primary carries), and propose the top single swaps plus a
   combined guided-tour branch picking the best swap in each
   domain — as branch specs the explorer accepts unchanged,
   with a score and rationale per suggestion. Suggestions
   propose only: the base is verified first, never mutated,
   and a tampered plan is refused rather than suggested from.
   And the ranking learns from the user's own past swap choices:
   swap events sealed in a plan's evidence record — plus any
   history plans supplied with the request — are read back as
   training signal, where a swap the user kept adds weight to
   the tags and domain of the alternative they chose and a
   swap they later undid subtracts it (undoing an undo restores
   it). The resulting profile folds into each candidate's score
   as a bounded, fully reported adjustment — every candidate
   names the learned tags behind its adjustment, and a history
   plan whose sealed chain does not verify is ignored and
   counted rather than believed, so the personalisation is as
   tamper-evident as everything else in the plan.
   Persistent preference profiles carry that learning across
   plans and sessions: the profile is stored under the hash of
   the owner's token (the same per-browser credential that owns
   share links — no account, no sign-up), written only by
   deriving it from sealed swap histories and merging it
   additively under the learner's own caps (no surface accepts
   caller-supplied weights), optionally persisted to a file so
   it survives restarts, clearable by its owner at any time,
   and folded into later suggestion rankings for that token —
   reported as a stored profile applied — while other owners'
   rankings are never touched. And any two plans can be compared
   directly: the diff names the items one plan has that the other
   lacks, the affinity changes on the slots both keep, and the
   alternatives and schedule changes between them — with both
   plans' verdicts recomputed from their own sealed records, so
   the branch explorer can state exactly what a swap path changed
   (+added, −removed, ~rescored) instead of asking the reader to
   eyeball two item lists. And the branches render as a visual
   timeline: recommendation slots grouped by the trip's legs,
   one column per branch beside the base plan, each cell marked
   kept, added, removed, or re-scored with its affinity, and
   each column headed by its recomputed verdict — a tampered
   branch is a FAIL column in the grid, never a clean one.
   And that timeline also leaves as one of the plan's take-away
   exports: the same grid rendered into Markdown and plain-text
   itinerary exports (CLI, API, and the explorer's download
   buttons), its banner and every column verdict recomputed at
   export time — a tampered branch exports as a FAIL column
   under a NOT VERIFIED banner, exactly as a tampered single
   plan exports. And the timeline leaves as a calendar too, as
   one of the timeline take-away exports: an iCalendar export
   with one event series per branch — for
   every dated leg, an all-day event naming its branch and
   carrying that branch's recomputed verdict and that leg's
   recommendations in that branch — so a recipient can compare
   swap paths on their own calendar; a tampered branch's series
   reads NOT VERIFIED, and a timeline with no dated leg in any
   column is refused rather than assigned invented dates.
   And that calendar also travels as live calendar subscriptions,
   not just a file: a timeline can be stored as a feed under a stable
   URL that a calendar app subscribes to and polls — every poll
   re-renders the stored timeline with its verdicts recomputed
   — and the feed's owner can replace the timeline in place
   when a branch is re-sealed, so subscribers see the new
   series under the same URL instead of importing a new file;
   feeds expire, can be revoked, and follow the same
   owner-token rules as share links. And several timelines
   travel together as one multi-timeline subscription: a
   bundle of named timelines stored under a single feed URL
   whose calendar carries every timeline's branch series at
   once, each timeline's verdicts recomputed on every poll.
   The bundle feed is versioned and keeps a change log —
   one entry per creation and per in-place update, naming
   the timelines it carries and the verdict recomputed at
   write time — and its calendar response carries an ETag,
   so a subscriber polling with If-None-Match gets a cheap
   304 while nothing changed and the new series (with a new
   etag) the first poll after an update: poll-based change
   notification. And change also pushes: a subscriber can
   register a webhook on the bundle feed — registration
   fires one test delivery, and every in-place update then
   POSTs the change (new version, etag, timeline names,
   recomputed verdict) to every registered webhook with
   bounded retries, each delivery's outcome recorded in the
   feed's delivery log; the webhook URL is stored only
   inside the feed entry and named everywhere else by host
   label only, and a failed delivery never fails the update
   that triggered it. A failed delivery is not lost, either:
   every record is stamped with a delivery id and keeps the
   payload it attempted, so the subscriber can have it
   redelivered on demand — the resend goes to the webhook's
   current URL and is recorded as a new delivery naming the
   original, which is never edited — or redelivered in bulk:
   one call resends the latest attempt of every failed
   delivery chain on the feed, while a failed record a later
   resend already superseded is reported as skipped and never
   resent, so a recovered receiver is brought up to date
   without duplicate pushes. Pushes are verifiable,
   too: a signed webhook's deliveries carry an HMAC-SHA256
   signature header over the exact body bytes — the
   subscriber supplies the signing secret at registration
   or has one generated and shown exactly once — so a
   receiver can prove a push genuinely came from TastePilot
   and was not altered, and a signed delivery's redelivery
   is signed the same way. A webhook whose endpoint keeps
   failing is paused, not hammered: three consecutive
   failed deliveries pause it (a delivered push resets the
   count), later pushes are recorded as skipped with their
   payload kept — so the missed change can be redelivered
   once the subscriber resumes the webhook, which clears
   the count. An auto-pause is a circuit breaker, not a
   life sentence: it is stamped with its reason and the
   moment it paused, and after a one-hour cooldown the
   next update push probes the endpoint with that push
   itself — a delivered probe revives the webhook with
   its count cleared, a failed probe restarts the
   cooldown — while a webhook the subscriber paused by
   hand is never probed, because a deliberate pause is
   an instruction, not a symptom. Secrets are
   maintainable, too: a webhook's signing secret
   rotates in place — a fresh secret generated and
   shown once, the subscriber's own installed, or an
   explicit null un-signing the webhook — and the old
   secret stops verifying the moment the new one is
   stored, without the webhook losing its id, pause
   state, or failure count to a remove-and-re-register.
   Endpoints are maintainable the same way: a webhook's
   URL rotates in place (`rotate-url`) — validated
   exactly as at registration, the id recomputed from
   the new URL, the rotation moment stamped — and the
   signing secret, pause state, failure count, and
   registration moment all survive, so an endpoint
   migration costs the subscriber nothing but the edit;
   rotating onto an already-registered URL is refused,
   and delivery records keep the old id and host as the
   audit trail. Subscriptions are selective, too: a
   webhook names the events it wants — at registration
   or later in place (`set-events`), with an explicit
   null resetting it to every event — and a push whose
   event it filtered out is never sent to it (no attempt,
   no delivery record, no failure-count effect), while
   an explicit on-demand redelivery still resends,
   because that is the subscriber's own act. And a
   webhook can be test-pushed on demand
   (`POST .../webhooks/<id>/test`): one
   `multi_feed.test` delivery to that webhook alone,
   sent regardless of its filter and refused while it
   is paused, so a subscriber who rotated an endpoint
   or a secret can verify the endpoint — and the
   signature — without forcing a feed update.
5. **Evidence-verifies** the result. Every Qloo call is sealed in a
   hash-chained, HMAC-signed evidence log. A deterministic checker
   admits an item only if its cited response exists in the sealed log,
   is an insights call, contains the entity, and matches the affinity
   exactly. Anything else is rejected as a hallucination and shown as
   rejected, with the reason. The web UI surfaces this per item: checker
   badge, the affinity found in the cited response, and an
   evidence-record viewer that opens the sealed Qloo call behind any
   recommendation. And because a plan is just JSON, anyone can audit
   one they did not produce: the UI's "Verify a shared plan" panel
   recomputes the chain and every checker verdict from the sealed
   record alone — the record's own verdict is never trusted. Plans
   also travel as shareable verification links: a plan can be stored
   under a content-addressed id, and anyone opening the link gets the
   plan back with its verification recomputed — storing grants a plan
   no trust, so a tampered shared plan still fails by link. Links
   expire after 7 days, can be revoked by anyone holding them, and —
   with an opt-in JSON file store — survive a server restart. Links
   can also be owner-managed without any account system: the browser
   that creates a link presents an owner token (generated locally,
   kept in localStorage; the server stores only its hash), and only
   that token can list the owner's links — each shown with its
   recomputed verdict — or revoke them. The token is portable:
   every owned surface names it only by a fingerprint (the first
   12 characters of its hash), and pasting the token into another
   browser — validated against the server before it is adopted —
   gives that browser the identical dashboard, so a user's links
   follow them between devices with no sign-up at all. And a plan leaves the app
   as a take-away itinerary: Markdown, plain-text, and iCalendar
   exports (one all-day event per dated leg; a dateless plan is
   refused rather than assigned invented dates), each carrying a
   verification banner recomputed at export time — a tampered
   plan exports as NOT VERIFIED, never clean. And a plan hands
   off to the user's own apps in one tap: a Google Calendar
   template link per dated leg and a Google Maps link per leg
   and per recommendation, rendered in the UI, the CLI, and the
   Markdown/text exports — recomputed from the plan on every
   surface (a stored link field is never trusted), with no
   account or sign-in on TastePilot's side: the user's own
   browser completes the action. The same handoffs also come in
   each platform's native dialect — an Apple Maps link and an
   Android `geo:` intent for every leg and recommendation — and
   as an OS share sheet payload (Web Share API): a title and a
   short text naming the legs and their recommendations, whose
   verification line is recomputed from the sealed evidence, so
   a plan shared to the user's own messaging or notes apps
   carries its PASS — or its NOT VERIFIED — with it.

**[FINALIZE: one real-mode example — a plan produced against the live
Qloo API, with its item count and checker verdict.]**

## How we built it

Python 3.11+, standard library only — no dependencies to install, so
judges can run everything offline. A hand-rolled, transparent agent loop
(parse → plan → Qloo tool calls → compose → evidence-verify) instead of
a black-box framework. The Qloo adapter has one interface with two
clients: a real client for the hackathon API (`X-Api-Key` header, key
kept only in a gitignored `.env`, never logged) and a deterministic mock
client whose fixtures follow the public API examples, so the full flow —
including the evidence checker — runs in a zero-key Judge Mode. The
evidence log's hash-chain + HMAC design is ported from the entrant's
VerifyGate project. A one-command smoke check (`tastepilot smoke`)
validates the live payload shape on first real-key use, seals both
calls, and re-verifies the chain before reporting. The demo server is
hardened for a public URL (input caps, security headers, bounded
evidence cache, leak-free error responses) and ships with a Dockerfile,
a PaaS Procfile, and a deployment guide.

## Challenges we ran into

- **Response-id collisions:** places and food share Qloo's
  `urn:entity:place` filter type, so identical request parameters first
  produced identical response ids and legitimate items were rejected by
  our own checker. The id now also hashes the result entity ids — the
  checker catching our own bug was the invariant working as designed.
- **Reasoning-model token budgets:** the explanation model's reasoning
  trace silently consumed the whole completion budget and returned empty
  content with HTTP 200. The failure is now recorded as provenance
  (source, model, fallback reason) on every item instead of being hidden.
- **Keeping the LLM honest by construction:** the architecture had to
  guarantee — not merely prompt — that no language-model output can
  become a recommendation. The checker, not the model, has the final
  say, and it re-derives admissibility from the sealed evidence alone.

## Accomplishments we're proud of

- A recommender whose every claim is independently re-verifiable:
  `tastepilot verify` re-checks a saved plan against its sealed evidence
  record, and editing any affinity in the file makes verification fail —
  tamper-evidence you can try yourself in the repo samples.
- **[FINALIZE: test count at submission]** automated tests, all green in
  mock mode, covering the mock and real clients, the evidence chain
  (including tamper cases), the checker, the smoke ritual, the hardened
  server, and end-to-end agent runs.
- Zero-install Judge Mode: the entire demo, checker included, runs from
  a fresh clone with no keys and no `pip install` of the app itself.

## What we learned

Cross-domain taste transfer is only impressive if it is accountable.
The most valuable component turned out not to be the agent loop but the
boring deterministic layer underneath it: once every recommendation
carries a verifiable citation to the taste graph, the agent's fluency
stops being a liability and becomes pure upside.

## What's next for TastePilot

- Named taste-account API integrations — a specific provider's own
  API (the Spotify Web API's OAuth flow and schema, a Letterboxd
  API) wired behind the built generic HTTP taste-feed provider, so
  seeds refresh themselves from that account instead of arriving as
  a downloaded file or a generic-schema endpoint.
- Named timetable API integrations — a specific flight/train
  provider's own API (its auth, its schema) wired behind the built
  generic HTTP timetable provider, so scheduled services refresh
  themselves from that provider instead of arriving as a supplied
  file or a generic-schema endpoint.
- Full user accounts (sign-up/login) on top of the built
  token-owned share management and token portability, so a user's
  identity is an account they sign into instead of a token they
  carry between browsers themselves.
- A native mobile app on top of the built share-sheet payload
  and platform maps links, with two-way calendar sync, instead
  of the one-tap template/search links and one-way share
  payload the handoff layer already produces.
- Cloud-synced, account-backed preference profiles — the
  built token-owned persistent profile (derived only from
  sealed swap histories, stored beside the token-owned share
  data) graduating to a synced account artefact that follows
  the user across devices without them carrying a token, with
  conflict resolution when two devices save lessons offline,
  instead of the single-server store the built profile uses.
- Hosted push-notification accounts for calendar
  subscriptions — the built generic webhooks (a subscriber
  registers an endpoint on their multi-timeline feed;
  registration pings it once, and every in-place update
  pushes the change to it with bounded retries, each
  delivery recorded in the feed's log, a failed
  delivery redeliverable on demand from its stored payload,
  deliveries HMAC-signed with a per-webhook secret the
  subscriber supplies or has generated and shown once —
  a secret that also rotates in place, the old one
  stopping verifying the moment the new one is stored,
  and an endpoint URL that likewise rotates in place,
  the id recomputed from the new URL while the secret,
  pause state, and registration moment survive, and an
  event filter the subscriber sets at registration or
  replaces in place, so a webhook receives only the
  events it named and a filtered-out push is never
  sent to it at all, and an on-demand test push the
  subscriber can fire at any time to verify a rotated
  endpoint or secret without forcing a feed update —
  and a webhook auto-paused after consecutive failures until
  the subscriber resumes it or a half-open probe — the
  next update push after a one-hour cooldown — revives it)
  graduating to a hosted notification service with
  subscriber accounts — managed endpoints, email delivery,
  and provider-side delivery guarantees — instead of
  endpoints a subscriber registers per feed on a single
  server.

## Built with

`python` · `qloo-api` · `nebius-token-factory` · `nemotron` ·
`html/css/javascript` · `docker`

## Links

- Public repo: **[FINALIZE: repo URL — created Oct 27]**
- Hosted demo: **[FINALIZE: demo URL — deployed Oct 21–24 window]**
- License: MIT
