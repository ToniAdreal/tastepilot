# Deploying the TastePilot demo

The demo is a single dependency-free Python process (standard library
only), so any container or PaaS host that runs Python 3.11+ can serve it.
Judge Mode (mock Qloo adapter, deterministic template explanations) needs
**zero keys** and is the default posture for a public demo URL.

## Option A — Docker (any container host)

```bash
docker build -t tastepilot .
docker run -p 8765:8765 tastepilot
# open http://127.0.0.1:8765 — health: GET /api/health
```

The image runs as a non-root user, carries a `/api/health` healthcheck,
and binds `0.0.0.0`; the port comes from the `PORT` environment variable
(platforms that assign a port work unchanged).

## Option B — PaaS (Procfile)

Hosts that honour a `Procfile` (or an equivalent start command) can run:

```
web: python3 -m tastepilot serve --host 0.0.0.0 --mode auto
```

No build/install step is required — there are no dependencies.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `PORT` | no | Bind port (default 8765). Set by most platforms automatically. |
| `QLOO_API_KEY` | no | When set (platform secret store only), `--mode auto` serves **real** Qloo responses; when unset it serves mock Judge Mode. Never bake the key into an image or commit it — `.env` is gitignored and dockerignored. |
| `TASTEPILOT_SIGNING_KEY` | no | Overrides the development HMAC key for evidence records. |
| `TASTEPILOT_SHARE_FILE` | no | Path of a JSON file the share-link store persists to (atomic writes), so verification links survive restarts. Needs a writable, persistent volume; unset = in-memory links. The CLI equivalent is `serve --share-file <path>`, and `--share-ttl-days` tunes the 7-day default link lifetime. The file holds only shared plan payloads (fictional demo data) and, for owner-managed links, SHA-256 hashes of owner tokens — never the tokens themselves, and never the Qloo key. |
| `TASTEPILOT_FEED_FILE` | no | Path of a JSON file the timeline calendar-feed store persists to (atomic writes), so subscription URLs survive restarts. Needs a writable, persistent volume; unset = in-memory feeds. The CLI equivalent is `serve --feed-file <path>`, and `--feed-ttl-days` tunes the 30-day default feed lifetime (an owner's in-place update renews it). The file holds only stored timeline payloads (fictional demo data) and, for owner-managed feeds, SHA-256 hashes of owner tokens — never the tokens themselves, and never the Qloo key. |
| `TASTEPILOT_PROFILE_FILE` | no | Path of a JSON file the persistent preference-profile store persists to (atomic writes), so learned suggestion profiles survive restarts. Needs a writable, persistent volume; unset = in-memory profiles. The CLI equivalent is `serve --profile-file <path>`. The file holds only owner-token SHA-256 hashes and profiles derived from sealed swap histories — never the tokens themselves, never caller-supplied weights, and never the Qloo key. |
| `TASTEPILOT_MULTI_FEED_FILE` | no | Path of a JSON file the multi-timeline calendar-feed store persists to (atomic writes), so bundle subscription URLs (with their versions and change logs) survive restarts. Needs a writable, persistent volume; unset = in-memory multi-feeds. The CLI equivalent is `serve --multi-feed-file <path>`, and `--multi-feed-ttl-days` tunes the 30-day default lifetime (an owner's in-place update renews it). The file holds only stored timeline bundles (fictional demo data), change-log entries, and, for owner-managed feeds, SHA-256 hashes of owner tokens — never the tokens themselves, and never the Qloo key. |

For live Nemotron explanations instead of the deterministic template,
append `--llm nebius` to the start command; every explanation records
its provenance, and any Nebius failure falls back to the template with
the reason recorded (the demo never goes down because the LLM did).

## Post-deploy smoke checklist

1. `GET /api/health` → `{"ok": true, "mode": "mock"|"real", "explainer": {...}}`
2. `POST /api/plan` with `{"text": "a slow weekend in Kyoto, I love Spirited Away"}`
   → items present, `checker.passed: true`, `checker.chain_valid: true`.
3. Open `/` and confirm the plan renders grouped by domain, each card
   shows its checker badge, and the evidence viewer opens a record.
4. Oversize probe: a `POST /api/plan` body over 16 KiB → `413`
   (hardening is live, not just written down).
5. Paste a repo sample plan (`samples/plan_weekend_kyoto.json`) into the
   UI's "Verify a shared plan" panel → `PASS` with every item `ok`;
   edit one affinity in the pasted JSON and re-verify → `FAIL` with
   `affinity_mismatch`.
6. Share-link probe: create a shareable link for a generated plan (or
   for the pasted sample) and open it in a fresh tab → the shared plan
   loads with its verification recomputed to `PASS`, with the link's
   expiry shown. Links expire 7 days after creation and are
   oldest-evicted after 128; `DELETE /api/share/<id>` revokes one
   immediately (it 404s afterwards). Links created in the UI are
   owner-managed: “Your share links” in the same browser lists the
   new link with a `PASS` verdict, while a `DELETE` sent without that
   browser's owner token returns `403` and the link stays live.
   Portability: “Your share links” shows a token fingerprint; copy
   the token, paste it under “Use this token on this browser” in a
   second browser → the fingerprint matches and the same dashboard
   (same links, `PASS` verdicts) appears there.
   Without `TASTEPILOT_SHARE_FILE`
   links live in the server process — a restart clears them; with it
   set (on a persistent volume) they survive restarts.
7. Export probe: export a generated plan (the "Export itinerary as"
   row, or `POST /api/export?format=markdown` with a sample plan) →
   the download carries a `Verification: PASS` banner; export a
   tampered copy → the banner reads `NOT VERIFIED`. iCalendar export
   of a dated multi-city plan yields one all-day event per leg;
   of a dateless plan → `400 no_dated_legs`.
8. Handoff probe: generate a dated plan → each leg shows an
   "Add this leg to Google Calendar" link (all-day, end date
   exclusive) and every card shows an "open in Maps" link; a
   dateless plan shows Maps links only — no calendar link is
   ever rendered for a leg without dates.
10. Attachment probe: attach a taste export (e.g. a Letterboxd-style
   CSV) on the demo page and plan → the status line names the import
   (basename + format) and the plan's `imports` carries it; attach a
   malformed file → a categorised `400` (`import failed: …`) with no
   file content echoed.
9. Native-integration probe: each leg also shows an "Open in
   Apple Maps" link (the plan JSON's legs/items also carry
   `maps_geo_url` Android `geo:` intents for the same queries),
   and "Share via OS share sheet ↗" presents the plan's
   recomputed share payload (`handoffs.share`: title naming the
   legs, per-leg items, and a `Verification: PASS` line) to the
   browser's share sheet — on a browser without the Web Share
   API the same payload is copied to the clipboard instead.
11. Swap probe: on a plan with alternatives (e.g. the Kyoto
   weekend), click an alternative card's "Swap in for …" button →
   the plan re-renders with the two items exchanged, the status
   line shows `Swaps: 1 (…, re-sealed)`, and the evidence index
   names a new record hash whose verdict is PASS; pasting the
   swapped plan into the verify panel still PASSes, and editing
   any affinity in it FAILs.
12. Undo probe: after the swap probe, the plan shows a "Swap
    history (sealed audit trail)" block naming the exchange;
    click "Undo last swap" → the original primary is restored,
    the history block lists two sealed events (the second marked
    as the undo of the first), and the plan still PASSes in the
    verify panel, whose output now includes the two-line sealed
    swap history.
13. Branch probe: on a plan with alternatives, click "Explore
    branches from alternatives" → one comparison line per branch
    (the plan as-is plus one swap-in branch per alternative),
    each with its recomputed PASS, chain validity, and swap
    count; "Load this branch" on a swap branch re-renders the
    plan with that branch's item primary, the status line names
    the branch, and the loaded plan still PASSes in the verify
    panel.
14. Suggest probe: on the same plan, click "Suggest branches" →
    ranked guided suggestions appear (score, affinity delta,
    coverage gain, rationale per line, the combined guided-tour
    branch last); "Explore suggested branches" compares those
    exact branches with recomputed PASS verdicts. A tampered
    plan posted to `POST /api/suggest` → `400
    suggest failed: plan_not_verified`, never suggestions.
15. Learned-ranking probe: swap one item in a plan and keep it,
    then click "Suggest branches" on the swapped plan → a
    "Learned from your past swaps: 1 kept · 0 undone" line
    appears and the affected candidates carry a learned
    adjustment in their rationale. Undo the swap, suggest again
    → the same tags now adjust negatively. A chain-broken plan
    in the request's `history` list is counted under
    `plans_ignored` and changes no score.
16. Profile probe (persistent preference profiles): with a
    swapped plan on screen, click "Save this plan's swap
    learning to my profile" under "Your preference profile" →
    the line reports the derived kept count and the profile's
    tag weights. Generate a fresh plan, click "Suggest
    branches" → the learned line adds "stored profile applied"
    and the same tags adjust the new plan's candidates. "Show
    my profile" reports the same weights; with
    `TASTEPILOT_PROFILE_FILE` set the profile survives a
    restart; "Clear my profile" removes it and suggestions
    return to the per-request ranking. `GET /api/profile`
    without the browser's owner token → `401`.
17. Diff probe (plan comparison): after the branch probe, each
    successful branch line ends with a "vs base:" summary naming
    the swapped items (`+Kyoto National Museum, −Gion, Kyoto`).
    `POST /api/diff` with `{"base": <plan>, "other": <branch
    plan>}` → `200` with both sides `passed: true`; edit one
    affinity in the `other` plan first → that side reports
    `passed: false` with an `affinity_mismatch` violation (the
    comparison is recomputed, never read from a stored field).
18. Timeline probe (visual branch timeline): in the branch
    explorer, "View branch timeline" renders a table whose rows
    are grouped by leg and whose columns are the base plus each
    successful branch, headed by its recomputed verdict; the
    swapped slot reads `−` in the base column's successor and
    `+` under the branch. `POST /api/timeline` with
    `{"base": <plan>, "branches": [{"name": "B", "plan":
    <branch plan>}]}` → `200` with `summary.all_passed: true`;
    edit one affinity in the branch plan first → that column
    reports `passed: false` in the same `200` (a FAIL column,
    never a clean grid).
19. Timeline export probe (branch timelines in take-away
    exports): in the branch explorer's timeline view, "Download
    timeline (Markdown)" downloads `tastepilot-timeline.md`
    whose grid carries the same recomputed column verdicts.
    `POST /api/export-timeline?format=markdown` with the
    timeline body → `200` download with `Verification: PASS`;
    edit one affinity in a branch plan first → the download
    still returns `200` but its banner reads NOT VERIFIED and
    that column heads FAIL. Timeline ICS probe: with a dated
    base + branch, `POST /api/export-timeline?format=ics` →
    `200` download `tastepilot-timeline.ics` with one VEVENT
    series per branch (summaries name the branch; the tampered
    branch's events read NOT VERIFIED); with a dateless
    timeline, `?format=ics` → `400` (`no_dated_legs`).
20. Feed probe (live calendar subscriptions): in the timeline
    view, "Subscribe to timeline feed (iCalendar)" →
    `POST /api/feeds` returns `201` with a stable
    `/api/feeds/<id>.ics` URL; fetching it returns `200`
    `text/calendar` with the per-branch series and
    `Cache-Control: public, max-age=300` (subscribable, not
    no-store). `POST /api/feeds/<id>` with a re-sealed timeline
    → `200`, and the same URL then serves the new series;
    a dateless timeline create → `400` (`no_dated_legs`);
    `DELETE /api/feeds/<id>` → `200` and the URL then `404`s.
    Without `TASTEPILOT_FEED_FILE` feeds live in the server
    process — a restart clears them; with it set (on a
    persistent volume) they survive restarts. `GET /api/health`
    reports the feed store's mode, count, and TTL.
21. Multi-feed probe (multi-timeline subscriptions + change
    notification): add two dated timelines to the bundle in
    the timeline view and "Create multi-timeline
    subscription" → `POST /api/multi-feeds` returns `201`
    with version 1, an etag, and a stable
    `/api/multi-feeds/<id>.ics` URL; fetching it returns
    `200` `text/calendar` carrying every timeline's series
    (summaries name timeline and branch) with an `ETag`
    header and `Cache-Control: public, max-age=300`.
    Re-fetch with `If-None-Match: "<etag>"` → `304` with an
    empty body. `GET /api/multi-feeds/<id>/changes` shows
    the creation entry; `POST /api/multi-feeds/<id>` with a
    changed bundle → `200` version 2 with a new etag, the
    old etag no longer matches (the same URL serves the new
    series), and the change log gains an `updated` entry.
    A bundle with no dated leg anywhere → `400`
    (`no_dated_legs`); `DELETE /api/multi-feeds/<id>` →
    `200` and the URL then `404`s. Without
    `TASTEPILOT_MULTI_FEED_FILE` multi-feeds live in the
    server process; with it set they survive restarts.
    `GET /api/health` reports the multi-feed store.
22. Push-webhook probe (webhook notifications): with the
    bundle feed from probe 21 live, `POST
    /api/multi-feeds/<id>/webhooks` with `{"url":
    "https://example.org/hook"}` (a receiver you control)
    → `201` naming the webhook by id + host label only,
    with a `multi_feed.subscribed` test delivery recorded
    (delivered, or failed with its category if the receiver
    is unreachable — registration stands either way).
    `GET /api/multi-feeds/<id>/webhooks` lists it by host
    label; `POST /api/multi-feeds/<id>` (the probe-21
    update) → the response's `notifications` shows one
    `multi_feed.updated` delivery for the new version, and
    `GET /api/multi-feeds/<id>/notifications` shows both
    deliveries in order. A URL with credentials
    (`https://user:pass@…`) → `400` `bad_webhook_url`.
    `DELETE /api/multi-feeds/<id>/webhooks/<webhook_id>`
    → `200`, the list is empty again, and a further update
    pushes nothing. On an owned feed, registering or
    removing a webhook without the owner's token → `403`.
23. Redelivery probe (failed push, resent on demand): point
    the probe-22 webhook at a receiver that answers `500`
    and register it → the test delivery is recorded
    `failed` with a `delivery_id` and its payload kept.
    Make the receiver answer `200`, then `POST
    /api/multi-feeds/<id>/notifications/<delivery_id>/
    redeliver` → `200` with a new delivery `delivered`
    whose `redelivery_of` names the failed record; the
    receiver got a body byte-identical to the failed
    attempt's, and `GET .../notifications` shows the
    original still `failed`, first — the log is an audit
    trail, never edited. Redelivering the delivered record
    → `400` `already_delivered`; an unknown delivery id →
    `404`; after `DELETE .../webhooks/<webhook_id>`,
    redelivering its failed record → `400`
    `webhook_not_found`. On an owned feed, redelivery
    without the owner's token → `403`.
24. Signed-webhook probe (HMAC signature on pushes):
    register a webhook on the probe-21 bundle feed WITHOUT
    a `secret` → `201` returns a `signing_secret` (shown
    only in this response) and `signed: true`; the test
    delivery at the receiver carries
    `X-TastePilot-Signature: sha256=…` and
    `X-TastePilot-Event`, and the signature verifies
    (HMAC-SHA256 over the exact received body bytes) under
    the returned secret. `GET .../webhooks` and `GET
    .../notifications` contain the secret nowhere — only
    the `signed` flag. Register a second webhook WITH your
    own `secret` → `201` has no `signing_secret` field and
    its deliveries verify under yours; a bad secret
    (`"short"`, a number) → `400` `bad_signing_secret`
    with the value nowhere in the response; `"secret":
    null` → the webhook registers `signed: false` and its
    deliveries carry no signature header. A redelivery
    (probe 23) of a signed delivery is signed too.
25. Bulk redelivery probe (all failed pushes, one call):
    with the probe-23 receiver still answering `500`,
    register the webhook and update the feed once more, so
    two deliveries stand `failed`; make the receiver answer
    `200`, then `POST /api/multi-feeds/<id>/notifications/
    redeliver-failed` → `200` with `attempted: 2`,
    `delivered: 2`, and one result per chain, each naming
    its original record; `GET .../notifications` shows both
    resends recorded with `redelivery_of` set. A second
    bulk call → `attempted: 0` with both originals reported
    under `skipped` as `superseded` — nothing is resent.
    After `DELETE .../webhooks/<webhook_id>`, a bulk call
    on a feed with a failed record → that record under
    `skipped` as `webhook_not_found`. On an owned feed,
    the bulk call without the owner's token → `403`.
26. Webhook auto-pause probe (failing endpoints are paused,
    not hammered): with a receiver answering `500`, register
    a webhook (ping `failed`), then update the feed twice —
    after the third consecutive failed delivery,
    `GET .../webhooks` reports the webhook `paused: true`
    with `consecutive_failures: 3`. Update once more → the
    update's delivery record is `skipped` with error
    `webhook_paused` and the receiver is NOT hit again;
    a single redelivery of a failed record → `400`
    `webhook_paused`, and a bulk redelivery reports the
    records under `skipped` as `webhook_paused`. Make the
    receiver answer `200`, then
    `POST .../webhooks/<webhook_id>/resume` → `200` with
    `paused: false` and `consecutive_failures: 0`; the
    skipped delivery now redelivers `delivered`, and
    `.../pause` pauses by hand the same way. On an owned
    feed, pause/resume without the owner's token → `403`.
27. Webhook probe probe (auto-paused webhooks are probed
    after a cooldown): continuing probe 26, the webhook
    listing now reports `paused_reason: "auto"`, the
    `paused_at` moment, and a `probe_eligible_at` one hour
    later. Before that moment an update is still recorded
    `skipped` and the receiver is not hit. After it, the
    next update IS attempted as a probe (delivery record
    stamped `probe: true`): with the receiver still at
    `500` the webhook stays paused and
    `probe_eligible_at` moves one hour past the probe;
    with the receiver at `200` the probe delivery is
    `delivered` and the listing reports `paused: false`
    with `consecutive_failures: 0`. Pause a webhook by
    hand instead (`.../pause`, `paused_reason: "manual"`,
    `probe_eligible_at: null`) → updates stay `skipped`
    no matter how long has passed; only `.../resume`
    restarts it.

## Notes

* The evidence viewer only serves responses sealed into plans this
  server process produced, and the in-process cache is bounded
  (512 records, oldest evicted) — restarting the demo clears it.
* Demo data is fictional; public copy describes the build truthfully as
  Toni Adreal's entry, built with AI assistance.
