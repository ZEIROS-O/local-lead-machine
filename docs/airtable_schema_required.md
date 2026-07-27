# Airtable Schema — Required Fields

Base of record: **Local Lead Machine CRM** (`AIRTABLE_BASE_ID`). It is the only base this repo
writes to. A second base named *Local Pro Marketing* exists in the workspace and is **not** part of
the pipeline — nothing here reads or writes it.

## `Prospects` (`AIRTABLE_PROSPECTS_TABLE`)

| Field | Airtable Type | Exists | Notes |
|-------|--------------|--------|-------|
| Business Name | Single line text | ? |  |
| Owner Name | Single line text | ? |  |
| Vertical | Single select | ? | Options: detailing, epoxy, garage_doors, hardscaping, landscaping, pressure_washing, roofing, water_restoration |
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
| Lead Priority | Single select | ? | Options: A, B, C (grade) |
| Offer Fit | Single select | ? | Options: No-Site Launch, Website Rebuild, Estimate Funnel, Automation / Follow-Up |
| Site Status | Single select | ? | Options: Missing, Broken, No Website, Loaded, Redirect Issue, Social Only, Unknown |
| Form Strength | Single line text | ? |  |
| Visible Pain Score | Number | ? | Integer 0–100 (sendability thresholds fire at 10/16/30/35/40) |
| Visible Pain Reasons | Long text | ? |  |
| Send Trigger | Single line text | ? |  |
| Buyer Intent Signals | Long text | ? | Comma-separated signals detected by LLM |
| Commercial Fit State | Single select | ? | Options: commercially-sendable, minor-fix/nurture, needs-broader-system-evidence, intentional-or-ambiguous, not-an-LPM-fit, unverified |
| Commercial Materiality | Long text | ? | Evidence-backed commercial consequence |
| Recurring Consequence | Long text | ? | Why the issue repeats or creates downstream loss |
| Root Cause Confidence | Long text | ? | Bounded connection between evidence and mechanism |
| LPM Offer Component | Single line text | ? | One authoritative offer component |
| Packageable Solution | Long text | ? | Bounded deliverable LPM can build |
| Value Mechanism | Long text | ? | Credible gain or avoided-loss mechanism |
| Beyond Free Fix | Long text | ? | Why this is broader than one free edit |
| Self Fix Risk | Long text | ? | Likelihood and scope of self-resolution |
| Intentionality | Long text | ? | Why the condition is not intentional or ambiguous |
| Contact Eligibility | Single select | ? | Options: contact-now, contact-after-asset, hold-commercial-case |
| Asset State | Single select | ? | Options: none, planned, verified-existing |
| Promise State | Single select | ? | Options: asset-free, future-build, existing-verified-asset |
| Asset ID | Single line text | ? | Verified asset identifier when present |
| Asset Verifier Verdict | Single select | ? | Options: PASS, FAIL, BLOCK |
| Evidence Refs | Long text | ? | Structured evidence identifiers |
| Safe Outreach Claims | Long text | ? | Claims bound to evidence identifiers |
| Evidence Snapshot | Long text | ? | Structured bounded evidence used by the morning verifier |
| AI Confidence | Single select | ? | Options: low, medium, high |
| Score Total | Number | ? | Integer |
| Opener | Long text | ? |  |
| Recommended Action | Single line text | ? |  |
| Send Blockers | Long text | ? |  |
| Skip Reason | Single line text | ? |  |
| Place Key | Single line text | ? | Used as upsert key; must be unique per business |
| Status | Single select | ? | Options: New, Mockup Needed, Manual Review, Not Fit, Sent, Follow-Up, Closed. `Not Fit` is applied by hand, never by code. These seven are the whole vocabulary — a choice outside it is drift, not a variant |
| Outreach Channel | Single select | ? | Options: Email, Phone, Instagram DM, SMS |
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

The field's stock choices (Completed / Scheduled / No Show / …) describe meeting logistics and
cannot express a funnel outcome, so the six values above are added by Airtable `typecast` on first
write. That is the only mechanism available — the Meta API refuses to add choices to an existing
select — so the vocabulary is deliberately **closed** and pinned by `tests/test_mark_outcome.py`.
Adding a seventh value is a schema change: update the code map, this table, and the test together.

### `Deals` (`AIRTABLE_DEALS_TABLE`, default `Deals`)

| Field | Airtable Type | Notes |
|-------|--------------|-------|
| Package | Single line text | Primary field; `--deal-package`, falling back to the business name |
| Deal Status | Single select | Created as **Deposit Pending** — the win is recorded, the money has not moved |
| Setup Price / Retainer Price | Currency | Optional, from CLI flags |
| Prospect | Link → Prospects | Set on create |
| Deposit/Contract/Invoice checkboxes, Kickoff / Launch / Retainer Start dates | — | Hand-kept |
| Clients | Link → Clients | Hand-linked at onboarding |

### `Tasks` and `Clients`

Manual tables. `Tasks` holds follow-ups linked to a prospect; `Clients` holds post-close hosting and
retainer state, linked to both the Prospect and the Deal. **No code writes either one** — first-close
onboarding is a human step by design (see AGENTS.md Gate 0), and the schema exists so that step has
somewhere to land.
