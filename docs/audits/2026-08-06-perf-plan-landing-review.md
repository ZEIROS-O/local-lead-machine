# Operational Review — landing of the "Speed up search → enrich → score" plan

**Date:** 2026-08-06
**Branch audited:** `main` @ `485e45a`
**Range reviewed:** `b644bcd..HEAD` (21 commits — the 7-phase performance plan plus the
`feat/multi-provider-llm-scoring` merge)
**Plan:** `~/.claude/plans/Speed up lead-machine.md`
**Scope:** read-only review of the plan's landing and the resulting state of the repo —
performance, correctness, spend safety, compliance, observability, operability, infrastructure.
No files modified, no pipeline run, no paid call, no Airtable write.

**Verification basis:**
- `.venv/bin/python -m unittest discover -s tests` → **1144 tests, OK** (57s), dirty tree.
- Same command against committed `HEAD` in a clean detached worktree → **1143 tests, OK**. The
  uncommitted working tree is not propping anything up.
- `make compile` → OK.
- Live repro of the claims round-trip bug against the real `lead_machine.sendability` functions.
- Independent re-derivation of the sendability gate over all 133 rows of
  `output/scored_garage_doors_hamilton_medium_001_api.csv`, and a corruption sweep over all 24
  `output/scored_*.csv` (541 rows carrying claims).
- Two read-only project reviewers over the same range: `invariant-guard` (AGENTS.md correctness
  invariants) and `compliance-reviewer` (outreach-compliance risk). Every finding relayed below
  was re-verified by hand against the named file:line before being recorded here.

---

## 0. Verdict

The plan landed and the engineering quality is high. Three things are not true of it:

1. **Phase 6 is one third done** — `reasoning_effort` landed; `thinking: disabled` and prompt
   caching did not.
2. **The concurrent scoring path enforces spend caps differently than the serial path it
   replaced** — three separate holes, one of which can disable a USD cap entirely.
3. **A pre-existing claims-serialization bug is now materially more reachable**, because this
   range changed who authors the claims.

Separately, and outranking every code finding: **22 commits are unpushed since 2026-08-02**, and
the project's own review tooling is untracked. The entire plan, and the agents that just reviewed
it, exist on one WSL box.

`compliance-reviewer`'s verdict on the range is **not safe to send behind**, driven by §4.1.

---

## 1. Did the plan land?

| Phase | Status | Evidence |
|---|---|---|
| 1 — enrichment fetch waste | Landed | `enrich_leads.py:936` (5xx-only retry), `:1027-1039` (gated fallbacks), `:179` per-lead cap, `:870-884` pooled thread-local session |
| 2 — browser mechanics | Landed | `:1572-1620` thread-local Chromium + per-lead context, `:1638` bounded `networkidle` |
| 3 — worker ceiling | Partial | browser 3→5 (`:277`). `DEFAULT_ENRICH_WORKERS` still **6** (`:276`); plan called for ~10 |
| 4 — concurrent page fetch | Built, shipped **off** | `DEFAULT_PAGE_FETCH_WORKERS = 1` (`:282`) — deliberate, see §2 |
| 5 — concurrent scoring | Mostly | pool + plan-time batching landed; `load_dotenv` hoisted (`llm_client.py:139-153`). **Missing:** thread-local `Session` for kimi/anthropic, batch-id prefix on `[LLM REQUEST]` |
| 6 — per-request cost | **1 of 3** | `reasoning_effort` landed (`llm_client.py:334-342`). **Missing:** `thinking: {"type":"disabled"}`, `cache_control` prompt caching |
| 7 — parallel batches + search | Landed | `make_search_batch.py:634` (lock), `:686-696` (atomic budget), `:737` (pool); `run_agent_pipeline.py:635` (`--lock-file` help) |

Phase 4 shipping off is a **correct** call, not a miss. `resolve_page_fetch_workers`'s docstring
(`enrich_leads.py:330-358`) carries an actual measurement table — 1.42× on an idle box, flat at
shipped concurrency, +320 MB PSS on a box where Phase 3 already measured 3.9 GB — and concludes
it is a knob rather than a default. Fidelity was verified independent of width: every width from
1 to 8 produced the same pages, the same 7.37 fetches per lead, and the same graded fields.

