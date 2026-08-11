# Handoff: re-verdict the corpus, then upload the 429

Written 2026-08-11. Everything below was measured against the real corpus and the
live Airtable base via dry run. **Nothing has been written to Airtable.**

The goal: get the 429 currently-sendable leads into Airtable carrying
`Verdict=send`, so `query_ready_to_send.py` serves them and the rep can dial
best-first. Lou has decided on the **full 429**, not a ranked slice.

---

## 1. Where things stand

`strip/commercial-tribunal` is merged to `main` and both are pushed
(`main` at `5cca4b3`). Suite 1,420 green via
`.venv/bin/python -m unittest discover -s tests` (bare `python -m unittest`
discovers zero tests). Two independent reviews — compliance and invariant — are
closed with no violations and a "safe to send behind" verdict.

The branch demoted the commercial tribunal and nine judgement gates to a
deterministic rank, which takes the sendable pool from **82 to 429 distinct
leads**. That change lives entirely in code. It has not reached any stored data.

---

## 2. The blocker

**`apply_verdict` has no production caller.** It is defined in
`lead_machine/verdict.py:97`, exported at `:51`, and referenced nowhere outside
`tests/test_verdict.py`. `refresh_packet_tiers.py` re-derives the four
`PACKET_FIELDS` but never touches `Verdict`.

So nothing in the repo propagates a changed gate to already-scored CSVs.

This bites at upload because of how `upload_airtable.py` treats a stored
verdict. `is_sendable_record` (`upload_airtable.py:348`) re-evaluates only when
the cell is empty:

```python
if not clean_cell(fields.get("Verdict")) and not evaluate_sendability({**record, **fields})["sendable"]:
    return False
```

A row with a non-empty `Verdict` is written through as-is. Measured on the
consolidated corpus:

| | count |
|---|---|
| stored `Verdict=send` in the CSVs | 96 |
| sendable under the **current** gate | 429 |
| sendable now, but stored as something else or blank | 350 |
| what a dry run actually plans to write as `send` | **113** |

113, not 429. The rows with an empty verdict get re-derived; the ones carrying a
stale `reject`/`review`/`watch` keep it. **Uploading as-is would leave the strip
invisible in the base and put the rep back on the old cohort.**

---

## 3. What to build

A step that applies the current gate to a scored CSV and rewrites the verdict
columns. `refresh_packet_tiers.py` is the shape to copy — same CLI ergonomics,
same in-place-with-temp-file write, same "no LLM calls" guarantee.

Use the existing tested functions. Do not reimplement the gate:

- `lead_machine.sendability.apply_sendability(row)`
- `lead_machine.verdict.apply_verdict(row)` — writes both `Verdict` and
  `Verdict Reasons` (see `tests/test_verdict.py:222`)

### Acceptance criteria

1. Running it over the consolidated corpus yields **429** rows with
   `Verdict=send`, and `evaluate_sendability(row)["sendable"]` agrees with
   `row["Verdict"] == "send"` for **every** row. That biconditional is the test.
2. `Packet Tier` stays consistent with the new verdict —
   `sendability.VERDICT_PACKET_TIERS` is the mapping, and
   `tests/test_verdict.py` already pins the bijection. A row that becomes `send`
   must not keep a `No Packet` tier.
3. It must be idempotent: running twice changes nothing the second time.
4. It must not touch human-owned operational columns. `Status`,
   `Last Contacted`, `Next Follow-Up`, `Assigned Setter` and
   `Outreach Channel` are owned by the rep the moment they touch the lead —
   `upload_airtable.enforce_operational_field_ownership` exists because copying
   a machine-written `Status: "New"` through a PATCH once reverted a dialled
   lead to `New`, put it back in `queue_state.QUEUE_FILTER_FORMULA`, and got a
   business contacted twice.
5. Tests written before the implementation, in the repo's style: name the
   behaviour and the failure it prevents, not the method.

### Watch out

The verdict is a **snapshot**, and this is the branch that spent five commits
learning what that means. Whatever you write, the biconditional in (1) is the
only thing that proves the stored column and the live gate agree.

---

## 4. Second blocker: the `Vertical` single-select

186 field values are dropped by Airtable during the dry run because the
`Vertical` single-select has no matching choice:

| value | records | **of which sendable** |
|---|---|---|
| `service` | 169 | **109** |
| `Landscaping / Hardscaping` | 10 | 0 |
| `Masonry / Hardscaping` | 7 | 1 |

`service` is not a real vertical — it is the fallback label when
`resolve_profile` matches nothing (`lead_machine/verticals.py:174`). It affects
**109 of the 429**, a quarter of the cohort. Uploading with it dropped means
those leads land with a blank `Vertical`, which:

- puts them in the `"unknown"` bucket in `hermes_queue.per_vertical_counts`
- breaks the per-vertical deficit logic that drives `/queue-topup`

Decide before uploading whether to (a) resolve `service` to a real vertical for
those 169 rows, or (b) add the choices to Airtable. (a) is better — a fallback
label is a scoring gap, not a vocabulary gap. Note the corpus also has casing
duplicates (`Hardscaping`/`hardscaping`, `Roofing`/`roofing`,
`Detailing`/`detailing`) that the base currently tolerates; worth normalising in
the same pass.

Adding a select choice is a schema change: see the "half a change" rule in
AGENTS.md and `upload_airtable.assert_required_choices_ready`.

---

