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
| Lead Priority | Single select | yes (2026-07-30) | Code writes **A, B, C, Skip** only (`score_leads.PRIORITY_RANK`; `classify_priority` can return `Skip`, and the total scored-row upload carries it through). Live choices are the same clean four: `A`, `B`, `C`, `Skip`. The junk `(440) 537-2459` choice and unused `D`/`New` choices were deleted during the cutover. |
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
| ICP Lane | Single select | yes (2026-07-30) | Created during the cutover as `fld0DevPnL52YCy4p`. All eight choices confirmed live: no_site_launch, social_jobber_escape, owned_weak_site_rebuild, premium_perception_gap, portfolio_presentation_gap, estimate_funnel, optimization_only, skip |
| Opportunity Score | Number | yes (2026-07-30) | Created during the cutover as `fldH6acA46yqTyEHb`, precision 0; integer 0–100 from `icp_lanes` |
| Opener | Long text | ? |  |
| Send Blockers | Long text | ? |  |
| Skip Reason | Single line text | ? |  |
| Place Key | Single line text | ? | Used as upsert key; must be unique per business |
| Assigned Setter | Single select | ? | One exact choice per name in `LPM_SETTER_ROSTER`. Upload seeds a stable assignment only on create; it never PATCHes an existing assignment |
| Status | Single select | operator-reported live (2026-08-03) | Canonical code vocabulary: **New, Manual Review, Not Fit, Follow-Up, Won, Lost, Booked**. Lou manually consolidated `Mockup Needed` into `New`, `Sent` into `Follow-Up`, and `Do Not Contact` into `Not Fit` in the live base. Run `airtable_required_choices.py check` before writes; `Booked` has not been independently verified live |
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

> **Previously unblocked 2026-07-30; MVP schema work is now pending.** `mark_outcome.py` was unable to write a single row from `91c7131`
> (typecast removal) until the eight missing select choices were created by hand — six in
> `Sales Activities`, plus `Won` and `Lost` on `Prospects.Status`. All eight now exist and the
> module became operational. The MVP now additionally requires `Sales Activities.Outcome = Held`,
> `Prospects.Status = Booked`, the `Assigned Setter` field and choices, and
> `Deals.Cash Collected`. These additions are code-declared/documented but not verified live.
>
> **If a write starts 422-ing, run this first:**
> `.venv/bin/python airtable_required_choices.py check` — it derives the requirement from
> `mark_outcome.REQUIRED_SELECT_CHOICES`, `mark_outcome.OPERATOR_REQUIRED_SELECT_CHOICES`,
> `queue_state.PROSPECT_STATUS_CHOICES`, and the configured setter roster, so it cannot go stale the
> way this page can. Exit 0 means the select vocabulary is complete; exit 2 prints exactly which
> choice is missing and the UI steps to add it. Adding a value to any declaration without adding it
> to the base puts the workflow straight back into the blocked state, and this check catches that.
> `mark_outcome.RUNBOOK_DOC` points a failing operator at this file, so this is the page that has to
> be right.

### `Sales Activities` (`AIRTABLE_SALES_ACTIVITIES_TABLE`, default `Sales Activities`)

One immutable row per observed funnel event — the activity ledger. This is the authoritative
in-CRM record of what happened after contact moves a lead to `Status=Follow-Up`.

| Field | Airtable Type | Notes |
|-------|--------------|-------|
| Activity Name | Single line text | Primary field; `"<Activity Type> — <Business Name>"` |
| Activity Type | Single select | `mark_outcome` writes **Reply, Call, Close** |
| Activity Date | Date/time | When the event happened (12:00 UTC on the reported date) |
| Outcome | Single select | `mark_outcome` writes **Replied, Meeting Booked, Won, Lost**; the closer marks **Held** manually |
| Details | Long text | `--notes` |
| Linked Prospect | Link → Prospects | Join back to the lead |

#### Every choice here must be PRE-CREATED by hand. The original eight exist; `Held` is pending live verification.

