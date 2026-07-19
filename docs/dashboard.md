# Pipeline dashboard

The dashboard is a read-only operational view of the canonical lead pipeline:

`Google Places discovery -> enrichment -> LLM scoring -> packet gate -> Airtable receipt`

It answers three questions without changing pipeline state:

1. Which configured batches are available, still unscored, or genuinely finished?
2. Where is each batch in the pipeline, and what artifact or gate needs attention?
3. Which leads in a batch are scored, sendable, money-signal positive, or blocked?

## Install and run

Install the dashboard dependencies once:

```bash
.venv/bin/pip install -r requirements-dashboard.txt
npm --prefix dashboard-ui install
```

Build and serve the production UI from one localhost process:

```bash
make dashboard
```

Open <http://127.0.0.1:8787>. The server reads the repository given by
`--repo-root` (the current repository by default) and refreshes the browser view
every 30 seconds. A manual refresh forces a new artifact scan.

For frontend development, run these in separate terminals:

```bash
make dashboard-api
make dashboard-ui
```

The Vite development server is at <http://127.0.0.1:5173> and proxies `/api`
to the FastAPI server.

## Remote access through a tunnel

Build the UI and leave the dashboard bound to localhost, then point an
authenticated tunnel at `http://127.0.0.1:8787`. For example, with an existing
Cloudflare Tunnel setup:

```bash
make dashboard-build
.venv/bin/python -m lead_machine.dashboard
cloudflared tunnel --url http://127.0.0.1:8787
```

The dashboard does not implement user accounts. Do not expose it through a
public unauthenticated tunnel; put tunnel-provider access control in front of
it. The API intentionally omits phone numbers, email addresses, openers, and raw
evidence, but it still contains internal batch and lead qualification data.

## Source-of-truth rules

- Canonical batch names are resolved with the existing `BatchPaths` contract.
  Debug, ad-hoc, and noncanonical artifacts are ignored and counted in source
  health.
- Physical CSV/JSONL artifacts determine row counts. `run_state/*.json` supplies
  receipts, approvals, cap stops, and errors; count differences are shown as
  drift instead of silently trusted.
- `unscored` is `max(enriched rows - scored rows, 0)`.
- A batch is `finished` only when scoring is complete, tiers and packets are
  current, and the saved live Airtable sync hash still matches those current
  inputs with zero skipped live rows.
- A matching clean dry-run receipt without a matching live receipt is
  `airtable_ready`, not finished.
- Reads are stable-stat checked so an artifact being rewritten is not presented
  as a complete snapshot.

No dashboard endpoint starts discovery, enrichment, scoring, packet generation,
or Airtable sync. The displayed next action is an operator command to run in the
existing Hermes workflow.

## Verification

```bash
make dashboard-test
make dashboard-build
```

The backend tests cover stage inference, stale receipts, malformed artifacts,
expired approvals, privacy exclusions, and the read-only HTTP contract. The
frontend tests cover the overview and batch inventory rendering.