## 5. Two env vars are unset, and one silently voids the experiment

Checked live on this machine:

- **`LPM_DIAL_DECISIONS_JSONL` — unset.** `mark_sent.py:246` warns and skips the
  ledger write. Every dial goes unrecorded, `evals/pre_dial_approval.py` reports
  `no-decisions` forever, and the rank falsifier never gets data. **The entire
  point of the cohort — measuring whether the strip cost anything — is void
  until this is set.** Set it before the first dial, not after.
- **`LPM_OUTCOMES_JSONL` — unset.** Sends are unaffected: `cmd_mark_sent` passes
  `--suppression-source` from `morning_queue.json.outcomes_jsonl`
  (`/home/lucascoh/operator-agent/STATE/outcomes.jsonl`). But `mark_declined.py`
  reads env-only, so its "this record is already suppressed" note can never
  fire. It now emits a `[WARN]` rather than failing open in silence.

---

## 6. The upload procedure, and four traps already hit

`upload_airtable.py --input` takes **one** file, and the 429 are the latest
version of each lead spread across 29 CSVs. A consolidated file is safer than 29
sequential live runs, which would write 3,048 rows and leave the base holding
stale verdicts if a run failed midway.

A verified consolidation script already ran; reproduce it rather than trusting
this description. Sources: `output/scored_*.csv` + `output/airtable_*.csv`,
walked in **mtime order** so the newest row for each lead wins.

**Trap 1 — dedupe by the uploader's upsert key, not your own identity.**
The key is `place:<Place Key>` if present, else `phone:<normalize_phone(Phone)>`
(`upload_airtable.py:658-663`). Deduping by anything else produces
`[ERROR] Duplicate Airtable upsert key in input`. One real collision exists:
*Outside Solutions LLC* appears under both `Macedonia` and `Cleveland` with no
Place Key and the same phone. Both unscored, so the 429 is unaffected.

**Trap 2 — one row has no upsert key at all.** *Agresta Landscaping Inc*
(Strongsville) has blank `Place Key` **and** blank `Phone`.
`csv_row_to_record` raises `ValueError`, and `read_csv_records` does not catch
it, so it aborts the entire run. Exclude it. It is not sendable (no phone → not
dialable), so the cohort is intact at 429.

**Trap 3 — packet paths cannot resolve across 29 batches.** With no
`--packet-dir`, inference falls back to `packets/`, whose `packet_index.csv`
carries a stale entry pointing at `packets_manual_existing_manual/...` when the
batch was archived to `runs/packets_manual_existing_manual/...`. That is a
genuine pre-existing defect and it fails the run closed. Two options: fix the
index entry, or point `--packet-dir` at an empty directory. The latter is safe
for a corpus-wide upload — `Packet Path` is only written when it resolves
(`upload_airtable.py:654-656`), so an unresolved path is simply absent from the
PATCH and **existing Airtable values are preserved**.

**Trap 4 — prove the consolidation is lossless.** Do not assume it. Compare
`csv_row_to_record(row_from_consolidated)` against
`csv_row_to_record(row_from_original_source)` field-for-field for every lead.
The verified run reported 1,091 identities preserved and **zero** records
differing from their source. This branch produced four falsely-clean results by
feeding code a different row shape than production uses; this check is what
catches that.

### Last measured dry run (before re-verdicting)

`--dry-run`, exit 0, 1,089 records: **1,021 POST / 68 PATCH**; verdicts 936
reject, 113 send, 35 review, 5 watch. Note the base currently matches only 68 of
these leads, so this is largely an insert of ~1,021 new rows. Re-run the dry run
after re-verdicting and expect `send` to move 113 → 429.

---

## 7. Before going live

- Re-run the dry run and read the plan. `--live` is the only writing flag;
  `--dry-run` and `--live` are mutually exclusive.
- `--receipts` + `--resume` exist so a partially-failed run can be continued
  without re-writing what landed. Use them.
- **Gate 0 stands**: nothing may transmit to a business without a human in the
  loop. Uploading and dialing are fine; autonomous sending is not.
- **`/mark-declined` has never touched the live base.** Its first run should be
  a single lead you do not mind retiring, watched — not a batch. It PATCHes
  `Status=New → Not Fit` and writes one ledger line.
- The `dial_rank` weights (35/25/15/10/10/5) are **invented and unvalidated
  against any close**. Dialing this cohort best-first is a real experiment on an
  unvalidated ordering. That is fine and deliberate, but it is what is
  happening — the ranking only becomes falsifiable after ~20 decisions with rank
  coverage on both sides, which requires §5's first env var.
- Recording-consent capture is still absent repo-wide. It predates this branch
  and neither review closed it.

---

## 8. Known-good facts to check yourself

Trust none of these on sight; each is cheap to reproduce and this document will
age.

- corpus: 3,048 rows across 29 CSVs → **1,091 distinct leads** (132 rows carry
  no `Place Key`)
- pre-strip sendable **82**, post-strip **429**, refused 1,009 → 662
- `hold-commercial-case` on **908** of 1,091 (83%); the *blocker string*
  `Contact eligibility is not contact-now` fires on 956 = 908 + 48
  `contact-after-asset`. Quote 908 for the state, 956 for the blocker.
- `scoring_v3` resolves a profile for **71 of 429**; the other 358 get no score.
  Counted by calling `score_v3`, not by deriving from
  `supported_scoring_cells`, which gives 41 and is wrong.