---

## 2. Performance — what was actually delivered

| Stage | Result | Basis |
|---|---|---|
| Enrichment fetches | 2.35× (14.24 → 5.52 fetches/lead) | measured, prior session |
| Browser pooling | 1.17× | measured at the 4–5 worker knee |
| Page-fetch concurrency | 1.42× on an idle box, flat at shipped defaults | measured; shipped off for this reason |
| Scoring | 774s for 133 leads = **5.8s/lead** at concurrency 4 | `output/llm_usage_garage_doors_hamilton_medium_001.json` |
| Search | `place_details` concurrent, 0.15s pacing sleep removed | `make_search_batch.py:737` |

**No speedup ratio is quoted for scoring.** The plan's serial baseline (38.9s/lead) was the codex
CLI runner; the measured run was kimi API. Different provider — the comparison would be
apples-to-oranges.

**Phase 5's byte-identical check could not be run live.** Grades are not reproducible run-to-run
(§4.3), so sampling variance swamps any serial-vs-concurrent difference. It holds only against
mocked calls, which `SerialConcurrentEquivalenceTest` already pins — with the caveat in §4.2.

---

## 3. The measured run

`output/llm_usage_garage_doors_hamilton_medium_001.json`, 2026-08-06:

| | |
|---|---|
| Leads scored | 133 / 133 (`partial: false`) |
| Wall clock | 774s at concurrency 4 |
| Grades | A 9 · B 27 · C 47 · Skip 50 |
| Sendable / packet-eligible | 30 / 31 |
| Calls · tokens · cost | 36 · 417,913 · $2.64 |
| vs caps | 36/60 calls · 418k/800k tokens · $2.64/$10 — no stops |

36 calls from 27 batches. 9 failure artifacts in `_api_bundles/` — **33% of batches needed a
second call.**

Independent re-derivation of the gate over all 133 rows: **0 sendable-but-weak leads**; every
admitted lead has a named send trigger, an opener, and safe claims carrying valid evidence refs.
28 of 30 sendable verdicts reproduce exactly; the 2 that do not are §4.1. No false skips —
AmeriDoor (A, 5.0/225) is correctly held as contact-after-asset with `asset_state=planned`
(deliver-first routing to Loop B, not a gate error); Schussler is minor-fix/nurture.

---

## 4. Findings

### 4.1 — BLOCKING: an unsupported claim can reach a business owner

`lead_machine/sendability.py:377-382` (write) and `:1372-1388` (read). Pre-existing; this range
changed its reachability.

`flatten_safe_outreach_claims` joins claims with `"; "`. `_parse_flattened_safe_claims` splits on
`";"` with no escaping. A claim containing a semicolon splits in two. Live repro against the real
functions:

```
flattened:  'Your site loads in 8s; competitors load in 2s [ev_1]'
round-trip: {'claim': 'Your site loads in 8s',   'evidence_refs': []}
            {'claim': 'competitors load in 2s',  'evidence_refs': ['ev_1']}
```

**The ordering is what makes this a compliance problem rather than a parsing nit.** In
`apply_sendability`:

```
832:  result = evaluate_sendability(updated)                              # gate runs on INTACT objects
842:  updated["Safe Outreach Claims"] = flatten_safe_outreach_claims(...) # then corrupts them
```

The gate passes on good data; the artifact a human writes copy from carries bad data.

**Two distinct harms, and the second is worse:**

1. The leading fragment loses its `[ref]` entirely — an operator copies an assertion with no
   verifiable backing.
2. The trailing fragment **inherits a ref that was evidence for a different assertion**. That is
   affirmative misattribution, not a missing citation — and it is the one an operator is least
   likely to catch, because the claim *looks* backed.

**Shortest path to a prospect is in-process, no CSV round-trip:** `score_leads.py:3929` builds
lead cards from the post-flatten `scored` rows → `lead_machine/lead_card.py:86` calls
`normalize_safe_outreach_claims(row)` → fragments. The same corrupted string also goes to Airtable
(`upload_airtable.py:145, 270`) and out through `query_ready_to_send.py:58`.

