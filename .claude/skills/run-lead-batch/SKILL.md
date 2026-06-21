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

## Status / prerequisites
- **Active now:** `run_pipeline.py` defaults to `--mode api-llm` and runs
  search → enrichment → scoring → packets → Airtable dry-run with a per-run JSON
  summary. Use `--mode manual-llm` only as the fallback path when API scoring is
  unavailable, and use `--mode deterministic` for rule-only checks.

## Pre-flight (do this every run)
1. Confirm `.env` has the keys the stages need: `GOOGLE_PLACES_API_KEY` (search),
   `AIRTABLE_*` (upload). For `api-llm`: `ANTHROPIC_API_KEY`, `LLM_SCORING_MODEL`.
2. Confirm the per-run caps are set (they default sanely, but a run should be
   intentional about its budget): `MAX_SEARCH_CALLS_PER_RUN`,
   `MAX_DETAILS_CALLS_PER_RUN`, `MAX_TOTAL_PLACES_PER_RUN`, and (api-llm)
   `MAX_LLM_CALLS_PER_RUN`, `MAX_LLM_TOKENS_PER_RUN`, `MAX_LLM_USD_PER_RUN`.
3. Pick the config in `search_configs/` (e.g. `detailing_cleveland_medium_001.json`)
   and confirm its `vertical` matches the intent. If the vertical or metro doesn't
   exist yet, stop and use the `add-vertical` / `add-metro` skill first.

## Run
Use `run_pipeline.py` so the stages chain with the right inputs/outputs:
```
python run_pipeline.py --batch <config_name> --vertical <vertical>
```
- The stages are: `places_search.py` → `enrich_leads.py` → `score_leads.py` →
  `make_packets.py` → `upload_airtable.py --only-sendable --dry-run`. Outputs
  land in `output/`, `packets_*/`, and `output/run_summary_<batch>.json`.
- **Respect the stop point.** In `manual-llm` mode the run halts and asks for
  scores in the scores dir. Generate them and re-run the same command to resume.
  In `api-llm` mode there is no manual clipboard stop.
- **Honor the caps.** If a stage prints `[STOP] Hit MAX_*`, that is the halt
  contract doing its job — do not raise caps to push past it without a reason.
- Add `--skip-airtable` for local/test runs that should not even dry-run the
  Airtable tail. Add `--airtable-live` only when credentials are configured and
  the sendable rows have been reviewed.

## Verify before trusting the output
- Run the contract tests after any code change touched scoring/enrichment/ICP:
  `python -m unittest tests.test_lead_machine_contracts tests.test_icp_lanes`.
- Sanity-check the batch with the `verify-batch` skill — never trust grades blind.

## Summarize (the run's telemetry)
Report, in one block: leads in · A/B count · sendable count · packets · (api-llm)
`$` spent / tokens / LLM calls vs caps · Airtable rows. This is the state surface
the operator (and later the scheduler) reads to decide whether the run was healthy.

## Guardrails
- One config per run; keep batches small enough to actually review the output.
- Never auto-send outreach from this skill — it stops at packets/Airtable. Sending
  is Loop C and is gated by Gate 0 (see `docs/outreach-loop-strategy.md`).
