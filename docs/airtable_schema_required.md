# Airtable Schema — Required Fields

Base of record: **Local Lead Machine CRM** (`appLbCDdUDSZf7qaK`, `AIRTABLE_BASE_ID`). It is the only
base this repo writes to. A second base named *Local Pro Marketing* exists in the workspace and is
**not** part of the pipeline — nothing here reads or writes it.

The Prospects field list below mirrors `upload_airtable._FIELD_SCHEMA`, which is the code's own
statement of every field the pipeline may write. That list is the authority; keep the two together.

## How to read the `Exists` column

`yes (2026-07-29)` means the field was **observed in the live base** on that date. `?` means **not
observed** — it is not a claim that the field is missing, only that nobody has checked. Do not
promote a `?` to `yes` without querying the base; the whole point of the column is that an unchecked
field and a present field are different states. The 2026-07-29 pass read select vocabularies only,
so it confirms exactly four Prospects fields.

Also observed 2026-07-29: the base has five tables (`Prospects`, `Sales Activities`, `Deals`,
`Tasks`, `Clients`) and Prospects carries nine views — `System`, `00 - Sales Assignment`,
`10 - New Calls`, `20 - Follow-Ups`, `30 - Active Pipeline`, `40 - Hot Opportunities`,
`50 - Closed Outcomes`, `60 - Data Repair`, `Grid 11`. No script here depends on a view.

## `Prospects` (`AIRTABLE_PROSPECTS_TABLE`)

| Field | Airtable Type | Exists | Notes |
|-------|--------------|--------|-------|
| Business Name | Single line text | ? |  |
| Owner Name | Single line text | ? |  |
| Vertical | Single select | yes (2026-07-29) | Live choices are twelve: hardscaping, landscaping, concrete, roofing, HVAC, plumbing, other, epoxy, detailing, water_restoration, garage_doors, pressure_washing. `lead_machine/verticals.py` registers **seven** profiles (hardscaping, detailing, water_restoration, garage_doors, plumbing, epoxy, roofing) and those are the only slugs the pipeline writes; the other five are live-only. Registering a profile can relabel existing leads (`tests/test_geo_and_vertical_resolution.py::RegistryGrowthReclassifiesLeadsTest`), so add the choice to this base *before* adding the profile |
| City | Single line text | ? |  |
| Metro Context | Single line text | ? | Authoritative Operator metro-context slug |
| Phone | Phone number | ? |  |
| Email | Email | ? |  |
| Website URL | URL | ? |  |
| Google Maps URL | URL | ? |  |
| Reviews | Number | ? | Integer; set field to 'Integer' precision |
| Rating | Number | ? | Decimal; set field to 2 decimal places |
| Specific Observation | Long text | ? |  |
| Packet Tier | Single select | ? | Options: Sendable Packet, Manual Review Packet, Watchlist, No Packet |
| Lead Priority | Single select | yes (2026-07-29) | Code writes **A, B, C, Skip** only (`score_leads.PRIORITY_RANK`; `classify_priority` can return `Skip`, and an upload without `--only-sendable` carries it through). Live also carries `D` and `New`, which no code path here writes, plus a junk choice literally named **`(440) 537-2459`** — a phone number minted by the `typecast` flag that has since been removed. **Runbook: delete `(440) 537-2459`** and decide whether `D`/`New` are intentional |
| Offer Fit | Single select | ? | Options: No-Site Launch, Website Rebuild, Estimate Funnel, Automation / Follow-Up |
| Site Status | Single select | ? | Options: Missing, Broken, No Website, Loaded, Redirect Issue, Social Only, Unknown |
| Visible Pain Score | Number | ? | Integer 0–100 (sendability thresholds fire at 10/16/30/35/40) |
| Visible Pain Reasons | Long text | ? |  |
| Send Trigger | Single line text | ? |  |
| Buyer Intent Signals | Long text | ? | Comma-separated signals detected by LLM |
| Commercial Fit State | Single select | ? | Options: commercially-sendable, minor-fix/nurture, needs-broader-system-evidence, intentional-or-ambiguous, not-an-LPM-fit, unverified |
| LPM Offer Component | Single line text | ? | One authoritative offer component |
| Contact Eligibility | Single select | ? | Options: contact-now, contact-after-asset, hold-commercial-case |
| Asset State | Single select | ? | Options: none, planned, verified-existing |
| Promise State | Single select | ? | Options: asset-free, future-build, existing-verified-asset |
| Evidence Refs | Long text | ? | Structured evidence identifiers |
| Safe Outreach Claims | Long text | ? | Claims bound to evidence identifiers |
| AI Confidence | Single select | ? | Options: low, medium, high |
| Score Total | Number | ? | Integer |
| ICP Lane | Single select | no (2026-07-30) | Create during the approved cutover. Options: no_site_launch, social_jobber_escape, owned_weak_site_rebuild, premium_perception_gap, portfolio_presentation_gap, estimate_funnel, optimization_only, skip |
| Opportunity Score | Number | no (2026-07-30) | Create during the approved cutover; integer 0–100 from `icp_lanes` |
| Opener | Long text | ? |  |
| Send Blockers | Long text | ? |  |
| Skip Reason | Single line text | ? |  |
| Place Key | Single line text | ? | Used as upsert key; must be unique per business |
| Status | Single select | yes (2026-07-30) | Canonical code vocabulary: New, Mockup Needed, Manual Review, Not Fit, Sent, Follow-Up, Won, Lost. The live base still has `Closed` instead of `Won`/`Lost`; the runbook must add both, classify any existing Closed rows, then remove Closed. `Not Fit` is applied by hand, never by code |
| Outreach Channel | Single select | yes (2026-07-29) | Live choices match exactly: Email, Phone, Instagram DM, SMS. This is why `mark_sent.py` is safe without `typecast` and `mark_outcome.py` is not — see below |
| Consent Posture | Single line text | ? | Required and documented before any SMS draft can pass |
| Last Contacted | Date | ? |  |
| Next Follow-Up | Date | ? |  |
| Mockup URL | URL | ? |  |
| Video URL | URL | ? |  |
| Packet Path | Single line text | ? | Local file path to the packet markdown file |