**Second consequence — a red CI gate.** `evals/reproducibility.py` recomputes every verdict from
the stored row through the same `evaluate_sendability`. Recomputing the two affected rows:

| Lead | Stored | Recomputed |
|---|---|---|
| A1 Garage Doors and Openers | `Sendable: Yes`, no blockers | `sendable: False` — *Safe outreach claim missing evidence reference* |
| Garage Door Openers and Doors, Inc. | `Sendable: Yes`, no blockers | `sendable: False` — same |

The daily `gates.yml` verdict-reproducibility check will go red once this batch is in the corpus.

**Blast radius, measured across all 24 scored CSVs:** 3 of 541 rows carrying claims (0.6%), of
which **2 sendable — both in today's batch** (2 of 30 = 6.7% of the sendable set). Rare
historically, present now.

**Why this range makes it more reachable:** `kimi-k3` is now the default claim author
(`llm_client.py:32`), replacing `claude-sonnet-5`. The prompt (`score_leads.py:2630-2637`) places
no constraint on punctuation inside claim text, and commit `485e45a`'s own message documents
kimi-k3 emitting a malformed `safe_outreach_claims` entry on 2026-08-06. Changing the author of a
field whose serialization is known-lossy is the reachability change.

**Currently contained:** no packets were generated for this batch and `airtable_synced: 0`.
Nothing corrupted has left the machine.

**Fix:** escape on write, or store JSON. Re-evaluation already fails *closed*
(`refresh_packet_tiers.py:184` → `SAFE_CLAIM_REF_BLOCKER`), so this admits no extra leads — the
entire risk is in the artifact.

### 4.2 — The concurrent scoring path does not enforce the caps the serial path did

Three holes in one path. The serial path checks `_hit_llm_cap(calls=…, tokens=…, usd=…)` against
**actual banked usage** before every batch (`score_leads.py:3430`). The concurrent path replaces
that with an up-front reservation and then never re-checks anything — all batches are submitted
(`:3386-3413`) before any is banked (`:3414-3423`), so there is no point at which a cap can still
stop the run.

**(a) `MAX_LLM_CALLS_PER_RUN` counts planned batches, not calls.** `:3395` caps submissions by
planned-batch index; `:3338` then increments `calls` by `result["call_count"]`.
`_score_batch_with_debug` (`:2940-3040`) can spend up to **three** calls per batch — the initial
call, the truncation retry at `max(8192, max_tokens * 2)` (`:3031-3035`), and the validation-repair
call (`:2989`) whose prompt embeds the invalid bundle *plus* the compact records. **Cap 60 admits
up to ~180 billed calls.** Today's run was mild only because most batches succeeded first try
(36 calls from 27 batches, 1.33×).

**(b) Token and USD reservations do not cover retries.** Reserved is
`estimated_prompt_tokens + max_tokens` (`:3184`) — exactly one call's worth. The truncation retry
*doubles* output tokens above that; the repair call's input is larger than the reserved prompt.
Actual can land at 2–3× the reservation with no `[STOP]` printed. The plan promised "conservative
— can stop early, never overshoot"; that holds only while slack absorbs retries.

**(c) A `$0`-first provider chain zeroes every reservation — and the repo already contains the
fix, wired to the wrong consumer.** `resolve_scoring_model()` is purely positional; it returns the
first chain entry's model without checking availability. Its own docstring
(`llm_client.py:563-570`) names the hazard exactly:

> NOTE: this is the *expected* model, not a spend guard. … a $0 subscription runner first in the
> chain would otherwise project $0 for a run that falls through to a paid API and spends real money.

`score_leads.resolve_projection_model()` (`:2070`) exists to solve precisely that — "the priciest
model the chain could reach." **It is used only for the pre-run confirmation print** (`:3838`,
`:3846`). The run itself gets `model=llm_model` (`:3896`), and `_plan_api_llm_batches` prices every
reservation with it at `:3185`.

