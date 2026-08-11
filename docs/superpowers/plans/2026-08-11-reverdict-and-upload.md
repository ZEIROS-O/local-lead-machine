# Re-verdict and Airtable Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one reproducible, upload-ready CSV whose 1,089 upsertable leads carry verdicts derived from the current sendability authority, including all 429 currently sendable leads, without overwriting human-owned Airtable state.

**Architecture:** Add a single-input `refresh_verdicts.py` projection that reuses `apply_sendability` and `apply_verdict`, then add `prepare_airtable_corpus.py` to select the newest row per uploader upsert key, normalize registered verticals, require explicit overrides for unresolved legacy rows, and invoke the projection. Both tools are local-only, atomic, fail-closed, and emit machine-readable reports. The existing uploader remains unchanged and is exercised first with an empty packet directory and `--dry-run`; `--live` is a separate final gate.

**Tech Stack:** Python 3.10, stdlib `csv`/`json`/`argparse`/`tempfile`, existing `lead_machine.sendability`, `lead_machine.verdict`, `lead_machine.verticals`, and stdlib `unittest`.

## Global Constraints

- No LLM calls and no network calls in either new preparation tool.
- `lead_machine.sendability.apply_sendability(row)` remains the sendability authority; do not reimplement its gate.
- `lead_machine.verdict.apply_verdict(row)` remains the only production function that writes `Verdict` and `Verdict Reasons`.
- For every output row, `evaluate_sendability(row)["sendable"]` must equal `(row["Verdict"] == "send")`.
- For every output row, `row["Packet Tier"]` must equal `VERDICT_PACKET_TIERS[row["Verdict"]]`.
- Preserve `Status`, `Last Contacted`, `Next Follow-Up`, `Assigned Setter`, and `Outreach Channel` byte-for-byte in local CSV preparation; the uploader's existing ownership enforcement must keep them out of PATCH payloads.
- Use the uploader upsert identity exactly: `place:<Place Key>`, otherwise `phone:<normalize_phone(Phone)>`.
- Unknown verticals fail closed. The three legacy `manual_existing` lawn-care rows require explicit upsert-key overrides to `hardscaping`; no implicit default is allowed.
- Write output atomically through a temporary file in the destination directory.
- Do not mutate `output/` source files. Write operational artifacts and receipts under `/tmp` until the dry-run is accepted.
- No live Airtable write until the dry-run exits 0, reports 1,089 records and 429 `send` verdicts, and required select choices pass.
- Gate 0 remains: this work uploads CRM records only and does not transmit to any prospect.

---

### Task 1: Current-authority verdict refresh

**Files:**
- Create: `refresh_verdicts.py`
- Create: `tests/test_refresh_verdicts.py`

**Interfaces:**
- Consumes: `apply_sendability(row: dict) -> dict`, `apply_verdict(row: dict) -> dict`, `evaluate_sendability(row: dict) -> dict`, and `VERDICT_PACKET_TIERS`.
- Produces: `refresh_row(row) -> dict`, `validate_refreshed_row(row) -> list[str]`, `refresh_csv(input_path, output_path=None) -> dict`, and CLI `refresh_verdicts.py INPUT [--output PATH] [--check]`.

- [ ] **Step 1: Write failing projection and ownership tests**

Create `tests/test_refresh_verdicts.py` with a real sendable fixture derived from `tests.test_verdict.SENDABLE_ROW`. Assert that a stale `Verdict=reject` becomes `send`, `Verdict Reasons` is refreshed, `Packet Tier` equals `VERDICT_PACKET_TIERS["send"]`, and `evaluate_sendability(updated)["sendable"]` is true. Start with the exact ownership sentinel:

```python
OPERATIONAL = {
    "Status": "Follow-Up",
    "Last Contacted": "2026-08-10",
    "Next Follow-Up": "2026-08-14",
    "Assigned Setter": "Lou",
    "Outreach Channel": "Phone",
}
```

Assert every sentinel value is unchanged. Add a rejected row carrying stale `Verdict=send` and assert it becomes `reject`. Add a CSV round-trip test with a `Raw Evidence` field larger than 131,072 bytes.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest tests.test_refresh_verdicts -v
```

Expected: import failure for missing `refresh_verdicts`.

- [ ] **Step 3: Implement the minimal projection**

In `refresh_verdicts.py`, reuse the existing packet refresh field contract and define:

```python
VERDICT_REFRESH_FIELDS = [
    *refresh_packet_tiers.REFRESH_FIELDS,
    "Verdict",
    "Verdict Reasons",
]

def refresh_row(row):
    projected = apply_verdict(apply_sendability(row))
    updated = dict(row)
    for field in VERDICT_REFRESH_FIELDS:
        updated[field] = projected.get(field, "")
    errors = validate_refreshed_row(updated)
    if errors:
        raise VerdictRefreshError("; ".join(errors))
    return updated
