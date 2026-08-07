# Verdict Cutover Runbook

Status: **COMPLETE — all four steps executed 2026-07-31.**

Nine judgment surfaces are one machine-owned `Verdict`. The base went 50 → 43
fields, the Call Queue filter went from four clauses to two, and the queue
returns the identical 12 leads it returned before any of it — the acceptance
test that mattered, run against live data rather than argued.

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
AND({Verdict}='send',{Status}='New')
```

The two clauses this drops — `Commercial Fit State` and `Contact Eligibility` —
are not lost. `send` is unreachable unless both hold, because both are blockers
inside the same admission; `tests/test_verdict.py` and
`tests/test_queue_state_formula.py` pin that implication. The shorter filter
selects the same leads, and step 2 confirmed that against live data.

**Only two of the four views change.** Checked against their definitions in the
[2026-07-30 runbook](airtable_cutover_runbook_2026-07-30.md#6-build-the-rep-facing-views--done):
`📅 Follow-Ups Due` filters on `Next Follow-Up` + `Status`, and `📊 Pipeline` is
a kanban grouped by `Status`. Neither mentions a readiness column, so neither
needs touching.

**🔥 Call Queue** (`viwTIS6NX47jK412Z`) — four conditions become two:

| Was | Now |
|---|---|
| `Packet Tier` is `Sendable Packet` | **`Verdict` is `send`** |
| `Commercial Fit State` is `commercially-sendable` | *delete this condition* |
| `Contact Eligibility` is `contact-now` | *delete this condition* |
| `Status` is `New` | **keep `New`; remove `Mockup Needed` if still present** |

Sort (`Lead Priority` asc → `Opportunity Score` desc → `Score Total` desc) and
grouping (`Vertical`) are unchanged.

**🧪 Manual Review** (`viw41G0duU8rwglL0`) — one condition:

| Was | Now |
|---|---|
| `Packet Tier` is `Manual Review Packet` | **`Verdict` is `review`** |

Sort (`Opportunity Score` desc → `Score Total` desc) unchanged.

Hidden fields: the seven deleted columns needed no action — already hidden from
every rep-facing view, and gone outright in step 4. The two *new* columns did,
because a field created through the API lands in every view. `Verdict` was
hidden from both rep views (it is a constant inside a view filtered on it) and
`Verdict Reasons` hidden from Call Queue but kept in Manual Review, where it is
the only column saying *why* a lead is sitting there. Populated on 38 of the 85.

- [x] 🔥 Call Queue: tier condition swapped for `Verdict = send`, the two
      now-redundant ones deleted
- [x] 🧪 Manual Review: tier condition swapped for `Verdict = review`
- [x] **Call Queue returns the identical set `query_ready_to_send.py` returns** —
      12 records, 9 garage-door and 3 plumbing, all `Status = New`
- [x] Manual Review shows **85** (`Verdict = review`), up from 39 — the collapse
      folded `Watchlist`-era leads and the stale `Sendable Packet` rows into
      `review`, which is where they belong

## Step 4 — Delete the seven superseded columns — done 2026-07-31

Deleted from `Prospects`:

`Packet Tier` · `Commercial Fit State` · `Contact Eligibility` · `Asset State` ·
`Promise State` · `Send Blockers` · `Skip Reason`

### The "code no longer requests any of them" claim was half true

It held for the **read** path — `query_ready_to_send.DISPLAY_FIELDS` dropped all
seven alongside adding `Verdict`. It was false for the **write** path, which
still put five of them in every payload. A payload naming a field the base does
not have is a 422 for the whole record, so deleting first would have broken
every upload: the same failure step 1's ordering avoids, inverted.

Four registries had to move together (commit `b23c470`), not one:

| Registry | Why it mattered |
|---|---|
| `AIRTABLE_ALLOWED_FIELDS` | the chokepoint every payload passes through |
| `WRITE_PATH_SELECT_CHOICES` | drives `airtable_required_choices`, which **refuses on a field the base lacks** — the five readiness selects would have failed the daily gate over columns nobody writes |
| `_FIELD_SCHEMA` | the list of columns the base is required to have |
| the `validate_airtable_fields` call | narrowed to fields a payload can actually carry |

`CSV_AIRTABLE_FIELDS` deliberately **keeps** `Packet Tier`: it is the CSV-side
contract, and the verdict is still derived from the tier in-process. Only the
tier's route to Airtable is closed, not its use.

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

### The last code item was wrong — the path is not dead, and stays

This runbook said to remove the reconstruct-from-tier path in
`sendability.readiness_flags` once the tier column was gone. **Do not.** It was
written speculatively and is the opposite of true. After the deletion a base row
carries `Verdict` and none of `Packet Tier`, `Sendable`, `Packet Eligible` — so
the verdict maps back to a tier and the tier derives the booleans. That branch
is now the *only* thing that resolves a row read back from Airtable:

```
post-cutover base row (send):   ('Yes', 'Yes')
post-cutover base row (review): ('No', 'Yes')
```

Remove it and every base row silently becomes `("", "")` — not sendable — and
`is_contact_ready` starts refusing the leads the queue is built on. Pinned by
`test_a_verdict_only_row_is_what_the_base_returns_now` so the next reader who
finds it looking dead has the counter-example in front of them.

- [x] Seven columns deleted
- [x] Prospects field count dropped 50 → 43
- [x] `query_ready_to_send.py` returns the same 12 leads as before the cutover
- [x] Gates exit 0 — required-choices over 12 fields, `verdict_drift 0`,
      `snapshot_drift 0`, `matched 35`
- [x] 961 tests green
- [x] Receipt written to
      [`docs/verdict_cutover_receipt_2026-07-31.json`](verdict_cutover_receipt_2026-07-31.json)

## Debt this left behind, on purpose

Neither is a backfill problem; both want a rescore:

- **21 rows carry no `Verdict`** — never rescored, so nothing to derive one
  from. 19 already had a blank tier. None are queue-eligible, so the queue is
  unaffected; they are simply invisible until rescored.
- **80 rows are `stale_evidence`** — scored before schema v2 and not derivable
  from their own evidence, so the eval reports `ok: false` until they are
  rescored. Real debt, correctly flagged, not a false alarm.

The 551 leads sitting in the scored corpus but not the base were **not**
imported, and should not be. 12 of the 31 corpus files are debug or superseded
artifacts (`scored_debug_*`, `*.before_131_fix.csv`, `*_tiered` re-scores of
batches already in the base). "Upload every scored lead, not just the sendable
ones" (`39160cc`) is a rule about not filtering *within a batch you run* — it
was never a mandate to retro-import every CSV on disk.

## What stays

- **`scoring_v3`** — deferred by decision. It gates paid DataForSEO enrichment
  spend via `Scoring V3 Rank`/`Disposition`, not sends, and nothing here touches
  it.
- **The `Sendable` admission invariant** — `Verdict` does not replace it. Both
  are now AST-enforced: exactly one function may spell each.
