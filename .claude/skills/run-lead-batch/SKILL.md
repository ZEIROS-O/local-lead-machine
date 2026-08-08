---
name: run-lead-batch
description: >-
  Run the local-lead-machine lead pipeline end-to-end for a search config —
  search → enrich → score → packets → Airtable — with budget caps enforced and a
  per-run summary. Use this whenever the user wants to run a batch, "run the
  pipeline", find/score leads for a metro or vertical, regenerate packets, or
  operate local-lead-machine end-to-end — even if they only name a config like
  `detailing_cleveland_medium_001` or say "score these leads". This is the
  operating procedure for Loop A; prefer it over ad-hoc python invocations so
  caps, verification, and the summary are never skipped.
---

# Run a lead batch (Loop A)

This skill encodes the one reliable way to run the pipeline so an operator never
forgets the caps, the verification, or the summary. The method is the product —
this is the procedure a Phase C scheduler will eventually call unattended.

## Pick the entrypoint first

There are two, and they are not interchangeable.

| | `run_agent_pipeline.py` | `run_pipeline.py` |
|---|---|---|
| Use for | **default — any real batch** | legacy / partial re-runs |
| Batch naming | resolved from `--vertical --market --size` | you pass `--batch` |
| Mutual exclusion | `--lock-file`, on by default | none |
| Airtable | `--airtable {skip,dry-run,live}`, default `skip` | `--airtable-live` flag |
| Spend confirmation | `--max-llm-usd`, `--yes` above threshold | none |

Prefer `run_agent_pipeline.py`. It is the Hermes-safe wrapper: it locks, it
refuses to spend above a threshold without `--yes`, and it defaults Airtable to
`skip`.

## Pre-flight (every run, no exceptions)

1. **`SUPPRESSION_SOURCE` must be exported.** It is *not* in `.env`. Without it
   (or `LPM_OUTCOMES_JSONL`), search, enrichment, scoring **and**
   `query_ready_to_send.py` all fail closed with `[ERROR]` and exit 1. That is
   deliberate — an empty index would disable every opt-out. There is no silent
   fallback; `ALLOW_MISSING_SUPPRESSION_INDEX=1` bypasses it loudly and should
   not be used for a real run.
2. **Keys for the stages you're running:** `GOOGLE_PLACES_API_KEY` (search),
   `AIRTABLE_*` (upload). For API scoring, whichever provider the chain resolves
   to — see below.
3. **Decide the provider chain.** `LLM_SCORING_PROVIDER_CHAIN` orders
   `lead_machine/llm_client.py`'s providers; the default is
   `kimi` (Moonshot API, `KIMI_API_KEY`) → `codex` CLI (ChatGPT subscription) →
   `claude` CLI (subscription). `anthropic` is the Messages API
   (`ANTHROPIC_API_KEY`); `claude` is the CLI subscription — they are different
   providers. Fallback fires only on provider *unavailability*; invalid output
   stays with the repair flow in `score_leads.py`. `[LLM PROVIDER]` in the run
   log says who actually served — read it, don't assume.
   - **A `$0`-priced subscription provider first zeroes the cost projection**,
     so `MAX_LLM_USD_PER_RUN` stops binding. If you want a free run, make the
     chain a *single* entry (`LLM_SCORING_PROVIDER_CHAIN=codex`) so there is no
     paid provider to fall through to. A chain like `codex,kimi` bills Moonshot
     per-batch, with no ceiling, the moment the subscription quota runs out.
4. **Confirm the caps are intentional.** They default sanely but a run should
   choose them: `MAX_LLM_CALLS_PER_RUN` (60), `MAX_LLM_TOKENS_PER_RUN` (800k),
   `MAX_LLM_USD_PER_RUN` ($10 — **the real backstop**; calls/tokens are blunt
   proxies), `LLM_SPEND_CONFIRM_USD` ($3).
   - At ~2,790 tokens/lead the **token cap binds first, at ~285 leads per
     invocation.** For anything larger, raise it deliberately or loop one config
     at a time (cleaner: you get a receipt between runs).
   - `MAX_WEBSITES_PER_RUN` (200) **discards** leads past the ceiling rather
     than stopping short — undersizing it is a silent yield loss, not a halt.