```

`validate_refreshed_row` must check the send biconditional and verdict-to-tier mapping. `refresh_csv` must preserve existing field order, append missing refresh fields, raise the CSV size limit, write atomically, and return counts by verdict. `--check` must compute and print the would-be report without writing.

- [ ] **Step 4: Verify GREEN and idempotency**

Run the focused test command again. Add a test that runs `refresh_csv` twice and compares the output bytes after run one and run two; rerun until it passes.

- [ ] **Step 5: Commit Task 1**

```bash
git add refresh_verdicts.py tests/test_refresh_verdicts.py
git commit -m "feat(verdict): refresh stored verdicts from current gate"
```

---

### Task 2: Reproducible upload-corpus preparation

**Files:**
- Create: `prepare_airtable_corpus.py`
- Create: `tests/test_prepare_airtable_corpus.py`
- Modify: `refresh_verdicts.py` only if Task 2 exposes a missing public helper; do not change gate behavior.

**Interfaces:**
- Consumes: explicit source paths, `normalize_phone`, `resolve_profile(..., fallback="")`, `normalize_airtable_value("Vertical", value)`, and `refresh_verdicts.refresh_row`.
- Produces: `upsert_key(row) -> str`, `resolve_upload_vertical(row, overrides, key) -> str`, `prepare_corpus(paths, overrides) -> (fieldnames, rows, report)`, and CLI arguments `--source PATH [PATH ...]` (repeatable and flattened), `--output`, `--vertical-overrides`, `--expect-source-rows`, `--expect-output-rows`, and `--expect-skipped-no-key`.

- [ ] **Step 1: Write failing newest-wins and upsert-key tests**

Use temporary CSVs with controlled mtimes. Assert a newer Place Key row replaces an older row; a phone-only row uses normalized phone; two Place Keys sharing a phone remain distinct; two no-Place-Key rows sharing a phone collapse to the newer row; and a row missing both identifiers is listed in `skipped_no_upsert` rather than written.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest tests.test_prepare_airtable_corpus -v
```

Expected: import failure for missing `prepare_airtable_corpus`.

- [ ] **Step 3: Implement deterministic consolidation**

Sort sources by `(path.stat().st_mtime_ns, str(path))`, stream with the raised CSV field limit, retain `(source_path, line_number, original_row)` for the last row per upsert key, and construct a stable union of source headers. The report must include source file count, source row count, output row count, overwritten row count, skipped no-key rows, and selected source provenance for every output key.

- [ ] **Step 4: Write failing vertical tests**

Cover:

```python
("Hardscaping", "hardscaping")
("Landscaping / Hardscaping", "hardscaping")
("Masonry / Hardscaping", "hardscaping")
("Roofing", "roofing")
("Detailing", "detailing")
```

For `Vertical=service`, put `google.category` and `google.source_query` inside `Raw Evidence` and assert registered profiles resolve. Assert an unresolved `service` row raises unless its exact upsert key appears in the overrides JSON. Assert an unsupported non-service vertical also raises.

- [ ] **Step 5: Implement fail-closed vertical normalization**

First resolve the stored value through `resolve_profile(value, fallback="")`. For `service`, parse `Raw Evidence.google`, then resolve `category` plus `source_query` with `fallback=""`. Apply an override only by exact upsert key and validate the override through `normalize_airtable_value`. Reject malformed evidence, unknown choices, unused override keys, and any final vertical outside `VERTICAL_CHOICES`.

- [ ] **Step 6: Write failing losslessness and ownership tests**

After preparation, compare each output row with its selected source row. Permit changes only in `Vertical` and `refresh_verdicts.VERDICT_REFRESH_FIELDS`. Assert all other keys and values match, explicitly including all five operational fields. Assert the final report has zero biconditional violations, zero verdict/tier violations, and verdict counts matching the rows.

- [ ] **Step 7: Implement refresh, atomic output, and report checks**

Normalize `Vertical`, call `refresh_row`, validate allowed-field-only differences, then atomically write the output. Expected-count flags must refuse with exit 2 on mismatch. Write the JSON report to stdout and optionally to `--report PATH`; never include business names, phones, or raw evidence in the report.

- [ ] **Step 8: Verify focused tests and full suite**

Run:

```bash
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest tests.test_refresh_verdicts tests.test_prepare_airtable_corpus -v
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest discover -s tests
```

Expected: all tests pass, with the authoritative suite reporting at least the 1,420-test baseline plus the new tests.

- [ ] **Step 9: Commit Task 2**

```bash
git add prepare_airtable_corpus.py tests/test_prepare_airtable_corpus.py refresh_verdicts.py
git commit -m "feat(airtable): prepare a lossless current-verdict corpus"
```

