# Re-verdict Airtable upload receipt — 2026-08-11

This receipt closes the data-upload gate described in
[`re-verdict-and-upload-handoff.md`](re-verdict-and-upload-handoff.md). It does
not authorize dialing or any other prospect contact.

## Frozen input

- Corpus: `/tmp/lpm-reverdict-upload/corpus.csv`
- Corpus SHA-256: `45ec85c80d67318e9037c90a626a7289481a5b2a3e55afa92849627f642d47a7`
- Preparation report: `/tmp/lpm-reverdict-upload/prepare-report.json`
- Report SHA-256: `98b2345c5d6e444790e637e0cdeacc60b6a1570ee29ebeea8aac874ea152ff37`
- Rows: 1,084 unique uploader identities
- Verdicts: 429 `send`, 614 `reject`, 24 `review`, 17 `watch`

The accepted preflight dry run planned 1,021 POSTs and 63 PATCHes, with zero
duplicate targets, zero dropped records, and zero human-owned operational
fields in PATCH payloads.

## Live run

The first live attempt stopped on its first request with Airtable 422 because
the legacy discovery placeholder `Outreach Channel=Google Maps` is not a live
select choice. The receipt file was empty, proving that no write had completed.

The branch then added a narrow placeholder normalization and moved select
validation to the final write seam:

- `25e097d fix(airtable): normalize legacy outreach channels`
- `fe466e7 fix(airtable): validate final write payloads`

The full corpus select audit returned zero unsupported planned values. Focused
tests passed, and the authoritative suite passed 1,455 tests before retry.
Independent review closed the two write-seam findings before the second live
attempt.

The approved retry used the same frozen corpus and empty receipt file. It
completed 1,084 of 1,084 planned writes:

- POST receipts: 1,021
- PATCH receipts: 63
- Unique upsert keys: 1,084
- Unique Airtable record IDs: 1,084
- Receipt SHA-256: `96bc8c64970c3a91b32a8282995f9242a3a86efde801ea57eb0f404d5d2c0947`

## Independent live readback

A separate read-only pagination pass indexed live Airtable records by the
1,084 receipted record IDs and compared each live `Verdict` with the frozen
corpus:

- Missing record IDs: 0
- Verdict mismatches: 0
- Live verdicts: 429 `send`, 614 `reject`, 24 `review`, 17 `watch`

The Airtable data-upload gate is therefore passed.

## Remaining human gate

No queue was handed to a rep and no prospect was contacted. Dialing remains
blocked because the required durable decision and suppression ledger settings
were unset at final verification:

- `LPM_DIAL_DECISIONS_JSONL`
- `LPM_OUTCOMES_JSONL`
- `SUPPRESSION_SOURCE`

At least `LPM_DIAL_DECISIONS_JSONL` and one of `LPM_OUTCOMES_JSONL` or
`SUPPRESSION_SOURCE` must resolve to the intended durable ledgers before the
first dial.
