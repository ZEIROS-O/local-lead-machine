# Re-verdict Airtable upload receipt — 2026-08-12

Second re-verdict pass, closing the 71 verdict drifts the 2026-08-12 state review
found. Same form as [`re-verdict-upload-receipt_2026-08-11.md`](re-verdict-upload-receipt_2026-08-11.md).
It does not authorize dialing or any other prospect contact.

## What this corrected

`evals/reproducibility.py` reported 71 verdict drifts against the live base, all
schema v2 — current evidence disagreeing with the stored verdict, not staleness.
70 came from `output/scored_rescore_claimfix.csv`:

| base → recomputed | count |
|---|---|
| send → watch | 31 |
| send → reject | 25 |
| send → review | 14 |

All 70 moved *away* from `send`, so the base was serving leads the current gate
refuses. Cross-referenced before the write: **62 sat at `Status=New`** — served
by `queue_state.QUEUE_STATUS_CLAUSE`, which selects exactly `Status=New` +
`Verdict=send` — **5 at `Follow-Up`** and **3 at `Not Fit`**.

The 71st drift is `Ricks Garage Door Service` (`review → send`), which lives in
`scored_recovery_garage_doors_001_api_tiered.csv`, not the refreshed file. It
moves *toward* send and is deliberately out of scope here.

## Sequencing prerequisite

`refresh_verdicts.py` and `prepare_airtable_corpus.py` re-derive `Verdict` from
stored CSV rows and never re-enrich, so until `27519d5` they could rebuild an
opted-out lead to `send` — the 2026-08-11 condition, where 429 send verdicts were
written having never been checked against suppression at any stage. Both now load
the configured suppression index and fail closed before reading a row. This run
was made with `SUPPRESSION_SOURCE=run_state/suppression_events.jsonl`; the
prepared corpus reports `suppressed_rows: 0` against an empty ledger.

## Frozen input

Preserved under `run_state/airtable_receipts/reverdict_2026-08-12/`.

- Refreshed corpus: `output/scored_rescore_claimfix_reverdict_2026-08-12.csv`
  (written with `--output`, not `--in-place`, so the original stays as the diff
  baseline)
- Corpus SHA-256: `7acf969ecd25bf220977a10deeb918994f261f440501ab8e8df354ab6545049a`
- Preparation report SHA-256: `07371b5b5e09e1d39cb391cf73742997dff0e64b6ff7edc4489c726d3e22e587`
- Rows: 112 unique uploader identities
- Verdicts: 5 `send`, 60 `reject`, 32 `watch`, 15 `review`

The refresh changed machine-owned fields only — `Verdict`, `Verdict Reasons`,
`Packet Tier`, `Sendable`, `Minimum Evidence Passed`, `Send Blockers`,
`Sendability Blockers`, `Packet Eligible`, `Packet Eligibility Reason`,
`Manual Review Reason` — verified by diffing the refreshed file against its
baseline row-for-row.

## Accepted dry run

112 planned writes: **75 PATCH, 37 POST**, zero duplicate record ids, zero
duplicate upsert keys, zero dropped records.

- **`OPERATIONAL_FIELDS` audit: 0 of 75 PATCHes carried `Status`,
  `Last Contacted`, `Next Follow-Up`, `Outreach Channel` or the generated
  `Assigned Setter`.** The human-owned funnel state was untouched.
- All 70 drifted records were covered by a PATCH; 0 uncovered.
- PATCH verdicts: 25 `reject`, 31 `watch`, 14 `review` (the 70), plus 5 `send`
  that were already `send` and stayed there.
- **The 37 POSTs create no sendable lead**: 35 `reject`, 1 `watch`, 1 `review`.
  These are claimfix-corpus leads the base did not hold. They legitimately seed
  `Status`/`Outreach Channel`/`Assigned Setter` because they are new records.

Approved on that basis: nothing in this write makes any lead newly sendable.

## Live run

Ran against the frozen corpus with an empty receipt file. Completed **112 of 112**
planned writes on the first attempt, no 422s.

- POST receipts: 37
- PATCH receipts: 75
- Unique upsert keys: 112
- Unique Airtable record IDs: 112
- Receipt SHA-256: `cc739d5914ce40225ad8c4ad99c32a1f4a88e7e2d9dca2f442eb6e6e7b440682`

## Independent live readback

A separate read-only pagination pass re-read the base and compared each receipted
record's live `Verdict` against the frozen corpus. This is a different code path
from the writer, not a re-read of its own claim.

- Live base rows re-read: 499 (462 before, + the 37 new records)
- Missing record IDs: 0
- Verdict mismatches: 0
- Live verdicts across the 112: 5 `send`, 60 `reject`, 32 `watch`, 15 `review`
- Live `Status` across the 112: 103 `New`, 5 `Follow-Up`, 3 `Not Fit`, 1 `Manual Review`

## Post-conditions verified

- `evals/reproducibility.py`: **71 verdict drifts → 1** (the out-of-scope
  `Ricks Garage Door Service`). `snapshot_drift` 0, `stale_evidence` 1, unchanged.
- `query_ready_to_send.py --limit 500`: serves 324 leads, **0 of which are any of
  the 62** that were sitting at `Status=New` under a withdrawn `send`.

## Judgement call left open: the 8 already-worked leads

Not a data fix, and deliberately not automated. Five leads were **contacted**
under a verdict the gate has since withdrawn, and three were retired to
`Not Fit`. Their `Status`, `Last Contacted` and `Outreach Channel` were left
exactly as the rep set them:

| Business | Status | verdict was → now | Last Contacted | Record |
|---|---|---|---|---|
| Arrow Door | Follow-Up | send → watch | 2026-08-03 | `recgiRxlpNoeuMrsZ` |
| B and A Garagedoor Services LLC | Follow-Up | send → reject | 2026-08-03 | `recoQ4JKFZRypTHDD` |
| Door Mechanics | Follow-Up | send → reject | — | `recle6Wgwfr4WoLT7` |
| Expert Door LLC | Follow-Up | send → watch | 2026-08-03 | `rec4ZUJvfJzWvEksx` |
| MASON GARAGE DOORS | Follow-Up | send → watch | 2026-08-03 | `recbZjyM1vCkFc4st` |
| Brown Overhead Door LLC | Not Fit | send → watch | 2026-08-03 | `recZ2opfFv2d6xSt5` |
| Pro Door Sales & Services | Not Fit | send → watch | 2026-08-03 | `recZ7uR7eEju9AeVQ` |
| Proline Garage Door Repair Cincinnati | Not Fit | send → watch | — | `recvNF5Hvrv1OQMEL` |

The two `send → reject` Follow-Ups are the sharpest: the current gate says those
should never have been dialed. Whether to continue the follow-up cadence on any
of these is a call about the relationship, not about the data.

## Remaining human gate

No queue was handed to a rep and no prospect was contacted. The dial ledgers were
supplied to this run through the environment; they are not yet in `.env`, so
**dialing remains blocked until `SUPPRESSION_SOURCE` and
`LPM_DIAL_DECISIONS_JSONL` are set there** — see the 2026-08-12 batch-1 handoff.