---

### Task 3: Production-shaped corpus proof and Airtable gate

**Files:**
- Create outside Git: `/tmp/lpm-reverdict-upload/vertical-overrides.json`
- Create outside Git: `/tmp/lpm-reverdict-upload/corpus.csv`
- Create outside Git: `/tmp/lpm-reverdict-upload/prepare-report.json`
- Create outside Git: `/tmp/lpm-reverdict-upload/empty-packets/`
- Create outside Git for a live run only: `/tmp/lpm-reverdict-upload/upload-receipts.jsonl`

**Interfaces:**
- Consumes: the 51 `output/scored_*.csv` and `output/airtable_*.csv` files in the primary checkout, plus exact override keys for the three `manual_existing` lawn-care rows.
- Produces: a verified 1,089-record CSV with 429 send verdicts and an Airtable dry-run plan; a live upload is allowed only after every stated gate passes.

- [ ] **Step 1: Generate and inspect exact legacy overrides**

Resolve the three upsert keys read-only from their Place Key or normalized Phone. Write an overrides JSON mapping only those keys to `hardscaping`. Confirm they are the three `service` rows in `scored_manual_existing_manual.csv` and that the other four rows in that same file already carry `hardscaping` for the same lawn/landscaping source family.

- [ ] **Step 2: Prepare the corpus with hard expectations**

Run from the isolated worktree, passing the 51 source files explicitly:

```bash
/home/lucascoh/local-lead-machine/.venv/bin/python prepare_airtable_corpus.py \
  --source /home/lucascoh/local-lead-machine/output/scored_*.csv \
  --source /home/lucascoh/local-lead-machine/output/airtable_*.csv \
  --vertical-overrides /tmp/lpm-reverdict-upload/vertical-overrides.json \
  --output /tmp/lpm-reverdict-upload/corpus.csv \
  --report /tmp/lpm-reverdict-upload/prepare-report.json \
  --expect-source-rows 3048 \
  --expect-output-rows 1089 \
  --expect-skipped-no-key 1
```

The CLI must report verdict counts `send=429`, `reject=615`, `review=27`, `watch=18`; zero unsupported verticals; zero lossless-comparison failures; and no unused overrides. These are the four-way projected buckets on the 1,089-key upload corpus; the handoff's `662` figure is the different binary count of refused distinct leads before the no-key row and phone collision are removed.

- [ ] **Step 3: Prove idempotency and uploader-shaped equivalence**

Hash `corpus.csv`, run preparation a second time, and confirm the hash is unchanged. For every output row, call `upload_airtable.csv_row_to_record` with the empty packet directory and compare it with the record derived from the selected source after applying only the declared vertical/verdict refresh; require zero unexplained field differences.

- [ ] **Step 4: Verify required Airtable choices read-only**

Run:

```bash
/home/lucascoh/local-lead-machine/.venv/bin/python airtable_required_choices.py check
```

Require exit 0 before any live upload. A non-zero result stops the plan for manual base schema changes.

- [ ] **Step 5: Run the Airtable dry run with no packet-path writes**

Run from the primary checkout so its owner-readable `.env` is used:

```bash
.venv/bin/python upload_airtable.py \
  --input /tmp/lpm-reverdict-upload/corpus.csv \
  --packet-dir /tmp/lpm-reverdict-upload/empty-packets \
  --dry-run
```

Require exit 0, 1,089 planned records, exactly 429 `Verdict=send`, zero dropped `Vertical` fields, and no operational field in any PATCH payload. Record the actual POST/PATCH split; do not require the older 1,021/68 split because the base can change.

- [ ] **Step 6: Configure decision-ledger evidence before dialing**

Verify `LPM_DIAL_DECISIONS_JSONL` resolves to a durable path and `LPM_OUTCOMES_JSONL` or `SUPPRESSION_SOURCE` resolves to the real suppression ledger. Do not print secret values. This gate is required before the first dial, though it does not itself authorize a live upload or any prospect contact.

- [ ] **Step 7: Live upload only after explicit final review**

Present the dry-run counts, schema-check result, corpus hashes, and operational-field proof. On approval, run `upload_airtable.py` with `--live --receipts /tmp/lpm-reverdict-upload/upload-receipts.jsonl`; if interrupted, resume only with the same corpus hash and `--resume` using those receipts. Read back the written cohort and require 429 records with `Verdict=send` before handing the queue to the rep.

- [ ] **Step 8: Finish the branch**

Invoke `superpowers:verification-before-completion`, rerun focused tests and the full suite, inspect `git diff --check` and `git status`, then invoke `superpowers:finishing-a-development-branch` to offer merge/push/keep/discard choices.