With `LLM_SCORING_PROVIDER_CHAIN=codex,claude,kimi` — a reorder `.env.example:7` documents as
supported, and an obvious one to try since the codex subscription is free — `llm_model` is
`subscription:codex`, priced `(0.0, 0.0)` at `score_leads.py:314`. Every `projected_usd` is `0.0`,
`remaining_exceeds_usd_cap` (`:3190`) is never true, **and `MAX_LLM_USD_PER_RUN` does not bind at
all.** Codex hits quota → chain falls to kimi → every batch billed at `(3.0, 15.0)` with no
ceiling. `resolve_served_model` keeps the *accounting* correct; nothing ever compares it to the cap.

The shipped default is kimi-first and kimi-k3 is priced, so today's runs are not exposed. The
guard is one config change from off.

**(d) No test could catch any of this.** All four `score_records_api_run` calls in
`tests/test_llm_scoring_concurrency.py` (`:146`, `:192`, `:304`, `:386`) pass
`max_calls=0, max_tokens_per_run=0, max_usd_per_run=0.0` — caps disabled.
`SerialConcurrentEquivalenceTest` (`:267`) proves row-for-row parity **only with caps off**, so
the equivalence guarantee the plan leans on says nothing about cap behaviour.

**Fix, as one change:** count actual calls; reconcile tokens/USD against banked totals in the
collection loop; pass `projection_model` for reservation pricing. Add two tests — (i) a concurrent
run whose batches each make two calls must not exceed `MAX_LLM_CALLS_PER_RUN`; (ii) a
`subscription:*`-first chain that falls through to a priced provider must still stop at
`MAX_LLM_USD_PER_RUN`.

### 4.3 — Grades are not reproducible run-to-run

`llm_client.py:339-343` sends `{model, max_tokens, reasoning_effort, messages}` — no
`temperature`, `top_p`, or `seed`, on any provider. Against the earlier partial run's 25
overlapping leads, **14 grades moved**: three A→B demotions (86→79, 85→78, 83→76), nine Skip→C
promotions, scores swinging ±7. No scoring code changed between the runs.

Fix: pin `temperature: 0`.

### 4.4 — Silent and mislabelled errors

- **A quarter of retry spend is invisible.** 9 failure artifacts exist in `_api_bundles/`, but
  `debug_notes` explains only the 6 `validation_failed` repairs. The three
  `invalid_response_attempt_1.txt` retries (batches 010, 015, 016) appear in the call count and
  the bill and **nowhere in the run's own reporting**.
- **Mislabelled skip channel.** `score_leads.py:3909` prints *"Records skipped by LLM caps"* for a
  list that now also carries `batch_failed`, and the run exits 0. An operator judging batch
  completeness reads the wrong cause.
- **Machine-minted evidence citations.** `_fallback_fill_empty_evidence_refs`
  (`score_leads.py:2870-2909`, reached from `:3001-3011`) fills up to 3 ids off
  `evidence_ref_options` when the model returns none. The model declining to cite is a signal;
  overwriting it manufactures the input to `EVIDENCE_COUNT_BLOCKER`. Same reachability argument as
  §4.1 — a new default provider has a different propensity to omit refs.

### 4.5 — Parallel batch runs are guarded only by prose

`run_agent_pipeline.py:634-657` and the new `docs/hermes-commands.md` give copy-paste per-batch
`--lock-file` commands and correctly name the consequence: two Airtable records for one business,
both `Status: New`, both satisfying `queue_state.QUEUE_FILTER_FORMULA`; `mark_sent` flips one and
the twin is served again. `MARK_SENT_ALLOWED_SOURCE_STATUSES` cannot catch it — the second record
legitimately *is* `new`. That is the 2026-07-31 double-contact incident via a route the gate does
not cover.

The default is safe (`--airtable` defaults to `skip`, `:626-630`) and the documentation is honest,
but there is **no code guard**: `main()` (`:784-793`) acquires whatever lock file it is handed and
`_run_pipeline` runs the sync (`:759-767`) regardless. One `if`: refuse when
`args.airtable != "skip"` and `Path(args.lock_file) != DEFAULT_LOCK_FILE`.

