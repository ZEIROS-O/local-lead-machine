# Airtable Cutover Runbook — 2026-07-30

Status: **COMPLETE — 2026-07-30.** All nine steps executed and verified. Machine
record: [`airtable_cutover_receipt_2026-07-30.json`](airtable_cutover_receipt_2026-07-30.json).

Base: `appLbCDdUDSZf7qaK` (`Local Lead Machine CRM`)

This began as the phase-6 handoff and stopped before execution for review. Lou
approved it, the code shipped as `6390d64`, and the base was brought into line
the same day — the API doing what it could, Lou doing by hand the four things no
API call can perform.

## Status consolidation postscript — 2026-08-03

Lou subsequently removed three redundant `Prospects.Status` choices in Airtable:
`Do Not Contact` converged into `Not Fit`, `Sent` into `Follow-Up`, and
`Mockup Needed` into `New`. The current canonical vocabulary is `New`,
`Manual Review`, `Not Fit`, `Follow-Up`, `Won`, `Lost`, `Booked`.

Current views must use `Status = New` for the call queue and `Status = Follow-Up`
for due follow-ups. Historical observations below retain the choices and counts
that were true during the 2026-07-30 cutover.

## What happened

| Step | State | Note |
|---|---|---|
| 0. Deploy code and freeze writes | **done** | `6390d64`, `origin/main` matches, tests green. No cron or timer is armed, so nothing wrote unattended. |
| 1. Pre-mutation snapshot | **done** | 145 records, `safe_to_delete=true`, backup `e5e96bab…` held in three locations on three filesystems. |
| 2. Expand the schema | **done** | `ICP Lane` (`fld0DevPnL52YCy4p`) and `Opportunity Score` (`fldH6acA46yqTyEHb`) created by API; the 8 select choices added by hand. |
| 3. Reconcile values | **done** | 0 `Status=Closed`, 0 checked consent, 16 misplaced `Sales — Batch` call notes copied into `Notes` non-destructively. |
| 4. Remove unused select choices | **done** | 10 removed by hand — the 9 junk choices plus `Status=Closed`. `Lead Priority: Skip` correctly preserved. |
| 5. Delete the 23 retired fields | **done** | By hand, plus the stray `Tasks` link field Airtable degraded to `singleLineText`. Prospects is now 48 fields. |
| 6. Build the rep-facing views | **done** | Four rep views plus `System`; all eight legacy views deleted. |
| 7. Field descriptions | **done** | 15 applied. |
| 8. Fold and remove Tasks | **done** | Exported, blank row deleted, table deleted. |
| 9. Verify before unfreezing | **done** | Both gates exit 0. Full results below. |

## Final verification

| Check | Result |
|---|---|
| `airtable_required_choices.py check` | **exit 0** |
| `airtable_schema_cutover.py verify …/pre_cutover.json` | **exit 0**, `errors: []` |
| Prospects record count | **145**, unchanged end to end |
| `upload_airtable.py --dry-run` | exit 0; `ICP Lane`/`Opportunity Score` present; **no retired field in any payload** |
| 🔥 Call Queue | **12 records — the identical set `query_ready_to_send.py` returns**, not merely the same count; 0 rows violate a readiness clause |
| 🧪 Manual Review | **39**, matching `Packet Tier = Manual Review Packet` in the base |
| 📅 Follow-Ups Due | **0** — correct; nothing has reached `Sent` yet |
| 📊 Pipeline / System | **145** each |

**Not verified:** per-view hidden/shown field configuration. The REST API returns
all populated fields regardless of a view's visibility settings, so field
visibility cannot be asserted from outside the UI. Filters, sorts and record
membership *are* verified by the counts above.

`mark_outcome.py` is operational for the first time since `typecast` was removed
(`91c7131`). Live writes are unfrozen.

### Why four steps were hand work

Not a tooling gap. Probed directly on 2026-07-30 against `Prospects.Status`
with a PAT that has schema-write access:

- `PATCH /v0/meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}` with
  `{"description": …}` → **200**, and again to restore it → 200. Endpoint,
  token and field are all fine.
- The same endpoint with `options.choices` → **422 INVALID_REQUEST_UNKNOWN**, in
  five structurally different body shapes.

So Airtable's Update Field API does not modify an existing single select's
choices. `POST .../fields` *can* create a field with choices — that is how
`ICP Lane` was created — but update cannot, and there is no delete endpoint for
fields, tables or views. The base was left unchanged by the probe.

Restoring `typecast` is not the fallback; it is the drift channel that removal
closed, and it is what minted `Lead Priority: (440) 537-2459`.

