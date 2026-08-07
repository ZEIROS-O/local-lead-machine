# Airtable Status Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every current repository surface match the live Airtable consolidation `Do Not Contact -> Not Fit`, `Sent -> Follow-Up`, and `Mockup Needed -> New`.

**Architecture:** `lead_machine.queue_state` remains the canonical status vocabulary and accept-rate partition. Queue, upload, contact logging, post-contact outcomes, Hermes messaging, schema checks, and current operator documentation consume that contract without a compatibility layer for deleted Airtable choices. Historical sent-event names remain action vocabulary, while Airtable status outputs become `Follow-Up`.

**Tech Stack:** Python 3, stdlib `unittest`, mocked `requests`, Airtable formulas, Markdown operator documentation.

**Execution status:** Completed and verified 2026-08-03; all task checkboxes below are retained as the original execution sequence. The authoritative suite passed 1,017 tests.

## Global Constraints

- Repository-only: make no live Airtable request or mutation.
- Never add Airtable `typecast`.
- Preserve suppression-ledger semantics and fail-closed contact checks.
- Preserve `mark_sent` and historical `sent` event names where they describe actions rather than Airtable choices.
- Do not change cadence, scoring, ICP, enrichment, or unrelated vertical/search work.
- Do not commit or push unless Lou explicitly asks.
- Run tests with `.venv/bin/python -m unittest`; authoritative discovery is `.venv/bin/python -m unittest discover -s tests`.

---

### Task 1: Canonical vocabulary, queue, and accept-rate partition

**Files:**
- Modify: `tests/test_queue_state_formula.py`
- Modify: `tests/test_accept_rate_eval.py`
- Modify: `lead_machine/queue_state.py`
- Modify: `lead_machine/compliance.py`
- Modify: `query_ready_to_send.py`

**Interfaces:**
- Produces: `PROSPECT_STATUS_CHOICES = ("New", "Manual Review", "Not Fit", "Follow-Up", "Won", "Lost", "Booked")`.
- Produces: `QUEUE_STATUS_CLAUSE.allowed == ("New",)` and `AND({Verdict}='send',{Status}='New')`.
- Produces: accepted `{Follow-Up, Won, Lost, Booked}`, rejected `{Not Fit}`, undecided `{New, Manual Review}`.
- Produces: `MARK_SENT_ALLOWED_SOURCE_STATUSES == {"new"}`.

- [ ] **Step 1: Write literal failing contract tests**

Change the queue tests to assert the exact seven-value tuple, exact single-status filter/formula,
exact partition, and `New`-only queue clause. Change accept-rate fixtures so contacted/accepted
records use `Follow-Up`, and remove `Mockup Needed` from undecided cases.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_queue_state_formula tests.test_accept_rate_eval tests.test_send_queue_scripts.TestMarkSent.test_mark_sent_source_gate_tracks_the_queue_gate_and_the_vocabulary
```

Expected: failures show the old ten-choice vocabulary, two-choice queue, and old buckets.

- [ ] **Step 3: Implement the canonical contract**

In `queue_state.py`, replace the vocabulary, buckets, queue clause, comments, and failure text with
the consolidated values. In `compliance.py`, accept only normalized `new` at the mark-sent boundary.
Update `query_ready_to_send.py` current help/doc text from `New or Mockup Needed` to `New`.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command and require all tests to pass.

---

### Task 2: Contact logging and post-contact outcomes

**Files:**
- Modify: `tests/test_send_queue_scripts.py`
- Modify: `tests/test_mark_outcome.py`
- Modify: `mark_sent.py`
- Modify: `mark_outcome.py`

**Interfaces:**
- Consumes: `is_mark_sent_source_status(Status)` from Task 1.
- Produces: `apply_mark_sent(...)` PATCHes and receipts report `Status = Follow-Up`.
- Produces: exact idempotency recognizes `Follow-Up` plus matching contact fields.
- Produces: `OUTCOME_SOURCE_STATUSES == {"follow-up"}`.

- [ ] **Step 1: Write failing contact-transition tests**

Update mark-sent helpers and assertions so a `New` record PATCHes to `Follow-Up`, an exact
`Follow-Up` state returns `already_applied`, conflicting `Follow-Up` contact data fails, and
`Mockup Needed` is refused. Update outcome fixtures so only case-normalized `Follow-Up` is accepted
and `Sent`/`Mockup Needed` are refused.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_send_queue_scripts.TestMarkSent tests.test_mark_outcome.TestApplyMarkOutcome
```

Expected: failures show `mark_sent` still writes/checks `Sent` and `mark_outcome` still accepts it.