### 4.6 — Billed Places spend is unbounded, and now 6× faster

`run_agent_pipeline.py` never passes `--max-details`, so `collect_raw_rows` builds
`DetailsBudget(None)` (`make_search_batch.py:686`) — no ceiling. Pre-existing, but this range
removed the per-call `time.sleep(0.15)` and added 6-way concurrency
(`resolve_details_workers`, `:680`), so unbounded billed spend now runs ~6× faster with no brake.
`docs/hermes-commands.md` names this honestly; nothing enforces it. Suggested: a
`getenv_int("MAX_DETAILS_CALLS_PER_RUN", …)` default inside `collect_raw_rows` rather than `None`.

### 4.7 — Lead PII now defaults to a new processor, undocumented

`build_llm_scoring_record` (`score_leads.py:2506-2578`) ships per lead: business name, vertical,
category, city, **street address** (`:2513`), **phone** (`:2514`), website URL, Google Maps URL,
rating/review count, buyer-intent stack, screenshot paths — plus up to 8 raw page-text snippets
(`:2179-2205`) taken as sentence windows around terms including `"contact"`, `"call"`, `"text"`.
There is no email field in the capsule, but a window around `"contact"` on a contact page
routinely contains the owner's name and email. Assume email is in scope.

**New in destination.** At `b644bcd`, `score_batch` was Anthropic-only and the codex/claude CLI
adapter was a standalone script, not an automatic chain member. This range makes Moonshot AI the
**first and default** provider (`llm_client.py:32`), so the automatic destination for identifiable
small-business owners' names, addresses and phone numbers moves to a new processor in a different
jurisdiction. The only opt-out is reordering `LLM_SCORING_PROVIDER_CHAIN`.

**Undocumented.** A grep for `PII`, `data processing`, `third-party processor`, `data residency`
across every `.md` in the repo returns **zero hits**. `AGENTS.md:91-97` documents the chain without
naming what crosses it. No redaction step, no field allowlist framed as a privacy boundary, and
nothing that would stop a future field addition from widening the payload silently.

Contacts nobody, so not blocking — but it is the same shape as §4.1: a named human's contact
details moving somewhere nobody decided they should go.

### 4.8 — Truncated-crawl absence flags still score as prospect weakness

`enrich_leads.py:1462-1466` rewrites `contact_form_detected` / `estimate_form_detected` /
`form_strength` to `"Unknown"` when the crawl was truncated, but `:1468-1469` still appends
`no_form_detected` from that same truncated crawl and `:1470-1471` still appends
`no_clear_cta_detected`. Downstream: `score_leads.py:836` charges +8 for `no_form_detected` when
corroborated (and `weak_services = not services_ok` at `:833` is itself a corroborator);
`lead_machine/icp_lanes.py:556` and `:913` branch on it; `lead_machine/evidence.py:608` adds +14
for a weak CTA inside the `not strong_estimate_form` branch that the `"Unknown"` rewrite
*guarantees* is entered.

`tests/test_enrichment_fetch_budget.py:589` pins the property only through
`build_evidence_profile().visible_pain_score` on one fixture whose truncated pages happen to carry
form controls, so `no_form_detected` never fires there. Suggested test: compare
`score_leads.score_online_weakness` at the ceiling vs an unlimited budget, on a site whose only
form lives past the 16th candidate page.

### 4.9 — Unpriced models do not abort estimation as AGENTS.md claims

`AGENTS.md:93-94`, as amended by this range, asserts: *"Every model the chain can resolve must have
a price … or the run aborts at cost estimation."* Neither half holds. `resolve_projection_model`
`continue`s past an `UnknownLlmModelError` candidate (`score_leads.py:2088-2089`), so an unpriced
chain member does not abort as long as one other member is priced — e.g. default chain with
`KIMI_SCORING_MODEL=kimi-k2-0905-preview` (absent from the table; `llm_pricing`'s prefix match at
`:336` does not reach `kimi-k3`) falls through to `subscription:codex` and prints
`[LLM COST] … projected $0.00` with no `[REFUSE]`. `resolve_served_model` (`:2096-2118`) then
charges an unpriced served model at the run-level fallback with a `[WARN]` instead of failing;
when that fallback is a `subscription:*` id, real paid spend is banked as `$0.00`.