5. **Concurrency knobs** (all env, all have sane defaults — raise deliberately):
   `LLM_SCORING_CONCURRENCY` (4; the safe value is provider-specific — CLI
   runners are bounded by local CPU/memory, `kimi`/`anthropic` by per-org
   tokens-per-minute), `ENRICH_WORKERS` (6, or 5 with screenshots/rendered
   checks), `PAGE_FETCH_WORKERS` (1 = sequential crawl within a lead),
   `PLACES_DETAILS_WORKERS` (6, clamped to 8).
6. **Pick the config** in `search_configs/` and confirm its `vertical` matches
   intent. If the vertical or metro doesn't exist yet, stop and use the
   `add-vertical` / `add-metro` skill first.

## Run

```
SUPPRESSION_SOURCE=<path> .venv/bin/python run_agent_pipeline.py \
  --vertical <vertical> --market <market> --size <small|medium|full> \
  --mode <search-only|score-dry-run|score-confirmed|packets-only|full> \
  --airtable skip
```

- **Stages:** search (`make_search_batch.py`) → `enrich_leads.py` →
  `score_leads.py` → `make_packets.py` → `upload_airtable.py`. Outputs land in
  `output/`, `packets_<batch>/`, and the per-run JSON summary in `output/`.
- **Honor the caps.** `[STOP] Hit MAX_*` is the halt contract working. Do not
  raise a cap to push past it without a reason you can state.
- **`--lock-file` and Airtable do not mix.** Every output is keyed by batch
  name, so giving each batch its own lock file lets distinct batches run in
  parallel through search/enrich/score. The Airtable base is **not** batch-keyed
  shared state: two concurrent runs can create two records for one business,
  both `Status: New`, both matching the queue filter — a rep dials the twin
  after `mark_sent` flips the first. If you use a custom `--lock-file`, use
  `--airtable skip`.

### Subscription (zero-cost) scoring path

Serial, no chain, cannot spend money by construction. Slower but safe:

```
make score-batch-subscription QUEUE_DIR=review_queue_<batch> RUNNER=codex
```

Quota exhaustion exits cleanly with `[HARD STOP]` and prior batches stay
committed; resume matches receipts in `.subscription_receipts/` so nothing is
re-spent.

**Trap:** `make batch` truncates to 12 leads (`Makefile:6`, `LIMIT ?= 12`). Pass
`LIMIT=0` for a full batch.

## Verify before trusting the output

- Full suite after any code change to scoring/enrichment/ICP:
  ```
  .venv/bin/python -m unittest discover -s tests 2>&1 1>/dev/null | tail -5
  ```
  **Never** run bare `python -m unittest` — it discovers **zero** tests and
  reports OK. That is a false green, and it has fooled a review before. The
  `.claude/hooks/block-bare-unittest.sh` hook exists to stop it. The suite
  prints heavy stdout, which is why the summary is filtered off stderr.
- `make compile` for a fast syntax check across the pipeline modules.
- Sanity-check the batch with the **`verify-batch`** skill — never trust grades
  blind. That skill is the *checker*; this one is the *maker*. Keep them
  separate; do not have the same pass both score and bless a batch.
- Dry-run the upload before ever going live, and confirm the diff does not
  touch `Status` on human-decided rows:
  ```
  .venv/bin/python upload_airtable.py --dry-run --input <scored.csv> --packet-dir <packets_dir>
  ```
  Upload the **total** scope — pass no row-filtering flags. The dry-run's job
  is to show every row the live run would touch, so a narrowed preview blesses
  a plan that is not the one that executes. `make airtable-dry` /
  `make airtable-live` wrap the same command with the Makefile's batch
  variables.

## Summarize (the run's telemetry)

Report, in one block: leads in · A/B count · sendable count · packets ·
`$` spent / tokens / LLM calls vs caps · **which provider actually served**
(`[LLM PROVIDER]`) · Airtable rows. This is the state surface the operator (and
later the scheduler) reads to decide whether the run was healthy.

## Guardrails

- One config per run; keep batches small enough to actually review the output.
- Never auto-send outreach from this skill — it stops at packets/Airtable.
  Sending is Loop C and is gated by Gate 0 (see
  `docs/outreach-loop-strategy.md`).
- An opt-out is recorded with `mark_outcome.py --event opt_out`, which appends
  to the suppression ledger and sets `Status = Not Fit`. The ledger is the only
  record of it; `Do Not Contact` is deliberately not a Status.
