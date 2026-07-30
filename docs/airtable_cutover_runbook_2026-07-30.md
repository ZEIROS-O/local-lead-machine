# Proposed Airtable Cutover Runbook — 2026-07-30

Status: **review only; no Airtable mutation has been performed**

Base: `appLbCDdUDSZf7qaK` (`Local Lead Machine CRM`)

This is the phase-6 handoff. It deliberately stops before execution. Lou must
approve this document, and the code changes must be reviewed, committed, pushed,
and deployed before a separate run changes the live base.

## Live read-only checkpoint

Observed through the Airtable integration on 2026-07-30:

- Prospects: **145 records**. Treat this as an observation, not the future
  cutover invariant; the deletion-time snapshot supplies the authoritative count.
- Sales Activities: 1 record; Deals: 1; Tasks: 1 blank record; Clients: 2.
- `ICP Lane` and `Opportunity Score` do not exist. `Selected Opportunity` also
  does not exist and is deliberately deferred because no producer or approved
  taxonomy exists.
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

Lou should explicitly approve or amend these before execution:

- [ ] **Four rep-facing views:** Call Queue, Follow-Ups Due, Pipeline, and Manual
  Review. Blocked/data-repair diagnostics move into the hidden System view.
- [x] **Selected Opportunity:** defer it. No code producer or approved
  opportunity taxonomy exists, so an empty schema-only field would add clutter
  without creating an operational capability.
- [ ] **Assigned Rep:** follow the locked decision and delete `Sales — Assigned
  Rep` after exporting its 25 populated values. If future multi-rep assignment is
  wanted now, stop and revise the locked two-field expansion before deleting it.
- [ ] **Tasks:** delete the table after exporting and confirming its only current
  row is blank. If any real task exists at execution time, migrate its due date
  to `Prospects.Next Follow-Up` and its notes to `Prospects.Notes` first.

## Execution order

### 0. Deploy code and freeze writes

1. Review this worktree and the 836-test result.
2. Commit/push/deploy the code-side field swap. Do not run the Airtable cutover
   while the deployed uploader can still send deleted fields.
3. Pause `upload_airtable.py --live`, `mark_sent.py`, `mark_outcome.py`, and
   manual Airtable insertion for the cutover window.
4. Record the deployed Git SHA in the execution receipt.

Hard stop: the current local branch is ahead of the remote and the worktree is
dirty. This step is not satisfied merely because the code exists locally.

### 1. Capture the pre-mutation snapshot

Run the GET-only preparation command:

```bash
.venv/bin/python airtable_schema_cutover.py \
  prepare \
  --fields-config airtable_cutover_fields_2026-07.json \
  --output-dir run_state/airtable_schema_cutover/2026-07-cutover-pre
```

Required outputs:

- `backup.json`
- `pre_cutover.json`
- `delete_checklist.md` only when `safe_to_delete=true`
- a recorded Prospects count, schema, populated-field counts, and backup SHA-256

Copy the output directory to a second durable location before mutation. A
non-zero exit or `safe_to_delete=false` blocks deletion. It may be resolved only
by the explicit migrations below, followed by a new preparation snapshot.

### 2. Expand the schema before any deletion

Create these two Prospects fields:

| Field | Type | Configuration |
|---|---|---|
| ICP Lane | Single select | `no_site_launch`, `social_jobber_escape`, `owned_weak_site_rebuild`, `premium_perception_gap`, `portfolio_presentation_gap`, `estimate_funnel`, `optimization_only`, `skip` |
| Opportunity Score | Number | Integer precision, 0–100 |

Add, but do not yet remove, these select choices:

| Table.field | Add |
|---|---|
| Prospects.Status | Won, Lost |
| Sales Activities.Activity Type | Reply, Close |
| Sales Activities.Outcome | Replied, Meeting Booked, Won, Lost |

Do not add typecast to any write. Do not change `Deals.Deal Status`; Deposit
Pending already exists.

### 3. Reconcile values before narrowing choices

#### Status

Re-count `Status=Closed`.

- Current read-only count is 0.
- If the execution-time count is still 0, remove Closed after Won/Lost exist.
- If records exist, classify each as Won or Lost from evidence. If the outcome is
  ambiguous, stop; never guess just to complete the migration.

The final Status vocabulary must be exactly:

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
  --output-dir run_state/airtable_schema_cutover/2026-07-cutover-ready
```

#### Misplaced Sales data

Review every populated `Sales — *` value in the backup. Some observed
`Sales — Batch` cells contain call notes rather than batch identifiers. Copy any
operationally valuable notes into the surviving `Notes` field before deletion.
The backup is not a substitute for data that should remain usable in the CRM.

### 4. Remove unused select choices

Only after confirming zero records at execution time, remove:

- Lead Priority: `D`, `New`, `(440) 537-2459`
- AI Confidence: `No-Site Launch`
- Lead Source: `50`, `Medium`
- Site Status: `Audit site / booking flow`
- Offer Fit: `Not Fit`, `Mediocre`

All nine choices had zero records in the 2026-07-30 read-only check.

### 5. Delete the 23 retired Prospects fields

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

### 6. Build the rep-facing views

Keep one hidden `System` view containing all fields for diagnostics. Delete
`Grid 11` and consolidate the legacy numbered/temporary views into exactly four
rep-facing views:

1. **🔥 Call Queue**
   - Filter: Packet Tier = Sendable Packet
   - Commercial Fit State = commercially-sendable
   - Contact Eligibility = contact-now
   - Status is New or Mockup Needed
   - Sort: Lead Priority ascending, then Opportunity Score descending, then
     Score Total descending
   - Group: Vertical
2. **📅 Follow-Ups Due**
   - Next Follow-Up is on or before today
   - Status is Sent or Follow-Up
   - Sort: Next Follow-Up ascending
3. **📊 Pipeline**
   - Kanban grouped by the canonical eight-value Status
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

### 7. Add rep-readable field descriptions

At minimum, apply these descriptions to every displayed field:

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

### 8. Fold and remove Tasks

Export the Tasks table. It currently contains one blank row.

- If it remains blank, delete the row and then the Tasks table.
- If any real task appears, migrate linked Prospect, Due Date, and Notes into the
  surviving Prospects fields before deletion.
- Confirm Prospects retains `Next Follow-Up` and `Notes`.

Do not delete Sales Activities, Deals, or Clients.

### 9. Verify before unfreezing writes

Run:

```bash
.venv/bin/python airtable_schema_cutover.py \
  verify \
  run_state/airtable_schema_cutover/2026-07-cutover-ready/pre_cutover.json
```

Then verify read-only:

- Prospects count exactly matches the deletion-ready snapshot.
- All 23 retired fields are absent.
- Both new fields exist with the exact types above.
- Status has the canonical eight choices and no Closed.
- Required Sales Activities choices exist.
- Deposit Pending still exists.
- The nine junk choices are absent.
- The four rep-facing views return the intended records and expose only the
  intended fields.
- `query_ready_to_send.py` count matches the Call Queue count.
- `upload_airtable.py --dry-run` succeeds on a representative scored CSV.
- A read-only schema check contains every value in
  `mark_outcome.REQUIRED_SELECT_CHOICES`.

Only after every check passes should live Airtable writes be unfrozen.

## Rollback boundary

- Before field deletion: revert select/view edits manually and remove the two
  new fields if necessary.
- After field deletion: Airtable field deletion is not treated as reversible.
  Restore data only from the versioned backup and snapshot; stop before any
  automated upload.
- Any Prospects count drift, missing canonical field, ambiguous Closed outcome,
  or unmigrated checked consent value is an immediate abort.