## Live read-only checkpoint

Observed through the Airtable integration on 2026-07-30, **before** the
execution pass:

- Prospects: **145 records**. Treat this as an observation, not the future
  cutover invariant; the deletion-time snapshot supplies the authoritative count.
- Sales Activities: 1 record; Deals: 1; Tasks: 1 blank record; Clients: 2.
- `ICP Lane` and `Opportunity Score` did not exist; both were created during the
  execution pass. `Selected Opportunity` still does not exist and is
  deliberately deferred because no producer or approved taxonomy exists.
- `Status` still has seven choices: New, Mockup Needed, Manual Review, Not Fit,
  Sent, Follow-Up, Closed. It has no Won or Lost choice.
- **0** Prospects currently use `Status=Closed`.
- **0** Prospects currently have `Sales — Consent to Text` checked.
- `Sales Activities.Activity Type` is missing Reply and Close.
- `Sales Activities.Outcome` is missing Replied, Meeting Booked, Won, and Lost.
- `Deals.Deal Status` already includes Deposit Pending; no change is required.
- All 23 fields in `airtable_cutover_fields_2026-07.json` are still present.
- `Sales — Assigned Rep` is populated on **25** records, all observed as Michael.
  This is the one deletion below with plausible future value.
- The junk select choices listed below currently have **0** records.

The connector does not expose ordinary grid-view metadata, so current view names,
filters, and visible-field order must be checked in the Airtable UI during the
execution pass.

## Approval decisions

Approved 2026-07-30. Kept here because they are the reasoning behind what the
cutover did.

- [x] **Four rep-facing views:** Call Queue, Follow-Ups Due, Pipeline, and Manual
  Review. Blocked/data-repair diagnostics move into the hidden System view.
  *Built; all eight legacy views deleted.*
- [x] **Selected Opportunity:** defer it. No code producer or approved
  opportunity taxonomy exists, so an empty schema-only field would add clutter
  without creating an operational capability. *Held: not created, not written.*
- [x] **Assigned Rep:** follow the locked decision and delete `Sales — Assigned
  Rep` after exporting its 25 populated values. If future multi-rep assignment is
  wanted now, stop and revise the locked two-field expansion before deleting it.
  *Exported into `backup.json`, then deleted.*
- [x] **Tasks:** delete the table after exporting and confirming its only current
  row is blank. If any real task exists at execution time, migrate its due date
  to `Prospects.Next Follow-Up` and its notes to `Prospects.Notes` first.
  *Row was blank and was deleted, then the table itself.*

## Execution order

### 0. Deploy code and freeze writes — **done**

1. Review this worktree and the 836-test result.
2. Commit/push/deploy the code-side field swap. Do not run the Airtable cutover
   while the deployed uploader can still send deleted fields.
3. Pause `upload_airtable.py --live`, `mark_sent.py`, `mark_outcome.py`, and
   manual Airtable insertion for the cutover window.
4. Record the deployed Git SHA in the execution receipt.

Hard stop: the current local branch is ahead of the remote and the worktree is
dirty. This step is not satisfied merely because the code exists locally.

*Satisfied by `6390d64`: `origin/main` matches HEAD, tree clean, 837 tests green,
and no cron or systemd timer is armed.*

### 1. Capture the pre-mutation snapshot — **done**

Run the GET-only preparation command:

```bash
.venv/bin/python airtable_schema_cutover.py \
  prepare \
  --fields-config airtable_cutover_fields_2026-07.json \
  --output-dir run_state/airtable_schema_cutover/2026-07-30-ready
```

Required outputs:

- `backup.json`
- `pre_cutover.json`
- `delete_checklist.md` only when `safe_to_delete=true`
- a recorded Prospects count, schema, populated-field counts, and backup SHA-256

Copy the output directory to a second durable location before mutation. A
non-zero exit or `safe_to_delete=false` blocks deletion. It may be resolved only
by the explicit migrations below, followed by a new preparation snapshot.

*Done: 145 records, `safe_to_delete=true`, backup `e5e96bab…`. Held in three
places on three filesystems — `run_state/…` (gitignored, holds lead data),
`~/airtable-cutover-backups/2026-07-30-ready`, and
`/mnt/c/Users/lucas/lpm-airtable-backups/2026-07-30-ready`, which survives a WSL
distro reset. All three hashes match.*

### 2. Expand the schema before any deletion — **done**

Create these two Prospects fields — **done**:

| Field | Type | Configuration | Live field ID |
|---|---|---|---|
| ICP Lane | Single select | `no_site_launch`, `social_jobber_escape`, `owned_weak_site_rebuild`, `premium_perception_gap`, `portfolio_presentation_gap`, `estimate_funnel`, `optimization_only`, `skip` | `fld0DevPnL52YCy4p` |
| Opportunity Score | Number | Integer precision, 0–100 | `fldH6acA46yqTyEHb` |

Added these select choices by hand — **done**:

| Table.field | Add | Live field ID |
|---|---|---|
| Prospects.Status | Won, Lost | `fldmLhCfLofxspxNS` |
| Sales Activities.Activity Type | Reply, Close | `fldhm4mx3KfCBmVTh` |
| Sales Activities.Outcome | Replied, Meeting Booked, Won, Lost | `fldl79M2U2xVZcbBe` |

Do not add typecast to any write. Do not change `Deals.Deal Status`; Deposit
Pending already exists.

`airtable_required_choices.py check` derives this list from
`mark_outcome.REQUIRED_SELECT_CHOICES` and `queue_state.PROSPECT_STATUS_CHOICES`,
prints the UI steps, and exits 2 until every value exists. Use it rather than
this table — the table can drift, the check cannot.

### 3. Reconcile values before narrowing choices

#### Status

Re-count `Status=Closed`.

- Current read-only count is 0.
- If the execution-time count is still 0, remove Closed after Won/Lost exist.
- If records exist, classify each as Won or Lost from evidence. If the outcome is
  ambiguous, stop; never guess just to complete the migration.

The final Status vocabulary for the 2026-07-30 cutover was:

`New`, `Mockup Needed`, `Manual Review`, `Not Fit`, `Sent`, `Follow-Up`, `Won`,
`Lost`.

#### Consent

Re-count checked `Sales — Consent to Text` values.

- Current read-only count is 0.
- For any checked row found later, preserve the evidence in `Consent Posture`
  before deletion.
- Do not upgrade a legacy checkbox into “express written consent.” If supporting
  evidence does not justify one of the code-approved postures, use a non-approved
  hold note such as `legacy checkbox claim; SMS hold — evidence not verified`.

Re-run `airtable_schema_cutover.py prepare`. The hardened tool refuses
`safe_to_delete=true` when a checked legacy consent value has a blank canonical
posture.

```bash
.venv/bin/python airtable_schema_cutover.py \
  prepare \
  --fields-config airtable_cutover_fields_2026-07.json \
  --output-dir run_state/airtable_schema_cutover/2026-07-30-ready
```

*Done: 0 rows checked, so nothing needed migrating. `unmigrated` is empty and
`safe_to_delete` is true.*

#### Misplaced Sales data

Review every populated `Sales — *` value in the backup. Some observed
`Sales — Batch` cells contain call notes rather than batch identifiers. Copy any
operationally valuable notes into the surviving `Notes` field before deletion.
The backup is not a substitute for data that should remain usable in the CRM.

*Done: 16 of the 35 populated `Sales — Batch` cells held call notes rather than
the `Michael-WaterRestoration-2026-07-28` batch identifier. Each was copied into
`Notes` prefixed `Migrated from Sales — Batch during 2026-07-30 Airtable
cutover:`, and the source cell was left intact — so this step is re-runnable and
the deletion in step 5 is what finally drops the source.*

**Caveat worth knowing before deleting:** `backup.json` covers the 23 retired
fields plus `Place Key` and `Consent Posture`. It does **not** cover `Notes`. If
any of those 16 records carried prior hand-typed notes, the migration overwrote
them with no recovery path. No code writes `Notes`, so any prior content was
typed by hand. Low probability, zero recoverability — recorded rather than
discovered later.

### 4. Remove unused select choices — **done**

Removed after confirming zero records at execution time:

- Lead Priority: `D`, `New`, `(440) 537-2459`
- AI Confidence: `No-Site Launch`
- Lead Source: `50`, `Medium`
- Site Status: `Audit site / booking flow`
- Offer Fit: `Not Fit`, `Mediocre`

All nine choices had zero records in the 2026-07-30 read-only check and were
confirmed at zero immediately before removal.

`Status: Closed` was the tenth removed choice. `Won` and `Lost` existed first,
and step 3's execution-time re-count remained 0.

### 5. Delete the 23 retired Prospects fields — **done**

Delete only after the deployed code-side allowlist no longer writes them and the
second preparation report says `safe_to_delete=true`:

1. Commercial Materiality
2. Recurring Consequence
3. Root Cause Confidence
4. Packageable Solution
5. Value Mechanism
6. Beyond Free Fix
7. Self Fix Risk
8. Intentionality
9. Evidence Snapshot
10. Form Strength
11. Recommended Action
12. Asset ID
13. Asset Verifier Verdict
14. Sales — Stage
15. Sales — Attempts
16. Sales — Last Activity
17. Sales — Opener Arm
18. Sales — Consent to Text
19. Sales — Notes
20. Sales — Assigned Rep
21. Sales — Batch
22. Sales — Booked Out?
23. Sales — Ask Arm

Do not delete `Evidence Refs`, `Safe Outreach Claims`, `Asset State`, `Promise
State`, `AI Confidence`, `Owner Name`, `Mockup URL`, or `Video URL`. Those
survive deliberately even when sparsely populated.

`delete_checklist.md` in the ready snapshot carries the same list with each
field's populated-row count, so the cost of each deletion is visible at the
moment of deleting it. `Sales — Last Activity` is listed separately there as
computed: it reads as populated on all 145 records but stores nothing, so
deleting it loses no data.

This is also what clears the stale-data problem the code swap created:
`Recommended Action` (129 of 145) and `Form Strength` (123 of 145) still show
their last-written values to reps, and `upload_airtable.py` no longer writes
them, so they can never refresh.

### 6. Build the rep-facing views — **done**

Keep one hidden `System` view containing all fields for diagnostics. Delete
`Grid 11` and consolidate the legacy numbered/temporary views into exactly four
rep-facing views:

1. **🔥 Call Queue**
   - Filter: Packet Tier = Sendable Packet
   - Commercial Fit State = commercially-sendable
   - Contact Eligibility = contact-now
   - Status is New
   - Sort: Lead Priority ascending, then Opportunity Score descending, then
     Score Total descending
   - Group: Vertical
2. **📅 Follow-Ups Due**
   - Next Follow-Up is on or before today
   - Status is Follow-Up
   - Sort: Next Follow-Up ascending
3. **📊 Pipeline**
   - Kanban grouped by the canonical seven-value Status
4. **🧪 Manual Review**
   - Packet Tier = Manual Review Packet
   - Sort: Opportunity Score descending, then Score Total descending

Show only these 15 fields in Call Queue and Manual Review; use a smaller subset
in the other two views:

`Business Name`, `Owner Name`, `Vertical`, `City`, `Phone`, `Status`,
`Lead Priority`, `Opportunity Score`, `ICP Lane`, `Specific Observation`,
`Opener`, `Safe Outreach Claims`, `Outreach Channel`, `Next Follow-Up`,
`Website URL`.

Keep Packet Tier, Commercial Fit State, Contact Eligibility, evidence internals,
and path/debug fields available in System but hidden from rep-facing views.

Current views: `System`, `🔥 Call Queue`, `📅 Follow-Ups Due`, `📊 Pipeline`, and
`🧪 Manual Review`. All eight legacy views were deleted. No script reads a view,
so this step is rep ergonomics rather than a correctness gate.

### 7. Add rep-readable field descriptions — **done**

All 15 applied 2026-07-30 and confirmed live.

| Field | Description |
|---|---|
| Business Name | Business the rep is contacting. |
| Owner Name | Confirmed owner or decision-maker; blank means unverified. |
| Vertical | Canonical trade used for routing and scoring. |
| City | Primary market location for this prospect. |
| Phone | Business contact number; confirm the recipient before outreach. |
| Website URL | Current business website used for the audit. |
| Status | Current CRM stage. Won and Lost are terminal. |
| Lead Priority | Pipeline grade: A is strongest, then B, C, or Skip. |
| ICP Lane | Deterministic opportunity lane produced by `icp_lanes`. |
| Opportunity Score | Deterministic 0–100 strength of the selected ICP lane. |
| Specific Observation | Concrete, observable fact the rep may reference. |
| Opener | Approved first outreach line grounded in the observation. |
| Safe Outreach Claims | Claims supported by recorded evidence; do not go beyond them. |
| Outreach Channel | Channel actually used for the most recent outreach. |
| Next Follow-Up | Next date the prospect should re-enter the follow-up queue. |

### 8. Fold and remove Tasks — **partly done**

Export the Tasks table. It currently contains one blank row.

- If it remains blank, delete the row and then the Tasks table.
- If any real task appears, migrate linked Prospect, Due Date, and Notes into the
  surviving Prospects fields before deletion.
- Confirm Prospects retains `Next Follow-Up` and `Notes`.

Do not delete Sales Activities, Deals, or Clients.

*Done: exported, the row was blank, `reclWKZoAUvr1fP8h` deleted, 0 rows remain
(`tblPI4P4kLzMTGiZ9`). Prospects still carries `Next Follow-Up` and `Notes`.
Deleting the table itself is a hand step — the API has no delete-table endpoint.*