The base also carries a `Sales — *` block (Stage, Outcome, Assigned Rep, Opener Arm, Ask Arm,
Booked Out?, Consent to Text, Next Follow-Up, …) used by the manual cold-call test. **No script in
this repo reads or writes those columns.** Two of them shadow columns the pipeline does own —
`Sales — Next Follow-Up` (dateTime) against `Next Follow-Up` (date), and `Sales — Outcome` against
`Status`. The pipeline's columns are the ones automation trusts; the `Sales — *` pair is hand-kept
call-sheet state. Do not wire a script to both.

## Post-send CRM tables

Written only by `mark_outcome.py`, after Lou reports something that already happened. Nothing in
the automated pipeline creates rows here.

> **Blocked as of 2026-07-29.** `mark_outcome.py` cannot write a single row until six select choices
> are created by hand in `Sales Activities`. The exact list, and why it is a hand step rather than
> something the code can do for itself, is immediately below. `mark_outcome.RUNBOOK_DOC` points a
> failing operator at this file, so this is the page that has to be right.

### `Sales Activities` (`AIRTABLE_SALES_ACTIVITIES_TABLE`, default `Sales Activities`)

One immutable row per observed funnel event — the activity ledger. This is the authoritative
in-CRM record of what happened on a lead after `Status=Sent`.

| Field | Airtable Type | Notes |
|-------|--------------|-------|
| Activity Name | Single line text | Primary field; `"<Activity Type> — <Business Name>"` |
| Activity Type | Single select | `mark_outcome` writes **Reply, Call, Close** |
| Activity Date | Date/time | When the event happened (12:00 UTC on the reported date) |
| Outcome | Single select | `mark_outcome` writes **Replied, Meeting Booked, Won, Lost** |
| Details | Long text | `--notes` |
| Linked Prospect | Link → Prospects | Join back to the lead |

#### These six choices must be PRE-CREATED by hand. They do not exist yet.

The field's stock choices describe meeting logistics and cannot express a funnel outcome. They used
to be minted on first write by Airtable's `typecast` flag; **`typecast` was removed from every write
path** in this repo (`91c7131`) and cannot come back — `tests/test_mark_outcome.py`'s
`test_no_module_hands_airtable_a_typecast_flag` AST-scans every shipped module and fails the suite
if any of them sets it. It was removed because it minted a choice for *whatever* string it was
handed, which is how `Prospects.Lead Priority` acquired an option named `(440) 537-2459`.

