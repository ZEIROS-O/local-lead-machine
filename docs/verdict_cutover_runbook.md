# Verdict Cutover Runbook

Status: **STEP 1 DONE — the two fields exist; the branch is clear to merge.**

Base: `appLbCDdUDSZf7qaK` (`Local Lead Machine CRM`)

Nine Airtable surfaces answered one question — *should we contact this lead* —
across five parallel reason columns. `Verdict` is now the one machine-owned
answer, and `lead_machine/verdict.py` derives it from the same
`evaluate_sendability` + `evaluate_packet_tier` that already decided it. This
runbook brings the base into line.

Reuses the shape of the [2026-07-30 cutover](airtable_cutover_runbook_2026-07-30.md),
which worked: checklist → validation → backup → execute → receipt. Write each
step body in past tense as it completes; the "blocked, hand step" drift in that
runbook came from doing it the other way.

## Ordering, and why it is not the usual one

The repo's standing rule is *code leads, the base follows* — `queue_state.py`
says so about `Won`/`Lost`, where a write 422s until a human creates the choice.
**That rule is inverted here, deliberately.** `Won` is written by `mark_outcome`
on a closed deal, so a 422 costs one command. `Verdict` is written on **every
upload**, so merging this branch before the field exists breaks every upload and
empties the morning queue.

So: **step 1 happens before this branch merges.** Steps 2–4 follow the merge.

## Step 0 — Verify the collapse changed no decision

Already run on this branch, and the gate the whole step rests on:

```
.venv/bin/python evals/reproducibility.py --output-dir output \
    --snapshot-out /tmp/verdicts-before.json
# ... apply the collapse ...
.venv/bin/python evals/reproducibility.py --output-dir output \
    --baseline /tmp/verdicts-before.json
```

Result on this branch: **`snapshot_drift: 0`**, verdict sets byte-identical
before and after. If that diff is ever non-empty, the collapse changed a
decision and must not ship.

- [x] `snapshot_drift = 0`
- [x] 955 tests green
- [x] `upload_airtable.py --schema-report` lists `Verdict` with 4 choices

## Step 1 — Create the two fields (BEFORE merging) — done 2026-07-31

Created in `Prospects`:

| Field | Type | Field ID | Choices |
|---|---|---|---|
| `Verdict` | Single select | `fldKQrryUKS29zMH9` | `send`, `review`, `watch`, `reject` |
| `Verdict Reasons` | Long text | `fldq0ua0VeSoIOFb0` | — |

The choices are exactly what `upload_airtable.py --schema-report` prints. The
message this step existed to clear:

```
[airtable-required-choices] REFUSED: expected one field named/id 'Verdict'
in table 'Prospects', found 0
```

The plan's open question about the Create Field API is now answered: **creating
a single select with its choices works.** All four landed in one call, which the
**Update** Field API could not have done afterwards
(`airtable_required_choices.py:20-37`). Verified by reading back
`live_choice_count: 4` rather than by trusting the create response.

Choice order is decision order, not alphabetical — the gate checks presence, not
sequence, so the rep-facing dropdown reads `send → review → watch → reject`.

- [x] `Verdict` created with all four choices
- [x] `Verdict Reasons` created
- [x] `.venv/bin/python airtable_required_choices.py check` exits **0** — all 17
      select fields OK, `missing: []`

## Step 2 — Merge and backfill

Merging without step 1 breaks every upload. With step 1 done:

- [ ] Merge this branch
- [ ] Back up the base first — `airtable_schema_cutover.py` snapshot, held in
      more than one place, as 2026-07-30 did
- [ ] Backfill `Verdict` across all rows by re-running the upload
- [ ] `.venv/bin/python evals/reproducibility.py --output-dir output` — the
      `verdict_drift` count must be **0**

`stale_evidence` is expected to be non-zero until the base is re-uploaded in
full: 80 rows today were scored before schema v2 and cannot be re-derived from
their own evidence. That is real debt, not a false alarm, and the total upload
is its remedy.

## Step 3 — Rebuild the views

`READINESS_CLAUSES` is now one clause, so the queue filter is:

```
AND({Verdict}='send',OR({Status}='New',{Status}='Mockup Needed'))
```

The two clauses this drops — `Commercial Fit State` and `Contact Eligibility` —
are not lost. `send` is unreachable unless both hold, because both are blockers
inside the same admission; `tests/test_verdict.py` and
`tests/test_queue_state_formula.py` pin that implication. The shorter filter
selects the same leads.

- [ ] 🔥 Call Queue rebuilt on the formula above
- [ ] The other three rep views repointed off `Packet Tier` onto `Verdict`
- [ ] **Call Queue returns the identical set `query_ready_to_send.py` returns** —
      the same records, not merely the same count. This was the 2026-07-30
      acceptance test and it is the one that matters.

## Step 4 — Delete the seven superseded columns (hand step)

Only after steps 1–3 verify. Delete from `Prospects`:

`Packet Tier` · `Commercial Fit State` · `Contact Eligibility` · `Asset State` ·
`Promise State` · `Send Blockers` · `Skip Reason`

The code no longer requests any of them: `query_ready_to_send.DISPLAY_FIELDS`
dropped all seven in the same commit that added `Verdict`, because a `fields[]`
naming a deleted field is a 422 rather than a blank.

`Score Total` and `Lead Priority` **survive as display only.** They are not
authoritative and must not appear in any view filter.

Still to do in code before this step, and deliberately not done yet — it would
be dead code until the column is actually gone:

- [ ] Remove the reconstruct-from-tier path in `sendability.readiness_flags`.
      It exists because the base stored only the tier; `Verdict` replaces its
      reason for existing. The `Verdict`-to-tier branch beside it stays.

- [ ] Seven columns deleted
- [ ] Prospects field count drops by 7
- [ ] `query_ready_to_send.py` returns the same leads as before the cutover
- [ ] Both gates exit 0
- [ ] Receipt JSON written to `docs/`, as `airtable_cutover_receipt_2026-07-30.json`

## What stays

- **`scoring_v3`** — deferred by decision. It gates paid DataForSEO enrichment
  spend via `Scoring V3 Rank`/`Disposition`, not sends, and nothing here touches
  it.
- **The `Sendable` admission invariant** — `Verdict` does not replace it. Both
  are now AST-enforced: exactly one function may spell each.