Requires a config error to reach — unlike §4.2(a) and (b).

### 4.10 — Observability, operability, hygiene

- **No timing instrumentation anywhere.** No `perf_counter` / `elapsed` / `duration` in
  `run_agent_pipeline.py` or `run_pipeline.py`. The plan's verification step 5 was "record wall
  clock per stage so the win is measured, not asserted" — every measurement in this project has
  been made by hand and cannot be reproduced from artifacts.
- **The run summary is stale and wrong.** `output/agent_run_summary_garage_doors_hamilton_medium_001.json`
  reports `scored: 0`, `airtable_synced: 0`, `mode: search-only` from 2026-08-04, while a 133-lead
  scored CSV from 2026-08-06 sits beside it. The file meant to say what happened does not.
- **Today's batch is stranded mid-pipeline** — scored, $2.64 spent, but no
  `packets_garage_doors_hamilton_medium_001/` directory exists and nothing reached Airtable. The
  30 sendable leads are not actionable as they stand.
- **`make compile` omits the two most-changed modules** — `lead_machine/llm_client.py` and
  `lead_machine/sendability.py` are both absent from the `Makefile:33-34` list.
- **The operating skill predates the work.** `.claude/skills/run-lead-batch/SKILL.md` was last
  touched 2026-07-31 and mentions none of `LLM_SCORING_CONCURRENCY`, `PAGE_FETCH_WORKERS`,
  `PLACES_DETAILS_WORKERS`, `--lock-file`, or the provider chain. AGENTS.md documents them; the
  procedure that actually runs the pipeline does not use them.
- **The scored CSV is 53 MB across 186 columns** for 133 leads — 38.6 MB `Raw Evidence` +
  12.3 MB `Evidence Snapshot`. Unopenable in a spreadsheet, slow to parse everywhere, and it is
  the artifact the operator is meant to review.
- **`KIMI_API_KEY` ships blank** in `.env.example:10` while the default chain is kimi-first. Without
  it, kimi raises `ProviderUnavailable` and every run silently falls to the codex CLI.
- **Plan defect:** verification step 4 referenced `.venv/bin/python verify_batch.py`. No such script
  exists — the maker≠checker verifier is the `verify-batch` *skill*.
- **3 stale worktrees** attached, 2 of them on branches already merged into `main`.

### 4.11 — INFRASTRUCTURE: the work exists in one place only

`main` is **22 commits ahead of `origin/main`**, unpushed since 2026-08-02 (`228cb53`). The entire
7-phase plan and the multi-provider merge are local-only.

On top of that, only 6 files under `.claude/` are tracked. **Untracked:** `.claude/agents/`
(both `invariant-guard.md` and `compliance-reviewer.md` — the reviewers used for this audit),
`.claude/hooks/block-bare-unittest.sh`, `.claude/settings.json`, and the `airtable-schema-change`
and `gate-triage` skills.

There is also ~241 insertions of uncommitted work across 25 files on `main` (the Airtable
status-consolidation line, per the untracked `docs/superpowers/plans/2026-08-03-*`), unrelated to
the perf plan. `HEAD` passes independently of it, so it is not load-bearing — but it means the perf
commits were authored against a mixed tree.

---

## 5. Verified clean

Recorded because these were the concurrency risks most worth betting on, and they held.

- **Compliance fails closed on every path this range touched.** Suppression index loaded on the
  main thread and passed by value as an immutable set: `make_search_batch.py:1124` (ahead of the
  first billed Places call, `ComplianceError` → exit 1 at `:1138`), `enrich_leads.py:2329`
  (→ `:2384` into workers), `score_leads.py:3772-3781`. Pre-crawl check `enrich_leads.py:1899` and
  post-crawl re-check against scraped emails/phones `:2000-2010` both unchanged. No new
  `()`/`set()`/`None` default on a suppression argument; no new entry point reads leads without
  loading the index; no new contact channel.