The field's stock choices describe meeting logistics and cannot express a funnel outcome. They used
to be minted on first write by Airtable's `typecast` flag; **`typecast` was removed from every write
path** in this repo (`91c7131`) and cannot come back — `tests/test_mark_outcome.py`'s
`test_no_module_hands_airtable_a_typecast_flag` AST-scans every shipped module and fails the suite
if any of them sets it. It was removed because it minted a choice for *whatever* string it was
handed, which is how `Prospects.Lead Priority` acquired an option named `(440) 537-2459`.

Removing it moved the obligation onto the base. The base did not meet it for a day and a half —
`mark_outcome` 422'd on every event with `INVALID_MULTIPLE_CHOICE_OPTIONS` and recorded nothing —
until the eight were added by hand on 2026-07-30:

| Field | Added by hand 2026-07-30 | Live vocabulary now |
|-------|--------------------------|---------------------|
| `Sales Activities.Activity Type` | **Reply**, **Close** | Call, Email, Meeting, Follow Up, Demo, Note, Other, Reply, Close |
| `Sales Activities.Outcome` | **Replied**, **Meeting Booked**, **Won**, **Lost** | Completed, Scheduled, Rescheduled, Cancelled, No Show, Outcome Pending, Replied, Meeting Booked, Won, Lost. Add **Held** for the MVP closer workflow |
| `Prospects.Status` | **Won**, **Lost** (and `Closed` removed) | Current canonical vocabulary after the 2026-08-03 consolidation: New, Manual Review, Not Fit, Follow-Up, Won, Lost, Booked |

The same obligation applies to any value added later: it is a hard prerequisite, not cleanup, and
restoring `typecast` is not the fix — it is the drift channel the removal closed.

#### Why the code cannot do it: the recorded result (2026-07-30)

This was carried as "unverified" for two days. It is now tested. Probed against
`Prospects.Status` (`fldmLhCfLofxspxNS`) with the repo's own PAT, which has schema-write access —
it created `ICP Lane` and `Opportunity Score` the same day:

| Call | Result |
|---|---|
| `PATCH /v0/meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}` with `{"description": …}` | **200**, and 200 again restoring the original value |
| The same endpoint with `options.choices` | **422 `INVALID_REQUEST_UNKNOWN`** |

The 422 reproduced across five structurally different bodies: existing choices echoed verbatim
(`id` + `name` + `color`); without colours; as `id` only; new choices with and without colours; and
with an explicit `"type": "singleSelect"`. Identical generic validation failure every time, while a
`description` PATCH on the very same field succeeded. So it is not the token, not the endpoint, and
not the body shape — **Airtable's Update Field API does not modify an existing single select's
choices.**

`POST .../fields` *can* create a new field with choices; that asymmetry is why `ICP Lane` shipped
with all eight lanes and `Status` could not gain two. There is no delete endpoint for fields,
tables or views either. The base was left unchanged by the probe.

Re-run those two calls before believing otherwise. One version of this file asserted the refusal on
no recorded evidence; the next called it unverified but leaned the other way, also on none. Both
guesses cost a cutover pass. Do not re-assert either direction without a result you actually ran.

The two alternatives are both worse than a hand step: restoring `typecast` is the drift channel
that removal closed, and creating a replacement field to migrate 145 records onto would be a
destructive rename dance on the CRM's most-read column with no way to delete the original.

`mark_outcome.REQUIRED_SELECT_CHOICES` is the exact machine-readable writer vocabulary;
`mark_outcome.OPERATOR_REQUIRED_SELECT_CHOICES` separately owns manual values such as `Held`.
The read-only checker unions both, while `preflight_select_values` checks only values the writer is
about to send. Code that outruns its declaration fails by name instead of as an opaque 422; a
*base* that is behind either declaration is reported by `airtable_required_choices.py check`.

The writer vocabulary is deliberately **closed** and pinned by `tests/test_mark_outcome.py`. Adding
a writer-emitted value changes the event map, `REQUIRED_SELECT_CHOICES`, this table and the base.
Adding an operator-only value changes `OPERATOR_REQUIRED_SELECT_CHOICES`, this table and the base.

### `Deals` (`AIRTABLE_DEALS_TABLE`, default `Deals`)