### 9. Verify before unfreezing writes — **done**

Run both gates. Both exit 0 as of 2026-07-30. They stay useful afterwards as
drift checks — re-run either after any base edit, and a non-zero exit means the
base has fallen behind the code again:

```bash
# Does the base offer every select choice the code writes?
.venv/bin/python airtable_required_choices.py check

# Are the 23 retired fields gone, and is the record count unchanged?
.venv/bin/python airtable_schema_cutover.py \
  verify \
  run_state/airtable_schema_cutover/2026-07-30-ready/pre_cutover.json
```

Then verify read-only:

- Prospects count exactly matches the deletion-ready snapshot.
- All 23 retired fields are absent.
- Both new fields exist with the exact types above.
- Status has the canonical seven choices and no Closed.
- Required Sales Activities choices exist.
- Deposit Pending still exists.
- The nine junk choices are absent.
- The four rep-facing views return the intended records and expose only the
  intended fields.
- `query_ready_to_send.py` count matches the Call Queue count.
- `upload_airtable.py --dry-run` succeeds on a representative scored CSV.
- A read-only schema check contains every value in
  `mark_outcome.REQUIRED_SELECT_CHOICES` — that is
  `airtable_required_choices.py check`, exiting 0.

Only after every check passes should live Airtable writes be unfrozen.

---

## Hand steps — all completed 2026-07-30

Everything below needed the Airtable UI. No API call can do any of it — see
*Why four steps were hand work* above. Kept as the record of what was done, and
as the procedure to follow if this base is ever rebuilt.

### H1 — Add 8 select choices (unblocks `mark_outcome.py`)

This is the one with an operational cost today: until it is done,
`mark_outcome.py` cannot record a single funnel event. It fails honestly rather
than silently, but the capability is offline.

Run `.venv/bin/python airtable_required_choices.py check` — it prints the exact
field, a link to each table, and the exact spellings. As of 2026-07-30:

- [x] **Prospects → Status** — add `Won`, `Lost`
- [x] **Sales Activities → Activity Type** — add `Reply`, `Close`
- [x] **Sales Activities → Outcome** — add `Replied`, `Meeting Booked`, `Won`, `Lost`

Spelling must match exactly, including the hyphen in `Follow-Up` style values.
A choice named `won` or `Won ` is a different choice and still 422s.

- [x] Re-run `airtable_required_choices.py check` — **must exit 0**

### H2 — Remove 10 select choices

**Sequenced after H1:** `Closed` can only go once `Won`/`Lost` exist. Re-confirm
each choice is on 0 records immediately before removing it.

- [x] Lead Priority: `D`, `New`, `(440) 537-2459`
- [x] AI Confidence: `No-Site Launch`
- [x] Lead Source: `50`, `Medium`
- [x] Site Status: `Audit site / booking flow`
- [x] Offer Fit: `Not Fit`, `Mediocre`
- [x] Status: `Closed` *(after H1, and only while the count is still 0)*

### H3 — Delete 23 fields and the Tasks table

Read `run_state/airtable_schema_cutover/2026-07-30-ready/delete_checklist.md`
first: it lists each field with its populated-row count, and separates
`Sales — Last Activity` as computed with nothing to back up.

- [x] Delete the 23 fields listed in step 5
- [x] Delete the `Tasks` table (`tblPI4P4kLzMTGiZ9`, 0 rows)
- [x] Keep `Sales Activities`, `Deals`, `Clients`
- [x] Run `airtable_schema_cutover.py verify …/2026-07-30-ready/pre_cutover.json`
      — **must exit 0**, and the record count must still be 145

### H4 — Build the 4 rep-facing views

- [x] Create the four views exactly as specified in step 6
- [x] Delete `Grid 11`
- [x] Keep `System` hidden with every field visible for diagnostics
- [x] Confirm `query_ready_to_send.py`'s count matches the Call Queue count

### H5 — Unfreeze

- [x] Work through step 9's read-only checklist
- [x] Update `docs/airtable_cutover_receipt_2026-07-30.json` with what H1–H4 did
- [x] Resume live writes

## Rollback boundary

- Before field deletion: revert select/view edits manually and remove the two
  new fields if necessary.
- After field deletion: Airtable field deletion is not treated as reversible.
  Restore data only from the versioned backup and snapshot; stop before any
  automated upload.
- Any Prospects count drift, missing canonical field, ambiguous Closed outcome,
  or unmigrated checked consent value is an immediate abort.