- **Gate 0 holds, unhedged.** No email/SMS SDK, no webhook, no scheduler in the range. The only new
  outbound HTTP is `llm_client.py:355` (Moonshot). `mark_sent.py` / `mark_outcome.py` are not in
  the diff. No path from this range to a message reaching a person.
- **A contained batch failure cannot promote a lead.** `_record_batch_failure`
  (`score_leads.py:3341-3364`) writes no score file; `score_records_manual_run(..., only_scored=True)`
  (`:3583-3589`) then produces no row — no CSV row, no Airtable record, nothing
  `QUEUE_FILTER_FORMULA` can readmit. Each is printed with `reason=batch_failed`.
- **`sendability.admit()` remains the only place a lead is declared sendable.** `sendability.py`
  untouched in range; no new `Sendable = "Yes"`, no tier assignment bypassing `admit`. The
  repo-wide AST scan at `tests/test_sendability_admission.py:167-229` still passes.
- **Review-velocity banking** — exactly one observation per lead per run holds at the raised
  `DEFAULT_BROWSER_ENRICH_WORKERS = 5`; `ReviewSnapshotStore` holds a write lock plus a 10s busy
  timeout.
- **Order stability end to end.** `fetch_wave` returns by submission index with `strict=True` zip,
  so `_candidate_page_name` slot numbering (`estimate`, `estimate_2`, …) is unchanged;
  `collect_raw_rows` mutates `seen_place_ids`, `raw_rows`, `warnings`, `errors` and
  `duplicate_reason_counts` exclusively on the calling thread and collects futures in submit order;
  `_bank_batch` merges on the calling thread in submit order with deterministic plan-time
  `batch_id`s.
- **`DetailsBudget.reserve()`** (`make_search_batch.py:686-696`) is a correct atomic
  check-and-increment under one lock. No double-decrement, no overshoot.
- **`_ensure_dotenv_loaded`** (`llm_client.py:139-153`) is correct double-checked locking.
  `sanitized_environment` only copies `os.environ`. No other worker-thread mutation.
- **No metro or trade names in scoring logic** — every added non-comment line checked; hits only in
  test fixtures and measurement docstrings.
- **Correct tightening:** `lead_machine/evidence.py:60-69` adds `candidate_page_budget_exhausted`
  to `STATIC_FETCH_REVIEW_FLAGS`, so our own crawl budget no longer grades as the prospect's site
  being weak. That would have inflated `visible_pain_score` and cleared `VISIBLE_PAIN_BLOCKER`.
- **Cap constants match docs** — `AGENTS.md:229-230` updated to the resized defaults. No drift.
- **Gates CI dependencies are correct** — `requests` + `python-dotenv` covers the full eval import
  chain (`evals/*.py`, `lead_machine/verdict.py`, `lead_machine/sendability.py` pull no third-party
  beyond those). The earlier lxml gap was in `tests.yml` and is closed.
- **SMS consent, reply-stops-sequence, email hygiene, recording consent** — untouched. The
  2026-07-31 Setter Ops recording-consent gap remains open; this range neither closes nor worsens it.

---

## 6. Recommended order

1. **Push, and track `.claude/`.** Everything else is recoverable; this is not. (§4.11)
2. **Fix the claims round-trip** — escape on write or store JSON. Three things behind it: an
   unbacked claim, a misattributed citation, and a red daily gate. (§4.1)
3. **Fix concurrent spend caps as one change** — count actual calls, reconcile against banked
   totals, price reservations with `projection_model`; add the two cap tests. (§4.2)
4. **Pin `temperature: 0`.** (§4.3)
5. **The one-line parallel-run guard** (§4.5) and **bound `--max-details`** (§4.6).
6. Then the cheap ones: Phase 6's two missing items, batch-id on `[LLM REQUEST]`, the `make compile`
   list, the skip-channel label, `run-lead-batch/SKILL.md`. (§4.4, §4.10)

Deferred, needing a decision rather than a patch: document what crosses the Moonshot boundary
(§4.7), and the truncated-crawl absence flags (§4.8).
