# Consolidation Integration Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Status: complete.** All five tasks landed and shipped as `6390d64`; the suite is green at 869
tests. The runbook this plan produced was approved and then executed as far as the Airtable API
allows — see [`../../airtable_cutover_runbook_2026-07-30.md`](../../airtable_cutover_runbook_2026-07-30.md)
for what is done and what is still hand work.

**Goal:** Close the four phases-3/4/5 integration findings, restore a green suite, and generate an ordered code-only Airtable runbook for Lou's approval.

**Architecture:** Keep `lead_machine.airtable_sync` as the final authority for fields that can reach Airtable, and make upload/manual paths consume that authority. Keep `lead_machine.queue_state.PROSPECT_STATUS_CHOICES` as the Status vocabulary authority. Extend the existing read-only cutover model to distinguish migration pairs from plain deletions. Treat external/review collection as explicit opt-in pipeline stages with machine-readable completion and cost state.

**Tech Stack:** Python 3.10, stdlib `unittest`, Airtable REST payload planning, DataForSEO/Semrush/PageSpeed collector CLIs, Markdown runbook.

**Approved phase-6 amendment (2026-07-30):** `Selected Opportunity` is deferred
entirely. It has no producer or approved taxonomy, so it is neither written by
code nor created as an empty schema-only field. The live cutover adds only `ICP
Lane` and `Opportunity Score`.

## Global Constraints

- Do not mutate Airtable; connector use is read-only verification only.
- Do not commit or push.
- Preserve all pre-existing dirty-worktree changes.
- `typecast` must remain absent from every Airtable write path.
- Scoring V3 remains shadow-only.
- Historic scored CSVs are not rewritten.
- Phase 6 produces a proposed runbook only; execution requires separate approval.

---

### Task 1: Make the CRM field swap reach the final Airtable payload

**Files:**
- Modify: `lead_machine/airtable_sync.py`
- Modify: `upload_airtable.py`
- Modify: `lead_machine/manual_lead_insert.py`
- Modify: `tests/test_upload_airtable_schema.py`
- Modify: `tests/test_lead_machine_contracts.py`
- Modify: `tests/test_manual_lead_insert.py`

**Interfaces:**
- Consumes: `airtable_cutover_fields_2026-07.json["fields"]`
- Produces: `AIRTABLE_ALLOWED_FIELDS` matching the post-cutover code-side schema; final payloads containing `ICP Lane` and `Opportunity Score`

- [x] Add a failing test that builds the final payload from `csv_row_to_record()` and asserts `ICP Lane` and numeric `Opportunity Score` survive.
- [x] Add a failing invariant test that the cutover deletion set is disjoint from `AIRTABLE_ALLOWED_FIELDS`.
- [x] Add or update a manual-insert test proving cutover fields are not emitted.
- [x] Run the focused tests and confirm failures at the final payload boundary.
- [x] Remove all 13 doomed fields from the shared allowlist and manual producers.
- [x] Add `ICP Lane` and `Opportunity Score` to the shared allowlist and typed number handling.
- [x] Keep `Selected Opportunity` out of writes and out of the live schema because no producer or approved taxonomy exists.
- [x] Change `ICP Lane`'s proposed schema type to the locked single-select and enumerate the eight lane choices.
- [x] Run focused tests until green.

### Task 2: Make the eight-value Status vocabulary canonical

**Files:**
- Modify: `upload_airtable.py`
- Modify: `lead_machine/manual_lead_insert.py`
- Modify: `AGENTS.md`
- Modify: `tests/test_upload_airtable_schema.py`
- Modify: `tests/test_manual_lead_insert.py`
- Modify: `tests/test_queue_state_formula.py`

**Interfaces:**
- Consumes: `lead_machine.queue_state.PROSPECT_STATUS_CHOICES`
- Produces: schema reports and manual validation that accept `Won`/`Lost` and reject `Closed`

- [x] Add failing tests that the upload schema and manual controlled choices equal `PROSPECT_STATUS_CHOICES`.
- [x] Run the focused tests and confirm failure on the old `Closed` vocabulary.
- [x] Import/use the canonical tuple rather than restating Status choices where practical.
- [x] Update operator-facing code documentation to describe `Won`/`Lost`.
- [x] Leave the live base unchanged; phase 6 will propose adding choices before the code is used live.
- [x] Run focused tests until green.

### Task 3: Enforce consent migration before cutover deletion

**Files:**
- Modify: `airtable_schema_cutover.py`
- Modify: `tests/test_airtable_schema_cutover.py`

**Interfaces:**
- Consumes: optional `migrate_before_delete` mapping in a fields config
- Produces: `FieldCutover.pairs` for migrated fields and `FieldCutover.plain` for true drops; `safe_to_delete=False` on missing/conflicting consent migration

- [x] Add a failing test loading the shipped config and asserting `Sales — Consent to Text -> Consent Posture` is a migration pair, not a plain deletion.
- [x] Add a failing test where the checkbox is true and `Consent Posture` is blank; assert deletion is refused.
- [x] Add a failing test where migrated values reconcile; assert deletion may proceed.
- [x] Run focused tests and confirm the expected failures.
- [x] Parse and validate `migrate_before_delete`, rejecting malformed mappings and targets also scheduled for deletion.
- [x] Include migration pairs in snapshots and generated checklists.
- [x] Run focused tests until green.

### Task 4: Complete optional signal-stage integration and truthful accounting

**Files:**
- Modify: `collect_review_signals.py`
- Modify: `run_pipeline.py`
- Modify: `tests/test_run_pipeline.py`
- Modify: `tests/test_scoring_v3_operations.py` if needed for external observation fixtures

**Interfaces:**
- Consumes: completed review task receipts and normalized external signal files
- Produces: explicit `pending`, `partial`, `merged`, and `submitted` summary state; task/cost counts; optional external-signal merges before scoring

- [x] Add a failing test proving pending review tasks do not report `merged` and do not continue scoring under `--merge-review-signals`.
- [x] Add a failing test that review summary includes submitted/completed/pending counts and estimated/actual USD.
- [x] Add a failing pipeline test for opt-in `collect_external_signals.py merge` before scoring.
- [x] Run focused tests and confirm failures.
- [x] Make review collection emit a machine-readable collection summary and return a distinct pending outcome.
- [x] Gate scoring when the user explicitly requested review evidence but tasks remain pending.
- [x] Add repeatable `--external-signal` inputs that invoke the existing merge command before scoring without default spend.
- [x] Include signal paths, counts, state, and cost in `run_summary_<batch>.json`.
- [x] Run focused tests until green.

### Task 5: Verify and generate the phase-6 Airtable runbook

**Files:**
- Create: `docs/airtable_cutover_runbook_2026-07-30.md`
- Modify: `docs/airtable_schema_required.md`

**Interfaces:**
- Consumes: current live Airtable schema (read-only), shipped cutover config, canonical Status/lane choices, required select choices
- Produces: ordered proposed changes with preconditions, exact field types/choices, backup/verification commands, and explicit approval stop

- [x] Run `git diff --check`.
- [x] Run all focused tests touched above.
- [x] Run `.venv/bin/python -m unittest discover -s tests` and require zero failures.
- [x] Query Airtable schema read-only and reconcile every proposed field/choice with current state.
- [x] Write the runbook in safe order: snapshot; migrate consent; create fields/choices; update descriptions/views; delete fields/tables/views; verify record count and schema.
- [x] State explicitly that the runbook has not been executed and requires Lou's approval.
- [x] Re-run documentation/content assertions and `git diff --check`.
