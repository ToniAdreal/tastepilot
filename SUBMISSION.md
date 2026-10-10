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
   the seed entities you named.
2. **Plans** by decomposing the scenario into taste domains (film,
   music, food, places, books, brands, destinations).
3. **Calls Qloo** — seeds are resolved with `GET /search`, and
   cross-domain recommendations come from `GET /v2/insights`
   (`filter.type` + `signal.interests.entities`). Qloo is load-bearing:
   remove it and there is nothing left to recommend from.
4. **Composes** a grouped itinerary where each item carries a "why this
   fits your taste" line grounded in the Qloo correlation that produced
   it (Nemotron via Nebius Token Factory, with a deterministic template
   fallback; the LLM writes explanations only — it can never create a
   recommendation).
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
   record alone — the record's own verdict is never trusted.

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

- Richer seed capture (paste a playlist, a reading list, a watchlist)
  feeding the same evidence-checked loop.
- Multi-city trip composition with per-leg taste continuity.
- Shareable verification links, so a plan's audit result travels with
  the plan instead of needing the JSON pasted by hand.

## Built with

`python` · `qloo-api` · `nebius-token-factory` · `nemotron` ·
`html/css/javascript` · `docker`

## Links

- Public repo: **[FINALIZE: repo URL — created Oct 27]**
- Hosted demo: **[FINALIZE: demo URL — deployed Oct 21–24 window]**
- License: MIT