| Field | Airtable Type | Notes |
|-------|--------------|-------|
| Package | Single line text | Primary field; `--deal-package`, falling back to the business name |
| Deal Status | Single select | Created as **Deposit Pending** — the win is recorded, the money has not moved. Live choices observed 2026-07-29: Proposed, Deposit Pending, Deposit Paid, Contract Pending, Contract Signed, In Progress, Launched, Retainer Active, Lost. `Deposit Pending` is present, so this field needs no runbook work |
| Setup Price / Retainer Price | Currency | Optional, from CLI flags |
| Cash Collected | Currency | New MVP field, created by hand. The closer records the first cleared payment; setter commission is calculated on collected cash, never signature value |
| Prospect | Link → Prospects | Set on create |
| Deposit/Contract/Invoice checkboxes, Kickoff / Launch / Retainer Start dates | — | Hand-kept |
| Clients | Link → Clients | Hand-linked at onboarding |

### `Tasks` and `Clients`

Manual tables. `Tasks` holds follow-ups linked to a prospect; `Clients` holds post-close hosting and
retainer state, linked to both the Prospect and the Deal. **No code writes either one** — first-close
onboarding is a human step by design (see AGENTS.md Gate 0), and the schema exists so that step has
somewhere to land.

`Tasks` is being retired: its work folds into `Prospects.Next Follow-Up` and `Prospects.Notes`. As of
2026-07-30 it holds **0 rows** — the single blank row was deleted during the cutover — and the table
itself is waiting on hand step H3 because the API has no delete-table endpoint.

## Schema debt — cleared 2026-07-30

Everything here was a **base** change the repo is deliberately unable to make for itself; that is
the point of removing `typecast`. All of it landed in the cutover. Full record:
**[`airtable_cutover_runbook_2026-07-30.md`](airtable_cutover_runbook_2026-07-30.md)**.

1. ~~Create `Sales Activities.Activity Type` choices `Reply` and `Close`.~~ **Done.**
2. ~~Create `Sales Activities.Outcome` choices `Replied`, `Meeting Booked`, `Won`, `Lost`.~~ **Done.**
3. ~~Create `Prospects.Status` choices `Won` and `Lost`, then remove `Closed`.~~ **Done.** 0 rows
   used `Closed` at execution time, so nothing needed classifying.
4. ~~Delete `Prospects.Lead Priority` choice `(440) 537-2459`.~~ **Done** — typecast residue, not a
   grade.
5. ~~Decide on `Prospects.Lead Priority` choices `D` and `New`.~~ **Done — both deleted.** Neither
   appears anywhere in this repo. `A`, `B`, `C`, `Skip` remain; **`Skip` is written by the scorer on
   every disqualified lead and must never be removed**, despite having sat next to the junk.
   `upload_airtable.py:72` now correctly describes this field as "Options: A, B, C, Skip (grade)".
6. ~~Record whether the Meta API can add a choice to an existing single-select.~~ **Done
   2026-07-30: it cannot.** The result and the exact calls are in the Sales Activities section
   above. Adding choices by hand in the UI is the only path, not merely the assumed one.
7. **Fill the `Exists` column for the remaining Prospects fields** — the one item still open. `?`
   means unchecked, not absent. Date the pass when you do it.

### Standing obligation

The debt is cleared, not abolished. Any select value added to
`mark_outcome.REQUIRED_SELECT_CHOICES`, `mark_outcome.OPERATOR_REQUIRED_SELECT_CHOICES`,
`queue_state.PROSPECT_STATUS_CHOICES`, or the configured `LPM_SETTER_ROSTER` must be created in the
base by hand before that code path can run.
`airtable_required_choices.py check` is what catches it — exit 2 names the missing choice and prints
the UI steps. Field creation and non-select types (`Deals.Cash Collected`) remain manual checks.

## MVP setter-assignment cutover (manual, not yet verified live)

1. Create `Prospects.Assigned Setter` as single select and add every exact name from
   `LPM_SETTER_ROSTER`.
2. Confirm `Booked` exists in `Prospects.Status`.
3. Add `Held` to `Sales Activities.Outcome`.
4. Create `Deals.Cash Collected` as currency.
5. Run `.venv/bin/python airtable_required_choices.py check`; it must exit 0.
6. Follow [`setter-assignment-sop.md`](setter-assignment-sop.md) for the blank-only dry-run and
   backfill. Per-rep views and permissions are manual and are not verified by repository tests.