Removing it moved the obligation onto the base, and **the base has not met it as of 2026-07-29**:

| Field | Live choices (observed 2026-07-29) | Missing — must be created |
|-------|------------------------------------|---------------------------|
| Activity Type | Call, Email, Meeting, Follow Up, Demo, Note, Other | **Reply**, **Close** |
| Outcome | Completed, Scheduled, Rescheduled, Cancelled, No Show, Outcome Pending | **all four**: Replied, Meeting Booked, Won, Lost |

Until a human creates those six, **every** `mark_outcome` event 422s with
`INVALID_MULTIPLE_CHOICE_OPTIONS` and not one outcome can be recorded. This is a hard prerequisite,
not cleanup. Restoring `typecast` is not the fix; it is the drift channel the removal closed.

Whether the Meta API can add a choice to an existing single-select is **unverified** — an earlier
version of this file asserted it refuses, on no recorded evidence. The PAT is `permissionLevel:
create`, and Airtable does document *adding* (never removing) choices on an existing select, so the
refusal claim is probably wrong. **Verify it during the runbook**, in the base's own UI or via the
Meta API, before assuming either way. Do not re-assert either claim here without a recorded result.

`mark_outcome.REQUIRED_SELECT_CHOICES` is the machine-readable form of this table — the exact
`{table: {field: choices}}` set the base must offer — so a runbook can be generated from the module
rather than transcribed beside it. `preflight_select_values` checks a run's values against that
declaration before the first request, so code that outruns the declaration fails by name instead of
as an opaque 422; a *base* that is behind the declaration still surfaces as the 422 above.

The vocabulary is deliberately **closed** and pinned by `tests/test_mark_outcome.py`. Adding a
seventh value is a schema change in four places: the code map, `REQUIRED_SELECT_CHOICES`, this
table, and the base itself.

### `Deals` (`AIRTABLE_DEALS_TABLE`, default `Deals`)

| Field | Airtable Type | Notes |
|-------|--------------|-------|
| Package | Single line text | Primary field; `--deal-package`, falling back to the business name |
| Deal Status | Single select | Created as **Deposit Pending** — the win is recorded, the money has not moved. Live choices observed 2026-07-29: Proposed, Deposit Pending, Deposit Paid, Contract Pending, Contract Signed, In Progress, Launched, Retainer Active, Lost. `Deposit Pending` is present, so this field needs no runbook work |
| Setup Price / Retainer Price | Currency | Optional, from CLI flags |
| Prospect | Link → Prospects | Set on create |
| Deposit/Contract/Invoice checkboxes, Kickoff / Launch / Retainer Start dates | — | Hand-kept |
| Clients | Link → Clients | Hand-linked at onboarding |

### `Tasks` and `Clients`

Manual tables. `Tasks` holds follow-ups linked to a prospect; `Clients` holds post-close hosting and
retainer state, linked to both the Prospect and the Deal. **No code writes either one** — first-close
onboarding is a human step by design (see AGENTS.md Gate 0), and the schema exists so that step has
somewhere to land.

## Open schema debt (what the Airtable runbook has to do)

Everything here is a **base** change. The code side is already done and the repo is deliberately
unable to make these changes for itself — that is the point of removing `typecast`.

1. **Create `Sales Activities.Activity Type` choices `Reply` and `Close`.** Blocking; without them
   `mark_outcome` records nothing.
2. **Create `Sales Activities.Outcome` choices `Replied`, `Meeting Booked`, `Won`, `Lost`.** Same.
3. **Create `Prospects.Status` choices `Won` and `Lost`.** Classify every existing `Closed` row
   into one of them before deleting `Closed`; do not guess when the outcome is unknown.
4. **Delete `Prospects.Lead Priority` choice `(440) 537-2459`.** Typecast residue, not a grade.
5. **Decide on `Prospects.Lead Priority` choices `D` and `New`.** Present in the base, written by
   nothing in this repo.
6. **Record whether the Meta API can add a choice to an existing single-select**, and write the
   answer back into the Sales Activities section above, replacing the "unverified" note. Adding
   choices by hand in the UI is always available and is the assumed path.
7. **Fill the `Exists` column for the remaining Prospects fields** while you are in the base, and
   date the pass. `?` means unchecked, not absent.
