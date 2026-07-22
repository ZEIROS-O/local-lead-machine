# Pipeline dashboard

The dashboard is a read-only operational view of the canonical lead pipeline:

`Google Places discovery -> enrichment -> LLM scoring -> packet gate -> Airtable receipt`

It answers three questions without changing pipeline state:

1. Which configured batches are available, still unscored, or genuinely finished?
2. Where is each batch in the pipeline, and what artifact or gate needs attention?
3. Which leads in a batch are scored, sendable, money-signal positive, or blocked?

The inventory exposes every canonical batch through the **All**, **Active**,
and **Legacy** lifecycle views. Operational totals, throughput, and the
attention queue are active-only so preserved historical work cannot inflate
current pipeline capacity.

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
- Scored-artifact precedence is API, then manual, then the legacy unsuffixed
  CSV. A `_manual.csv` is counted only when its identities reconcile against
  enrichment; it is not relabeled as an API run.
- Discovery, enriched, scored, and packet stage counts are literal row counts
  from their selected artifacts. They are not inferred from status labels.
- `unscored` is the unmatched enriched-row count only after stable-identity
  reconciliation succeeds. It is `Unknown` when the artifacts cannot be
  reconciled safely; the dashboard never substitutes row-count subtraction.
- Current sendability is evaluated only for schema-v2 active scores and uses
  the objective four-field gate: `Packet Tier`, `Sendable`, `Packet Eligible`,
  and contact readiness. Legacy packet tiers are shown only as historical
  packet counts and are never labeled current-sendable.
- A batch is `finished` only when scoring is complete, tiers and packets are
  current, and the saved live Airtable sync hash still matches those current
  inputs with zero skipped live rows.
- A matching clean dry-run receipt without a matching live receipt is
  `airtable_ready`, not finished.
- Identity-verified custom Airtable receipts under
  `run_state/airtable_receipts/<batch>/` are displayed as `partial`. They show
  the actual synced cohort but never make the full batch appear finished.
- Airtable evidence is explicitly `Verified`, `Partial`, or `Unknown`. A local
  dry-run status by itself is not live Airtable proof.
- Reads are stable-stat checked so an artifact being rewritten is not presented
  as a complete snapshot.

No dashboard endpoint starts discovery, enrichment, scoring, packet generation,
or Airtable sync, and the dashboard never mutates Airtable. The displayed next
action is an operator command to run in the existing Hermes workflow.

## Verification

```bash
make dashboard-test
make dashboard-build
```

The backend tests cover stable-identity reconciliation, lifecycle totals, stage
inference, stale receipts, malformed artifacts, expired approvals, privacy
exclusions, and the read-only HTTP contract. The frontend tests cover lifecycle
filtering and explicit current, historical, and unknown states.
