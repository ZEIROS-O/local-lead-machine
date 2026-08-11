# Final Fix Wave Report

## Status

Complete. The three final-review findings are fixed on `feat/reverdict-upload` with strict test-first RED/GREEN evidence. No Airtable calls or operational corpus reads were performed. The authoritative full suite was intentionally not run per the task boundary.

## Changes

### 1. Create-only operational ownership

- Extended `upload_airtable.OPERATIONAL_FIELDS` with `Last Contacted` and `Next Follow-Up`.
- Kept both date fields in `CSV_AIRTABLE_FIELDS`, so CSV conversion retains them for POST creation.
- Added an end-to-end CSV conversion plus `plan_airtable_sync` regression covering all five human-owned fields: `Status`, `Last Contacted`, `Next Follow-Up`, `Assigned Setter`, and `Outreach Channel`.
- The regression proves all five values remain in POST payloads and all five are stripped from PATCH payloads.

### 2. Pairwise path-collision refusal

- Added a resolved-path validator in `prepare_airtable_corpus.py`.
- Validation runs before loading any source/support file or writing/replacing output.
- It checks every source, output, optional report, optional vertical-overrides file, and optional excluded-upsert-keys file pairwise.
- Added byte-preservation regressions for `report == source`, `report == output`, and `vertical-overrides == output`.
- Updated the existing multi-source report fixture to use three genuinely distinct source paths, matching the new no-alias contract.

### 3. Fail-closed CSV structure

- Added `lead_machine/csv_validation.py` as the shared structural CSV reader.
- Both `refresh_verdicts.py` and `prepare_airtable_corpus.py` now reject missing/blank headers, duplicate headers, and rows containing unexpected extra cells before projection.
- Changed refresh output writing from `extrasaction="ignore"` to `extrasaction="raise"`.
- Added byte-preservation regressions for in-place refresh and existing corpus output across all three malformed shapes.

## TDD Evidence

The worktree has no local `.venv` link, so all valid test commands used `/home/lucascoh/local-lead-machine/.venv/bin/python` from the repository's existing virtualenv.

### Operational ownership

RED command:

```text
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest tests.test_setter_assignment.UploadSetterAssignmentTest.test_csv_operational_fields_are_seeded_on_create_only
```

Expected RED: 1 failure. `Last Contacted` was unexpectedly present in the PATCH payload, alongside `Next Follow-Up`.

GREEN: the same command passed 1 test.

### Path collisions

RED command:

```text
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest tests.test_prepare_airtable_corpus.PrepareAirtableCorpusTests.test_report_cannot_replace_a_source_or_existing_output tests.test_prepare_airtable_corpus.PrepareAirtableCorpusTests.test_report_cannot_alias_output_and_preserves_existing_bytes tests.test_prepare_airtable_corpus.PrepareAirtableCorpusTests.test_support_file_cannot_alias_output_and_preserves_existing_bytes
```

Expected RED: 3 failures. Each invocation returned success (`0`) instead of refusal (`2`) and proceeded to overwrite a colliding target.

GREEN: the same command passed all 3 tests.

### Malformed CSV

RED command:

```text
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest tests.test_refresh_verdicts.RefreshVerdictsTests.test_in_place_refresh_refuses_malformed_csv_without_changing_bytes tests.test_prepare_airtable_corpus.PrepareAirtableCorpusTests.test_malformed_source_refuses_without_replacing_existing_output
```

Expected RED: 6 subtest failures across the two regression methods. Refresh accepted blank headers, duplicate headers, and extra cells; corpus preparation accepted blank/duplicate headers, while extra cells were rejected only later by `DictWriter` with the wrong structural error.

GREEN: the same command passed both regression methods and all six malformed cases.

## Focused Verification

- `tests.test_prepare_airtable_corpus`: 22 tests, OK.
- `tests.test_refresh_verdicts tests.test_setter_assignment tests.test_upload_airtable_schema tests.test_airtable_identity_collisions`: 62 tests, OK.
- `tests.test_lead_machine_contracts`: 217 tests, OK.
- Fresh combined final command covering all six modules above: 301 tests in 6.199s, OK.
- `git diff --check`: clean before report creation; rerun before commit.

## Files Changed

- `upload_airtable.py`
- `prepare_airtable_corpus.py`
- `refresh_verdicts.py`
- `lead_machine/csv_validation.py`
- `tests/test_setter_assignment.py`
- `tests/test_prepare_airtable_corpus.py`
- `tests/test_refresh_verdicts.py`
- `.superpowers/sdd/2026-08-11-reverdict-and-upload/final-fix-report.md`

## Concerns

- The authoritative full suite remains for the controller, as explicitly requested.
- No production Airtable verification was attempted; this wave changes local planning and fail-closed input handling only.
