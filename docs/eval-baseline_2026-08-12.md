# Eval baseline — 2026-08-12

The first measurement taken after the corpus manifest, so this is the number the
next run is compared against. Every figure below is from a local run with
`SUPPRESSION_SOURCE` and `LPM_DIAL_DECISIONS_JSONL` supplied through the
environment.

**Coverage: 14 corpora, `source: manifest`.** All three corpus evals previously
globbed `scored_*.csv` over `output/` — 49 files back to 2026-05-31, including
`scored_debug_1.csv` and every superseded recovery run. They now default to
[`evals/active_corpora.json`](../evals/active_corpora.json) and report the block
they replayed, so a green run states its own coverage instead of leaving the
reader to assume it.

| Eval | ok | Findings |
|---|---|---|
| `reproducibility` | ✗ (exit 2) | `verdict_drift` **0**, `stale_evidence` 1, `snapshot_drift` 0, `unverifiable` 23 (reported) |
| `completeness` | ✗ (exit 2) | `scored_orphans` **111**, `corpus_conflicts` 80, `base_orphans` 17, `blank_base_keys` 6, `blank_base_verdicts` 3, `pruned_rejects` 351 (reported) |
| `gate_liveness` | ✗ (exit 2) | `never_fires` 2, `unreferenced` 0, `outside_replay` 3 (reported) |
| `pre_dial_approval` (KPI) | NO VERDICT | Ledger is empty — no dial has been logged yet |
| `accept_rate` (operational) | FAIL | 1 lead holds a Status outside the accept/reject/undecided partition |

Corpus: 14 files, 1,315 scored rows, 938 distinct leads, against 499 base rows.

## What moved, and why

**`verdict_drift` 71 → 0.** Seventy were corrected by the
[2026-08-12 re-verdict upload](re-verdict-upload-receipt_2026-08-12.md). The
seventy-first — `Ricks Garage Door Service`, `review → send` — was **not a real
disagreement and was not fixed**: it was the eval comparing the base against a
superseded row. The manifest now selects `scored_recovery_consolidated.csv:420`
for that lead, whose current evidence recomputes to `review`, matching the base.
`scored_recovery_garage_doors_001_api_tiered.csv` held an older scoring of the
same lead. That is the exact class the manifest exists to remove.

**`scored_orphans` 462 → 111, with 351 reclassified as `pruned_rejects`.**
Reject-verdict leads are pruned from the CRM deliberately, so every one was a
permanent orphan under `scored_keys - base_keys`. The check now asserts the
invariant that matters — every lead the current gate calls actionable must exist
in the base — and reports pruned rejects separately.

**`corpus_conflicts` 84 → 80.** The plan expected most of these to resolve once
the replay was scoped. They did not. Only four were duplicate-corpus artifacts;
the remaining 80 are a lead's fields genuinely disagreeing across active corpora
and need to be looked at on their own. **Still open.**

## Still red, and what each means

- **`never_fires` 2** — `EVIDENCE_COUNT_BLOCKER` and `INVALID_REF_BLOCKER` are
  reachable from `evaluate_sendability` and no row in the corpus trips either.
  Per the eval's own remedy: either an earlier gate shadows them or the corpus
  cannot exercise them. Confirm which, then delete the gate or widen the corpus.
- **`stale_evidence` 1** — `JB Restoration And More`, schema v0, recomputes to
  `send` against a base `review`. It predates the current score schema and
  cannot be re-derived; it needs a re-run, not an investigation.
- **`scored_orphans` 111** — of these, the ones carrying a `send` verdict are the
  real finding (e.g. `Jeffries Overhead Door`). Many others have a blank
  `Verdict` column because they predate it; blank is not provably `reject`, so
  they stay reported rather than being silently pruned.
- **`base_orphans` 17 / `blank_base_keys` 6 / `blank_base_verdicts` 3** — base
  rows with no scored evidence behind them. Unchanged by this work.
- **`accept_rate` FAIL** — one lead sits outside the partition. Operational only;
  AGENTS.md is explicit that this cannot judge lead quality, which is why
  `pre_dial_approval` is now the KPI step in CI.

## CI

`gate_liveness --references-only` and the manifest-parse check run on
`ubuntu-latest` on every push — the first gate steps in this repo to execute in
CI since ~2026-08-01. The full corpus replay and every Airtable check stay on the
`lpm` self-hosted job, which is now **workflow_dispatch-only**: no runner has been
registered, and a scheduled job against a label nothing answers to queues for 24h
and cancels, which reads as coverage that does not exist. Re-add its schedule in
the same change that registers the runner.
