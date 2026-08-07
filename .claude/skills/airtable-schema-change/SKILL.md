---
name: airtable-schema-change
description: >-
  Change an Airtable field, select vocabulary, or table shape for local-lead-machine
  safely — preflight the live base, do the hand steps the API cannot do, backfill, and
  verify. Use whenever the work adds/removes a field, adds a value to a single-select
  (Status, Vertical, Lead Priority, Packet Tier, Offer Fit, Site Status…), registers a
  new vertical slug, deletes retired columns, or backfills a column across the base.
  The API cannot add a choice to an existing select, so half of every such change is
  hand work in the UI — this skill keeps the halves together and in the right order.
disable-model-invocation: true
---

# Airtable schema change

Base of record: **Local Lead Machine CRM** (`appLbCDdUDSZf7qaK`, `AIRTABLE_BASE_ID`). The
only base this repo writes to. *Local Pro Marketing* exists in the workspace and is **not**
part of the pipeline.

## The one fact that shapes everything

**The Airtable API cannot add a choice to an existing single-select.** Verified 2026-07-30:
`PATCH .../fields/{id}` accepts `description` but 422s on `options.choices` — five body shapes
tried. And `typecast: true`, which *would* mint choices, is banned repo-wide (it is how
`Lead Priority` acquired an option named `(440) 537-2459`); an AST scan in
`tests/test_mark_outcome.py` fails the suite if any module reintroduces it.

So: **adding a value to a select is half a change.** The code half
(`mark_outcome.REQUIRED_SELECT_CHOICES`, `queue_state.PROSPECT_STATUS_CHOICES`, a new vertical
slug) is worthless without a human editing the base. Ship them together or the write path 422s
in production while the test suite stays green.

## Two sources of truth, kept together

| Authority | What it states |
|---|---|
| `upload_airtable._FIELD_SCHEMA` | Every field the pipeline may write. **The** authority. |
| `docs/airtable_schema_required.md` | Human-readable mirror of the above + select vocabularies + tables/views. |

Update both in the same commit. In that doc, the `Exists` column has three states, not two:
`yes (date)` = observed live on that date, `?` = **nobody has checked**. Never promote `?` to
`yes` without querying the base — an unchecked field and a present field are different states,
and the column exists to keep them different.

## Order of operations

Deletion-before-expansion is how you strand data. The 2026-07-30 cutover ran this order and it
is the one to repeat:

1. **Deploy code, freeze writes.** No pipeline runs mid-change.
2. **Snapshot.** `airtable_schema_cutover.py prepare` — GET-only; writes the snapshot, backup
   and deletion checklist.
3. **Expand before you delete.** Add every new field/choice first, while the old ones still hold
   the data.
4. **Reconcile values before narrowing choices.** A select cannot drop a choice that rows still
   use. Migrate the rows, then remove the option.
5. **Delete retired fields** (hand work in the UI).
6. **Verify** before unfreezing writes.

## Run these three, in this order

```bash
# 1. Does the live base offer every select choice the code writes?
#    exit 0 = complete. exit 2 = names the missing choice AND prints the UI steps.
.venv/bin/python airtable_required_choices.py check

# 2. Does the base match _FIELD_SCHEMA? (read-only)
.venv/bin/python upload_airtable.py --schema-report

# 3. Are the retired fields gone, and is the record count unchanged?
.venv/bin/python airtable_schema_cutover.py verify <snapshot.json>
```

All three are read-only. A non-zero exit means **the base has fallen behind the code and a
write path is about to 422** — that is the whole signal. `prepare`/`verify` are GET-only; the
mutation between them is yours, by hand.

## The hand steps the API cannot do

Do these in the Airtable UI, and record them in the runbook as you go:

- **Add a choice to an existing select.** `airtable_required_choices.py check` prints the exact
  steps for each missing one.
- **Remove a choice.** Only after no row uses it.
- **Delete a field.** Use the checklist `prepare` wrote.
- **Change field precision.** `Reviews` is Integer; `Rating` is 2 decimal places.

## Special case: registering a new vertical

Order is inverted from intuition. **Add the `Vertical` choice to the base *before* adding the
profile in `lead_machine/verticals.py`.** Registering a profile can relabel existing leads
(`tests/test_geo_and_vertical_resolution.py::RegistryGrowthReclassifiesLeadsTest`), so the
choice must already exist or the relabelling write 422s.

Live `Vertical` choices are twelve; `verticals.py` registers **seven** profiles (hardscaping,
detailing, water_restoration, garage_doors, plumbing, epoxy, roofing) — those seven are the
only slugs the pipeline writes. The other five are live-only leftovers, not a bug.

For the full vertical workflow use the `add-vertical` skill; come here only for the base-side
half.

## Backfilling an existing column

- Backfills **skip** bad rows and report them; they never abort the run partway and leave the
  base half-written.
- Single-record `GET` does **not** accept a `fields[]` parameter — that is the list endpoint
  only. Passing it 422s. (Cost an hour on 2026-08-02.)
- Dry-run first. `upload_airtable.py --dry-run` prints planned writes without sending; `--live`
  sends them.
- Count the PATCHes before and after. The 2026-08-01 audit found 13 unwanted PATCHes precisely
  because someone counted.

## Verify before unfreezing

```bash
.venv/bin/python -m unittest discover -s tests          # ~964 tests, ~22s
.venv/bin/python airtable_required_choices.py check      # exit 0
.venv/bin/python upload_airtable.py --schema-report
.venv/bin/python airtable_schema_cutover.py verify <snapshot.json>
```

If the change touches a field any verdict reads, also run the reproducibility eval — it
re-derives every verdict from stored evidence and diffs it against the base:

```bash
.venv/bin/python evals/reproducibility.py --output-dir "$LPM_SCORED_OUTPUT_DIR"
```

Capture the change in a dated runbook under `docs/`, in the style of
`docs/airtable_cutover_runbook_2026-07-30.md`: what was done, what was hand work, what the
verification printed. The receipt is what makes the next change cheap.

## Common mistakes

| Mistake | What actually happens |
|---|---|
| Add a select value in code only | Green tests, 422 in production on that write path |
| Reach for `typecast: true` to avoid the hand step | AST scan fails the suite; if it ever shipped, it mints junk choices |
| Delete the old field before the new one holds data | Data stranded; the snapshot is your only way back |
| Narrow a select while rows still use the choice | Airtable refuses; or the rows silently lose the value |
| Promote a `?` to `yes (date)` in the doc without querying | The doc now asserts something nobody checked |
| Register a vertical profile before adding the base choice | Relabelling write 422s mid-run |
| `fields[]` on a single-record GET | 422 |