- [ ] **Step 3: Implement minimal writer changes**

Introduce `TARGET_STATUS = "Follow-Up"` in `mark_sent.py` and use it in exact-state checks,
conflict detection, PATCH payloads, receipts, and errors. Change
`mark_outcome.OUTCOME_SOURCE_STATUSES` and eligibility text to `Follow-Up` only. Keep request order,
cadence, activity/deal behavior, consent, suppression, and no-typecast guarantees unchanged.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command and require all tests to pass.

---

### Task 3: Upload defaults, schema checker, and Hermes boundary

**Files:**
- Modify: `tests/test_lead_machine_contracts.py`
- Modify: `tests/test_required_choices.py`
- Modify: `tests/test_hermes_queue.py`
- Modify: `tests/test_setter_assignment.py`
- Modify: `tests/test_backfill_setter_assignments.py`
- Modify: `upload_airtable.py`
- Modify: `lead_machine/hermes_queue.py`

**Interfaces:**
- Consumes: canonical choices and queue source statuses from Task 1.
- Produces: sendable uploads default to `Status = New`; existing status remains protected on update.
- Produces: required-choice audits require the consolidated vocabulary and writer/operator declarations.
- Produces: Hermes previews describe Airtable `Status=Follow-Up`, while command and stored action events remain mark-sent/sent.

- [ ] **Step 1: Write failing boundary tests**

Change the upload contract to expect `New`. Change required-choice assertions and complete fixtures
to exclude the three deleted choices while continuing to require `Booked` and `Held`. Change Hermes
mock receipts and current status/error copy to `Follow-Up` and `New` without renaming historical
sent-event state.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_lead_machine_contracts tests.test_required_choices tests.test_hermes_queue
```

Expected: failures identify the old upload default, deleted required choices, and stale Hermes copy.

- [ ] **Step 3: Implement upload and Hermes changes**

Set the sendable upload default to `New` in `upload_airtable.csv_row_to_record`. Update ownership
comments so examples use a current protected terminal status. Update only Airtable-status-facing
Hermes text and receipt expectations; retain v1 `Sent` event parsing and idempotent write-back keys
where `Sent` names the action rather than a base choice.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command and require all tests to pass.

---

### Task 4: Current documentation and stale-reference audit

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/airtable_schema_required.md`
- Modify: `docs/setter-assignment-sop.md`
- Modify: `docs/airtable_cutover_runbook_2026-07-30.md`
- Modify: `docs/verdict_cutover_runbook.md`
- Preserve: `docs/airtable_cutover_receipt_2026-07-30.json`
- Preserve: `docs/audits/**`
- Preserve: `docs/notion-plans-to-consolidate/**`

**Interfaces:**
- Consumes: the seven-value contract and workflow transitions from Tasks 1-3.
- Produces: current instructions that never tell an operator to recreate or use a deleted choice.

- [ ] **Step 1: Update current guidance**

Document the canonical choices, `New`-only call queue, `mark_sent: New -> Follow-Up`,
`mark_outcome` operating from `Follow-Up`, and the independent suppression-ledger requirement.
Where a dated runbook mixes historical state with current instructions, preserve the historical
observation but update actionable formulas/checklists and add a dated consolidation note.

- [ ] **Step 2: Audit every remaining reference**

Run:

```bash
rg -n --glob '!run_state/**' --glob '!data/**' --glob '!output/**' --glob '!docs/audits/**' --glob '!docs/notion-plans-to-consolidate/**' "Do Not Contact|Mockup Needed|Status=Sent|Status = Sent" .
```

Keep only design/history statements or action-event vocabulary; remove stale current Airtable guidance.
Do not rewrite immutable receipts or historical audits.

- [ ] **Step 3: Check formatting**

Run `git diff --check` and require exit 0.

---

### Task 5: Full verification and scope audit

**Files:**
- Verify only: all modified files.

**Interfaces:**
- Produces: local evidence that the consolidated contract passes without network access.

- [ ] **Step 1: Run focused regression suites**

```bash
.venv/bin/python -m unittest tests.test_queue_state_formula tests.test_accept_rate_eval tests.test_send_queue_scripts tests.test_mark_outcome tests.test_lead_machine_contracts tests.test_required_choices tests.test_hermes_queue
```

Expected: all tests pass.

- [ ] **Step 2: Run the authoritative full suite**

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: all discovered tests pass with no network or Airtable mutation.

- [ ] **Step 3: Inspect scope and whitespace**

Run `git diff --check`, `git status --short`, and a scoped `git diff` over the files named above.
Expected: only intended status-contract changes plus the approved spec and this plan; unrelated
vertical/search work remains untouched.
