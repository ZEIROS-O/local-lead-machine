# Subscription scoring

`score_batch_subscription.py` runs the existing five-lead manual score bundles through a fresh
Codex or Claude subscription session. It does not call `lead_machine/llm_client.py`, fall back to an
API key, write to Airtable, or join the morning/Hermes loop.

## Before the first run

Disable automatic credit purchases or top-ups in the relevant account. The adapter verifies that
the CLI is using first-party subscription authentication and removes provider API-key overrides
from child processes, but it cannot inspect or change account-side billing settings. A ledger entry
with `cost_basis: subscription_auth` proves the authentication route, not a guaranteed $0 charge.

Verify authentication without invoking a model:

```bash
.venv/bin/python score_batch_subscription.py \
  --queue-dir review_queue_detailing_columbus_medium_001 \
  --runner codex \
  --auth-check-only
```

Use `--runner claude` for Claude. Codex is the default runner. Optional model overrides use
`--model`, `CODEX_SUBSCRIPTION_SCORING_MODEL`, or `CLAUDE_SUBSCRIPTION_SCORING_MODEL`, in that order.

## Workflow

Generate current batch prompts, then score them:

```bash
make batch BATCH=detailing_columbus_medium_001
make score-batch-subscription BATCH=detailing_columbus_medium_001 RUNNER=codex
```

The adapter runs each batch in a fresh temporary directory with the prompt on stdin. Codex runs
ephemerally with read-only/no-tool/no-web settings and ignored user configuration. Claude runs in
safe mode with no tools, persistence, or MCP servers. Each returned bundle must have the requested
batch ID and exactly the filenames in `batch_NNN_expected_files.txt` before the existing v2 importer
can stage and promote it.

Run the existing downstream tail only after every selected batch succeeds or receipt-validates:

```bash
make full-subscription BATCH=detailing_columbus_medium_001 RUNNER=codex
```

`full-subscription` ends at `airtable-dry`. It never invokes `airtable-live`.

## Resume, receipts, and recovery

Successful imports write receipts under
`review_queue_<batch>/scores/.subscription_receipts/`. A batch is skipped only when its prompt,
expected-file manifest, and every score file match the recorded SHA-256 hashes.

If files exist without a matching receipt, the adapter returns `stale_existing` before spending
quota. Inspect the changed prompt/evidence first, then explicitly replace the batch when appropriate:

```bash
.venv/bin/python score_batch_subscription.py \
  --queue-dir review_queue_detailing_columbus_medium_001 \
  --batch-id batch_001 \
  --force
```

Invalid JSON and validation failures receive one fresh-session retry. Timeouts and runner errors do
not retry. Authentication, quota, rate-limit, and plan-limit failures stop all remaining batches.

Exit codes are `0` for imported/receipt-matched batches, `1` for ordinary batch failures, and `2`
for authentication/billing-guard or quota hard stops. The append-only audit log defaults to
`output/subscription_scoring_ledger.jsonl`.

## Pipeline distinction

This adapter consumes `make_llm_batch.py` bundle prompts. `run_pipeline.py --mode manual-llm`
generates per-lead prompts through `make_llm_queue.py`; it is a separate resume interface. Both
paths ultimately use the same score directory format and `score_leads.py --mode manual-llm` merge.

The paid `api-llm` mode remains available only when the operator selects it directly.
