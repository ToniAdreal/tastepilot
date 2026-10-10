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
   Without `TASTEPILOT_SHARE_FILE`
   links live in the server process — a restart clears them; with it
   set (on a persistent volume) they survive restarts.

## Notes

* The evidence viewer only serves responses sealed into plans this
  server process produced, and the in-process cache is bounded
  (512 records, oldest evicted) — restarting the demo clears it.
* Demo data is fictional; public copy describes the build truthfully as
  Toni Adreal's entry, built with AI assistance.
