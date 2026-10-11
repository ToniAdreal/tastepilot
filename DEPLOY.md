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

## Notes

* The evidence viewer only serves responses sealed into plans this
  server process produced, and the in-process cache is bounded
  (512 records, oldest evicted) — restarting the demo clears it.
* Demo data is fictional; public copy describes the build truthfully as
  Toni Adreal's entry, built with AI assistance.
