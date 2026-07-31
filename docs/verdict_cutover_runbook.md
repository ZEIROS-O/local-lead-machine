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

## Step 2 — Merge and backfill — done 2026-07-31

- [x] Backed up first — 145 records + schema to
      `~/lead-machine-backups/2026-07-31-verdict-cutover/` and
      `run_state/backups/2026-07-31-verdict-cutover/`, plus the seven-column
      `prepare` artifact (`safe_to_delete: true`) in both
- [x] Merged (fast-forward, 23 commits); 955 tests green on `main`
- [x] Backfilled — 124 of 145 rows patched, receipts in the backup dir
- [x] `evals/reproducibility.py` — `verdict_drift: 0`, `snapshot_drift: 0`

### The queue went empty between the merge and the backfill

`query_ready_to_send.py` returned `[]` the moment the merge landed, because the
filter reads `Verdict` and no row had one yet. That is the outage the step
ordering exists to keep short, and it is over — the query returns its 12 leads
again.

### "Re-run the upload" was the wrong instruction — do not do it

Measured against the live base before writing anything, a full corpus re-upload
(31 files, 1297 rows) would have done two things nobody asked for:

- **POST 1077 new rows**, taking `Prospects` from 145 to ~1222. The corpus holds
  666 distinct leads; only 145 were ever promoted into the base. Importing the
  rest is a business decision, not a backfill.
- **Revert 32 rows from `Manual Review` to `New`.** `Status` and
  `Outreach Channel` are in `OPERATIONAL_FIELDS`, which are written from the CSV
  and *not* marked defaulted, so a stale CSV overwrites the base's newer value.

So the backfill was narrowed to write **only `Verdict` and `Verdict Reasons`, and
only to rows that already exist**: `run_state/verdict-cutover/backfill_verdict.py`
(`--live` to write; dry-run otherwise). 124 of 145 rows,
`review 85 · reject 25 · watch 2 · send 12`.

### The acceptance test passed, on real data, twice

Predicted from the backup before writing, then re-run against the live base
after the backfill — same answer both times:

```
old queue: 12 | live queue: 12
IDENTICAL SET: True
lost: none
gained: none
```

The 36 rows carrying a stale `Sendable Packet` beside `hold-commercial-case` do
**not** become sends — they recompute to `review`. The one-clause filter is safe
because the verdicts are recomputed from evidence, not read off the stale tier.

### Known, and deliberately left

- **21 rows get no `Verdict`** (19 already had a blank tier). None are in the
  queue today, so the queue is unaffected; they stay invisible until rescored.
- **8 rows are scored in more than one CSV with disagreeing verdicts**
  (`reject` vs `review`, `review` vs `watch`). None is a `send`, so the queue is
  unaffected. Freshest file by mtime wins, which is what makes the run
  deterministic rather than file-order dependent.
- `stale_evidence: 80` stays non-zero — those rows predate schema v2 and cannot
  be re-derived from their own evidence. Real debt; a rescore is its remedy, not
  this backfill.

## Step 3 — Rebuild the views (hand step — the API cannot do this)

**This one has to be done in the Airtable UI.** View filters are not writable
through the REST API or the MCP server: fields, records, tables and automations
all are, views are not. Steps 1 and 2 were automatable; this is not.

`READINESS_CLAUSES` is now one clause, so the queue filter is:

```
AND({Verdict}='send',OR({Status}='New',{Status}='Mockup Needed'))
```

The two clauses this drops — `Commercial Fit State` and `Contact Eligibility` —
are not lost. `send` is unreachable unless both hold, because both are blockers
inside the same admission; `tests/test_verdict.py` and
`tests/test_queue_state_formula.py` pin that implication. The shorter filter
selects the same leads, and step 2 confirmed that against live data.

The four rep views, and the one that matters:

| View | ID | Change |
|---|---|---|
| 🔥 Call Queue | `viwTIS6NX47jK412Z` | rebuild on the formula above |
| 📅 Follow-Ups Due | `viwT8IoxEWyCOMA5M` | repoint off `Packet Tier` onto `Verdict` |
| 📊 Pipeline | `viwb3nZQ3lW0aIQBD` | repoint off `Packet Tier` onto `Verdict` |
| 🧪 Manual Review | `viw41G0duU8rwglL0` | `{Verdict}='review'` |

- [ ] 🔥 Call Queue rebuilt on the formula above
- [ ] The other three rep views repointed off `Packet Tier` onto `Verdict`
- [ ] **Call Queue returns the identical set `query_ready_to_send.py` returns** —
      the same records, not merely the same count. This was the 2026-07-30
      acceptance test and it is the one that matters. The 12 to expect today:
      9 garage-door and 3 plumbing leads, all `Status = New`.

## Step 4 — Delete the seven superseded columns (hand step)

Only after steps 1–3 verify. Delete from `Prospects`:

`Packet Tier` · `Commercial Fit State` · `Contact Eligibility` · `Asset State` ·
`Promise State` · `Send Blockers` · `Skip Reason`

The code no longer requests any of them: `query_ready_to_send.DISPLAY_FIELDS`
dropped all seven in the same commit that added `Verdict`, because a `fields[]`
naming a deleted field is a 422 rather than a blank.

`Score Total` and `Lead Priority` **survive as display only.** They are not
authoritative and must not appear in any view filter.

Both gates were repointed off the tier ahead of this step (commit `dc3bbae`):
`evals/reproducibility.py` and `evals/completeness.py` named `Packet Tier` as
the base column they read, and a `fields[]` naming a deleted field is a 422, so
the daily workflow would have gone red the moment the column went away.
`reproducibility` also recomputed *into* tier vocabulary — it answers in
verdicts now, with the live numbers unchanged (`matched 35`,
`verdict_drift 0`). `completeness` needed the two sides split: the scored CSVs
on disk still carry `Packet Tier` and have no `Verdict` column, so its
corpus-conflict check still reads the tier.

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
