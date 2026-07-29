# Airtable CRM rebuild — LPM caller/setter operating surface

> Local snapshot copied from Notion on 2026-07-28T13:20:31.289Z.  
> Source: [Airtable CRM rebuild — LPM caller/setter operating surface](https://app.notion.com/p/3abc57f0654f804688bfc2c3ee20e511)

## Context
The Airtable base `Local Lead Machine CRM` (`appLbCDdUDSZf7qaK`) is a lead-scoring output store, not a  
CRM. A review on 2026-07-28 found: 132 prospects, 0 real activities, 0 deals, 0 clients; `Last Contacted` filled on 1 record of 132; nothing ever marked `Sent`. Meanwhile `master-operating-plan.md`  
names the binding constraint as *contacts going out*, managed by a **daily contact counter visible in  
week 2**, and `LPM-business-plan.md` §8.3 states plainly that without a `Held?` field "none of the  
funnel math is computable." Neither number has a home in the base today.
Concretely, the base cannot answer: how many owners did we reach yesterday, what is the contact rate  
by trade or by hour, how many calls were held, which ask arm is winning, what is churn. It also  
actively misleads — 44 records read `Sendable Packet` in the grid while only **3** pass the live  
`query_ready_to_send.py` formula — and it carries \~310 KB of machine JSON (`Evidence Snapshot`,  
avg 16,325 chars/cell) that is dragged into every morning-queue API call.
Outcome intended: one base that a paid caller and an SMS setter can work from directly, that produces  
every metric the two plans manage against, that stays in lockstep with `local-lead-machine`'s write  
path and `operator-agent`'s outcome ledger, and that is materially smaller and faster.
### Decisions taken (2026-07-28)
<table header-row="true">
<tr>
<td>Question</td>
<td>Decision</td>
</tr>
<tr>
<td>Setter access</td>
<td>**Free editor collaborators** on the base → Interface Designer is available; build real cockpit + dashboard pages</td>
</tr>
<tr>
<td>Out-of-ICP leads (56 detailing, 31 epoxy)</td>
<td>**Keep.** Valid leads for a different offer. Segment via a new `Trade Tier` + `ICP Lane`, do not archive</td>
</tr>
<tr>
<td>Split-test arms (4 openers × 3 asks)</td>
<td>**Collapse to one variable.** One generic `Split Test` + `Arm` pair mirroring the ledger's keys</td>
</tr>
<tr>
<td>\~24 dead/low-value columns</td>
<td>**Backup then delete**, via the existing `airtable_schema_cutover.py` flow</td>
</tr>
</table>
### The two facts that shape the architecture
1. **`operator-agent`**** already owns the funnel vocabulary.** `scripts/append_outcome.py` defines  
`VALID_EVENTS` = `sent · opened · replied_pos · replied_neg · call_booked · call_held · deposit · opt_out · gate_block · gate_accept`, mandates `catalyst` / `pre_handle_objection` /  
`died_at_stage` on `call_held`, and enforces `split_test`+`arm` assigned before the attempt.  
`STATE/README.md` mandates a **dual write** — Airtable first, then the ledger — and says the two  
never read each other. A setter cannot run two CLIs, so **the bridge is the central piece of work**.  
Design rule: make Airtable's event vocabulary *identical* to `VALID_EVENTS` so the bridge is a  
field-for-field copy with no translation table that can drift.
2. **`STATE/outcomes.jsonl`**** is the designated authority for rates, arms and kill criteria** — not  
Airtable. That resolves the record-volume problem: Airtable holds CRM state and a hot window of  
activity; the ledger holds every attempt forever and feeds `feedback_report.py`.
---
## Phase 0 — Preflight (read-only, blocking)
1. Confirm the other base `appJrlU8S2RaNx49j` (`Local Pro Marketing`) is deleted. It holds an  
**`Inbound Requests`** table (form intake) and a **`Mockups`** table with the only  
`multipleAttachments` field in either base — neither exists in the live base. Both are rebuilt in  
Phase 1; confirm nothing else is wanted before it goes.
2. **Confirm the workspace plan's per-base record cap and monthly automation-run quota.** The design  
below is built to survive a low cap (activity rows are pruned to the ledger, automations kept to  
one), but the numbers decide the prune window.
3. Verify the Airtable MCP can write schema (`create_field` / `update_field` / `create_table`). A  
prior attempt to add select choices via the Meta API was blocked by PAT scope. If MCP schema  
writes fail too, every "add field" step below becomes a UI checklist for Lou — the plan is  
otherwise unchanged.
4. Snapshot: `.venv/bin/python airtable_schema_cutover.py prepare` — writes a JSON backup + checklist.  
`airtable_schema_cutover.py:24` currently hardcodes `LEGACY_FIELD_PAIRS`; generalise it to accept a  
field list (CLI `-fields` or a JSON config) so it can back up this cutover's set. Keep  
`prepare` / `verify` read-only against Airtable — that property is why this tool is being reused.
---
## Phase 1 — Schema: expand (non-destructive)
### 1a. `Prospects` — new fields
<table header-row="true">
<tr>
<td>Field</td>
<td>Type</td>
<td>Purpose</td>
</tr>
<tr>
<td>`Queue State`</td>
<td>formula</td>
<td>Mirrors `query_ready_to_send.build_formula` exactly. Emits `Ready to Dial` or `Blocked: <first failing clause>`. **Fixes the 44-vs-3 lie.**</td>
</tr>
<tr>
<td>`ICP Lane`</td>
<td>singleSelect</td>
<td>The 8 values of `lead_machine.icp_lanes.LANES` — already computed, never surfaced</td>
</tr>
<tr>
<td>`Trade Tier`</td>
<td>singleSelect</td>
<td>`Tier 1` / `Tier 2` / `Adjacent` — derived from vertical. Lets detailing/epoxy stay in place without diluting Tier-1 views</td>
</tr>
<tr>
<td>`Leak #1`</td>
<td>singleSelect</td>
<td>The 9-item ranked taxonomy from `LPM-business-plan.md` §4. The caller's 20-second read</td>
</tr>
<tr>
<td>`Evidence Screenshots`</td>
<td>multipleAttachments</td>
<td>**There is currently no attachment field anywhere in the base**, which blocks the #1 build item in both plans (evidence pre-rendering)</td>
</tr>
<tr>
<td>`Consent Captured At`</td>
<td>dateTime</td>
<td>Timestamped proof beside the posture</td>
</tr>
<tr>
<td>`Consent Note`</td>
<td>singleLineText</td>
<td>What he actually said, verbatim</td>
</tr>
<tr>
<td>`Do Not Contact`</td>
<td>checkbox</td>
<td>Immutable suppression flag. A *stage* gets overwritten by the next batch write; this must not</td>
</tr>
<tr>
<td>`Attempts` · `Last Attempt` · `Ever Reached` · `Ever Held`</td>
<td>rollups</td>
<td>From `Sales Activities`. Replaces the hand-kept, permanently-empty `Sales — Attempts` / `Sales — Last Activity`</td>
</tr>
<tr>
<td>`Split Test` · `Arm` · `Arm Preferred`</td>
<td>text + 2 selects</td>
<td>1:1 with the ledger's keys. Generic, so whatever `STATE/split-tests.md` registers is what the field carries</td>
</tr>
</table>
### 1b. `Prospects` — modified fields
- **`Status`**** becomes the single pipeline field.** Today `Status` (7 machine values) and `Sales — Stage`  
(8 human values) shadow each other, which is what produced the current mess. Extend `Status` to:  
`New · Manual Review · Mockup Needed · Not Fit · Sent · Follow-Up · Booked · Held · Proposal · Won · Lost · Do Not Contact`, retire `Sales — Stage`, and Kanban on `Status`. `Closed` is replaced by  
`Won`/`Lost` — "Closed" cannot express which.
- **`Consent Posture`****: singleLineText → singleSelect**, choices pinned to  
`compliance.ACCEPTABLE_SMS_CONSENT_POSTURES` (`documented opt in`, `express written consent`,  
`prior express written consent`) plus `no consent on file`. Keeps `has_acceptable_sms_consent`  
(`lead_machine/compliance.py:31`) working unchanged while making the setter's choice unambiguous.
- **Drop-and-rename the ****`Sales — `**** prefix**: `Assigned Rep`, `Batch`, `Booked Out?`, `Notes`  
(merged into the existing `Notes`), `Last Modified`.
- **Clean 7 junk select choices** created by a historical column-shift write:  
`Lead Priority: (440) 537-2459` · `AI Confidence: No-Site Launch` · `Lead Source: 50, Medium` ·  
`Site Status: Audit site / booking flow` · `Offer Fit: Not Fit, Mediocre`.
### 1c. `Sales Activities` → the unified event log
This table is reshaped rather than duplicated — one activity table, not two. Its vocabulary becomes  
`append_outcome.VALID_EVENTS` verbatim.
<table header-row="true">
<tr>
<td>Field</td>
<td>Type</td>
<td>Notes</td>
</tr>
<tr>
<td>`Event`</td>
<td>singleSelect</td>
<td>**Exactly** `append_outcome.VALID_EVENTS`. Replaces the stock meeting-logistics `Activity Type`/`Outcome` pair</td>
</tr>
<tr>
<td>`Channel`</td>
<td>singleSelect</td>
<td>Phone · SMS · Email · Instagram DM</td>
</tr>
<tr>
<td>`Occurred At`</td>
<td>dateTime</td>
<td></td>
</tr>
<tr>
<td>`Caller`</td>
<td>singleSelect</td>
<td>Michael · Lou · …</td>
</tr>
<tr>
<td>`Reached Owner?` · `Held?`</td>
<td>checkbox</td>
<td>`Held?` = 10+ min with the owner, per the anti-gaming rule</td>
</tr>
<tr>
<td>`Duration (min)`</td>
<td>number</td>
<td>Feeds the by-hour and hold-rate analysis</td>
</tr>
<tr>
<td>`DQ Reason`</td>
<td>singleSelect</td>
<td>`Booked solid · No leak found · Not the owner / no path · Franchise or PE · Wrong business · Other`</td>
</tr>
<tr>
<td>`Catalyst` · `Pre-Handle Objection` · `Died At Stage`</td>
<td>long text ×2, number</td>
<td>**Mandatory on ****`call_held`** per `STATE/README.md`. Validated by the sync script, not by Airtable</td>
</tr>
<tr>
<td>`Split Test` · `Arm` · `Arm Preferred`</td>
<td>text + 2 selects</td>
<td>Copied down from the Prospect at attempt time</td>
</tr>
<tr>
<td>`Synced to Ledger` · `Ledger TS`</td>
<td>checkbox, dateTime</td>
<td>Idempotency guard for the bridge</td>
</tr>
<tr>
<td>`Linked Prospect`</td>
<td>link</td>
<td>Existing</td>
</tr>
</table>
Existing `Activity Type` / `Outcome` are retired in Phase 4. The single stray row  
(`rectip5LjOAjxOTNm`, Note/No Show, no name or date) is a test artifact — delete it.
### 1d. `Deals` and `Clients` — make the governing formulas computable
`Deals` today has checkboxes but no amounts or dates, so *"revenue counts only when collected; MRR  
only when charged"* (`LPM-business-plan.md` §11) is uncomputable. Add:  
`Closed Won Date` · `Amount Collected` (currency) · `Collected Date` · `MRR Charged` (currency) ·  
`MRR Start` · `Churn Date` · `Churn Reason`.
`Clients` cannot express churn at all, so *steady-state clients = monthly closes ÷ monthly churn* is  
uncomputable. Add: `Client Status` (`Active · Paused · Churned`) · `Churn Date` · `Churn Reason` ·  
`Operator Hours / Week` (number — the `>5 hrs/wk` kill rule). Delete the 4 empty stub rows across  
`Deals` / `Tasks` / `Clients`.
### 1e. New table: `Inbound Requests`
Rebuilt from the deleted base, plus a **public Airtable Form** as its intake. This is the declared-pain  
/ inbound channel in `master-operating-plan.md` §5.3 and it currently has nowhere to land.
**Deferred, not built now:** a `Territories` table for one-contractor-per-trade-per-market exclusivity.  
It is the strongest closing lever in the plan but premature at zero clients — revisit at client #1.
---
## Phase 2 — Code: switch the write path
Order matters. Airtable 422s on an unknown field name, so code changes land **before** Phase 4's  
deletions. `query_ready_to_send.py:~120` degrades gracefully on unknown fields (drops with a warning)  
— convenient, but it means a missed update silently hollows out the morning-queue digest rather than  
failing loudly. Update it deliberately.
<table header-row="true">
<tr>
<td>File</td>
<td>Change</td>
</tr>
<tr>
<td>`upload_airtable.py`</td>
<td>`CSV_AIRTABLE_FIELDS` (line 185) and `_FIELD_SCHEMA` (line 50): remove the 8 commercial-case narrative fields, `Evidence Snapshot`, `Form Strength`, `Recommended Action`, `Asset ID`, `Asset Verifier Verdict`; add `ICP Lane`, `Trade Tier`, `Leak #1`</td>
</tr>
<tr>
<td>`query_ready_to_send.py`</td>
<td>`DISPLAY_FIELDS` (line 33): same removals. **Dropping ****`Evidence Snapshot`**** alone removes \~16 KB per record from every morning-queue call.** Point `build_formula` at the shared constant below</td>
</tr>
<tr>
<td>**new** `lead_machine/queue_state.py`</td>
<td>Single source for the readiness expression: one constant consumed by `query_ready_to_send.build_formula` *and* by a generator that emits the `Queue State` field formula. Without this the contract exists in three places (`sendability.contact_readiness_failures`, the API formula, the Airtable field) and will drift</td>
</tr>
<tr>
<td>`mark_outcome.py`</td>
<td>Extend `TARGET_STATUS` (line 54) for the new `Status` vocabulary (`meeting_booked → Booked`, `closed_won → Won`, `closed_lost → Lost`); replace `ACTIVITY_TYPE`/`ACTIVITY_OUTCOME` (lines 61–72) with the `Event` map; **remove ****`typecast: True`**** (line 191)** — choices are pre-created, and typecast is the exact mechanism that turned a column-shift bug into `Lead Priority: (440) 537-2459`; write `Closed Won Date` on a win</td>
</tr>
<tr>
<td>`mark_sent.py`</td>
<td>Also create a `Sales Activities` row with `Event=sent`, so the daily counter counts sends and not only outcomes. Reuse `lead_machine.follow_up.next_follow_up_date` as-is</td>
</tr>
<tr>
<td>`lead_machine/icp_lanes.py`</td>
<td>No logic change — `classify_icp_lane` already returns the lane. Wire its output into the card dict that `upload_airtable` reads so `ICP Lane` populates</td>
</tr>
<tr>
<td>**new** `sync_outcomes.py`</td>
<td>The bridge — see Phase 5</td>
</tr>
</table>
**Tests** (stdlib `unittest`, run `.venv/bin/python -m unittest discover -s tests`):  
extend `tests/test_upload_airtable_schema.py`, `tests/test_mark_outcome.py`,  
`tests/test_send_queue_scripts.py`; add `tests/test_queue_state_formula.py` (pins the Airtable  
formula against `sendability.contact_readiness_failures` — the anti-drift test) and  
`tests/test_sync_outcomes.py`.
---
## Phase 3 — Data cleanup and backfill
- **23 records have no ****`Place Key`** (15 epoxy, 8 detailing) — the upsert key.  
`upload_airtable.py:473` falls back to `Phone`, so a re-run risks duplicates. Backfill from  
`Google Maps URL`; reuse `dedupe_places.py` / `lead_machine.airtable_sync.card_place_key`.
- **`City`**** is free text** with 60+ values and duplicate pairs (`Mentor` / `Mentor, OH`,  
`Cleveland` / `Cleveland, OH`, one `OH 44118`). Normalise; convert to singleSelect afterwards.
- **`Metro Context`**** mixes granularity**: `ohio` (80) vs `cleveland` (37) vs `columbus` (6).  
"Ohio" is not a metro. Re-derive via the existing `infer_market` / `resolve_profile` +  
`market_profiles/*.json` (per `AGENTS.md`).
- **`Vertical`**: 12 choices, 4 in use, `plumbing` live but undocumented, `concrete`/`HVAC`/`other`  
unused. Prune to what `lead_machine.verticals` actually defines.
- Backfill `ICP Lane`, `Trade Tier`, `Leak #1` across all 132 records from existing scored data.
---
## Phase 4 — Contract: delete
Run `airtable_schema_cutover.py prepare` → delete → `verify`. Removed from `Prospects`:
- 8 commercial-case narrative fields (`Commercial Materiality`, `Recurring Consequence`,  
`Root Cause Confidence`, `Packageable Solution`, `Value Mechanism`, `Beyond Free Fix`,  
`Self Fix Risk`, `Intentionality`) — 19/132 filled, LLM reasoning trace, no operator reads them.  
They stay in the packet file that `Packet Path` already points at.
- `Evidence Snapshot` — 310 KB across 19 cells, max 53,639 chars.
- `Form Strength`, `Recommended Action` (duplicated by `Send Trigger` + `Opener`),  
`Asset ID`, `Asset Verifier Verdict` (both 0/132), `Sales — Stage`, `Sales — Attempts`,  
`Sales — Last Activity`, `Sales — Opener Arm`, `Sales — Consent to Text`, `Sales — Notes`.
**Kept despite low fill, deliberately:** `Evidence Refs` (the sendability floor asserts ≥2 refs),  
`Asset State` / `Promise State` (read by `sendability.evaluate_commercial_fit`), `AI Confidence`  
(confidence floor), `Mockup URL` / `Video URL` (Loop B will write them), `Owner Name` (0/132 —  
this is an *enrichment gap*, not a dead column; the ICP's core assumption is "owner answers his own  
phone" and we have his name nowhere).
Also delete the junk views `temp`, `Grid 11`, and consolidate the `00`–`60` numbered set.
---
## Phase 5 — The ledger bridge (`sync_outcomes.py`)
New script in `local-lead-machine`. Reads `Sales Activities` rows where `Synced to Ledger` is false,  
and for each one **shells out to ****`$OPERATOR_REPO/scripts/append_outcome.py`** rather than  
reimplementing the row builder — that script owns validation (mandatory `call_held` keys, the  
`opt_out`-needs-an-identifier rule, arm-before-attempt) and `STATE/README.md` says a hand-appended  
row is the exact failure mode it exists to prevent. On success, PATCH `Synced to Ledger` + `Ledger TS`.
- `-dry-run` prints the rows it would append and appends nothing.
- Idempotency rests on the checkbox. A crash between append and PATCH duplicates one row on re-run;  
mitigate with a `-reconcile` mode comparing Airtable synced-counts against ledger line counts by  
`lead_id`.
- **Opt-out path, the one that carries legal weight:** setter ticks `Do Not Contact` and logs an  
`opt_out` activity → sync writes an `opt_out` row carrying `email`/`phone` → that row *is*`SUPPRESSION_SOURCE`, so enrichment and scoring fail closed against it from the next run.  
Today there is no path at all from an Airtable click to the suppression ledger. Note the sharp  
edge documented in `append_outcome.py:20`: an `opt_out` row lacking a contact identifier takes down  
search, enrichment *and* scoring — the sync must never emit one.
- Because the ledger is permanent and Airtable is not, add a documented prune step: delete  
`Synced to Ledger` activity rows older than the window chosen in Phase 0, after `-reconcile` passes.
**Automation (exactly one, to respect the free-tier run quota):** when a `Sales Activities` row is  
created with `Event = opt_out`, set the linked Prospect's `Do Not Contact` and `Status = Do Not Contact`. Everything else is formulas and rollups, which are free.
---
## Phase 6 — The operating surface
### Views on `Prospects`
<table header-row="true">
<tr>
<td>View</td>
<td>Definition</td>
</tr>
<tr>
<td>`🔥 Call Queue`</td>
<td>`Queue State = Ready to Dial`, `Do Not Contact` unchecked; sorted Lead Priority → Score Total; grouped by Vertical. Shows only the caller's 12 fields</td>
</tr>
<tr>
<td>`🚫 Blocked`</td>
<td>`Queue State` starts with `Blocked`, grouped by reason — the fix-it queue, and the honest answer to "why is my queue 3 leads"</td>
</tr>
<tr>
<td>`📅 Follow-Ups Due`</td>
<td>`Next Follow-Up` ≤ today, `Status` in Sent/Follow-Up/Booked</td>
</tr>
<tr>
<td>`📊 Pipeline`</td>
<td>Kanban by `Status`</td>
</tr>
<tr>
<td>`🧪 Manual Review`</td>
<td>`Packet Tier = Manual Review Packet`</td>
</tr>
<tr>
<td>`⚠️ Data Repair`</td>
<td>Formula flags: missing Place Key, un-normalised City, junk select values</td>
</tr>
</table>
### Interfaces (free editor collaborators make these viable)
1. **Call Cockpit** — record-detail page for Michael and the setters. Above the fold: business, phone,  
`Leak #1`, `Evidence Screenshots`, `Specific Observation`, `Opener`, `Safe Outreach Claims`,  
`Booked Out?`. Below: the linked `Sales Activities` list with inline add — which logs an attempt  
natively, no automation run consumed. This is the "2 min → 20 sec" surface both plans rank as the  
highest-ROI change in the system.
2. **Daily Counter** — one dashboard, four Big Numbers: activities logged today · owners reached ·  
calls held · calls booked. This is *"a visible counter reading 3 by 10am"*  
(`master-operating-plan.md` §13) and it is the single most important thing this rebuild delivers.
3. **Funnel & Kill Criteria** — contact rate per lead, book rate of reached, hold rate, close rate on  
held, booked-solid rate by trade, each next to its kill threshold from `LPM-business-plan.md` §13.
### SMS setter guardrail
The rails allow no text without a spoken yes on a live call. The `Call Queue` view must therefore  
**not** expose SMS as a channel; a separate `📱 SMS Eligible` view filters on  
`Consent Posture` ∈ the three accepted values **and** `Consent Captured At` present, so the setter  
physically cannot pick an unconsented number.
---
## Phase 7 — Documentation
- `docs/airtable_schema_required.md` — full rewrite. It is currently stale in both directions: it  
warns about `Sales — Outcome` and `Sales — Next Follow-Up` shadow columns that **do not exist**, and  
omits `plumbing`, `Sales — Batch`, `Sales — Attempts`, `Sales — Last Activity` which do.
- `AGENTS.md` — the write-back section, for the new activity contract and `sync_outcomes.py`.
- `operator-agent/SKILLS/send-queue/SKILL.md` §3 — describes the query as `Sendable=Yes + Packet Eligible=Yes + Status not in {Sent, Closed}`, which has not been the formula for some time.
- `operator-agent/config/routes.yaml` — register `sync_outcomes` on `lpm.pipeline` (the only route  
permitted `pipeline_live_sync`), then regenerate the operator contracts.
- `operator-agent/STATE/split-tests.md` — reconcile the one-variable decision. **Flag at  
implementation:** the register's TEST 1 tests the *cold-open shape*, while `LPM-business-plan.md`  
§11 says fix the opener and test only the ask. Both cannot be the one variable; the pre-registration  
rule ("arms fixed before data is collected") argues for keeping TEST 1 and deferring the ask test.  
Lou's call — the generic `Split Test`/`Arm` fields work either way.
---
## Verification
1. `.venv/bin/python -m unittest discover -s tests` — all green, including  
`tests/test_icp_lanes` and `tests/test_lead_machine_contracts` per `AGENTS.md`.
2. `.venv/bin/python airtable_schema_cutover.py verify <snapshot>` — 132 records survive, only the  
intended fields gone.
3. `.venv/bin/python query_ready_to_send.py --vertical "water restoration"` — runs clean with no  
`[WARN] … failed defensive commercial readiness check` lines, and returns a payload measurably  
smaller than today's (`Evidence Snapshot` gone).
4. **The 44-vs-3 check:** count of the `🔥 Call Queue` view == count returned by  
`query_ready_to_send.py` with no `-vertical`. These must be equal; today they are 44 and 3.
5. End-to-end on one real lead: log an attempt in the Call Cockpit → `Attempts` rollup increments →  
`sync_outcomes.py --dry-run` shows the ledger row → run it → confirm the line in  
`operator-agent/STATE/outcomes.jsonl` and `Synced to Ledger` ticked.
6. **Opt-out drill:** tick `Do Not Contact`, log `opt_out` with a phone, sync, then re-run enrichment  
and confirm the lead is suppressed via `lead_machine.compliance.load_configured_suppression_index`.
7. `mark_sent.py` then `mark_outcome.py --event meeting_booked` on a test record → `Status` reaches  
`Booked`, an activity row exists with `Event=call_booked`, and **no new select choice was created**  
(the typecast regression check).
8. Open the Daily Counter interface as a non-owner collaborator and confirm the four numbers render.
## Rollback
Phases 1–3 are additive and reversible. Phase 4 is the only destructive step and is gated on the  
`airtable_schema_cutover.py prepare` snapshot; restoring means re-creating the fields and replaying  
the JSON backup. Code changes are one branch off `fix/repo-remediation`, kept as independently  
revertible commits per phase.
---
## Resume checkpoint — paused 2026-07-28
The user intentionally paused execution after Tasks 1–2. Resume with the  
subagent-driven-development workflow from **Task 3**; do not repeat reviewed work.
### Exact repository state
- Repo: `/home/lucascoh/local-lead-machine`
- Branch: `feat/airtable-crm-rebuild`
- Branch base: `9257cc38df4fca5d1149593d42fc34f2013f0a7b`
- Current reviewed HEAD: `cfa5bed`
- Worktree is clean except for the user's unrelated untracked file  
`search_configs/garage_doors_cleveland_medium_002.json`. Preserve it and never  
include it in this plan's commits.
- Do not push. No live Airtable schema/data mutation has been performed.
The durable SDD workspace is:  
`.superpowers/sdd/logical-snuggling-book/`
Read these before resuming:
1. `.superpowers/sdd/logical-snuggling-book/progress.md`
2. `.superpowers/sdd/logical-snuggling-book/task-3-brief.md`
3. This checkpoint
The seven task briefs already exist. Tasks 1–2 have reports and review packages;  
Task 3 has **no report** because its implementer was interrupted before writing code.
### Completed and independently reviewed
#### Task 1 — generalized Airtable cutover field lists
Commits:
- `ad7d439` — `feat: generalize Airtable schema cutover fields`
- `2517659` — `fix: reject empty Airtable cutover fields`
Delivered:
- `airtable_schema_cutover.py` accepts comma-separated `-fields` or a JSON  
`-fields-config`, while preserving the legacy no-override behavior.
- The selected cutover is stored in the snapshot, so later `verify` checks the  
exact prepared field set; older snapshots fall back to the legacy default.
- `airtable_cutover_fields_2026-07.json` contains the exact 19 Phase 4 Prospects  
fields.
- `prepare` and `verify` remain GET-only against Airtable.
- Explicit `-fields ""` now refuses before any Airtable request instead of  
silently falling back to the legacy default.
Review outcome: clean after one fix round. Focused test module: 25/25 green.
#### Task 2 — centralized queue readiness and Queue State formula
Commits:
- `96ac828` — `feat(crm): centralize Airtable queue readiness`
- `cfa5bed` — `fix(crm): align Queue State status gate`
Delivered:
- New `lead_machine/queue_state.py` is the shared base-facing clause source.
- `query_ready_to_send.build_formula`, the generated Airtable `Queue State`  
formula, its Python evaluator, and `sendability.contact_readiness_failures`  
consume shared readiness metadata.
- The static queue contract is:  
`Packet Tier = Sendable Packet`, `Commercial Fit State = commercially-sendable`,  
`Contact Eligibility = contact-now`, then  
`Status in {New, Mockup Needed}`.
- Queue State emits `Ready to Dial` or  
`Blocked: <first failing clause>`. Readiness failures are evaluated before  
the status failure. Optional `Vertical` remains a runtime query filter and  
is intentionally absent from the static Airtable formula.
- Python still independently checks source-row `Sendable` and  
`Packet Eligible` values fail-closed.
- The 13 retired/machine-heavy fields, including `Evidence Snapshot`, were  
removed from `query_ready_to_send.DISPLAY_FIELDS`.
- Unknown optional display fields still degrade without crashing, but the  
warning now explicitly identifies all missing expected fields and the  
Airtable schema mismatch.
- Anti-drift tests use hand-authored literal expected reasons rather than  
deriving expected results from production code.
Review outcome: clean after one fix round. Queue/send tests: 34 green;  
`tests.test_lead_machine_contracts`: 172 green.
### Resume at Task 3 — current blocker
Task 3 was dispatched from commit `cfa5bed` and interrupted before any source  
change, report, or commit. Its brief requires the exact nine `Leak #1` taxonomy  
strings from `~/operator-agent/LPM-business-plan.md` section 4.
Current evidence:
- `/home/lucascoh/operator-agent/LPM-business-plan.md` does not exist.
- Searching the current `operator-agent` worktree and its git history did not  
find that file.
- Searching `local-lead-machine/docs` and `operator-agent` did not find an  
equivalent authoritative nine-item taxonomy.
**Do not invent the nine strings.** Before implementing Task 3, locate the  
current authoritative business-plan artifact elsewhere in the workspace/vault,  
or ask the user for the exact taxonomy/source. The trade-tier mapping may still  
be researched independently, but do not commit a partial Task 3 unless the brief  
can be satisfied as one reviewed unit.
Once the taxonomy is resolved, resume Task 3 from:  
`.superpowers/sdd/logical-snuggling-book/task-3-brief.md`  
with base `cfa5bed`, strict TDD, a fresh implementer, task review, and the usual  
fix/re-review loop.
### Remaining task order
1. Task 3 — `upload_airtable.py` field set plus reusable `ICP Lane`,  
`Trade Tier`, and `Leak #1` derivations.
2. Task 4 — update `mark_outcome.py` / `mark_sent.py` status and unified  
Sales Activities event contract.
3. Task 5 — add `sync_outcomes.py`, including dry-run, reconcile, fail-closed  
`call_held`/`opt_out`, and 30-day prune gating.
4. Task 6 — add `backfill_crm_fields.py`, importing Task 3 derivations rather  
than duplicating them.
5. Task 7 — rewrite Airtable docs/runbook and update `operator-agent` on its  
own `feat/crm-rebuild-contract` branch without pushing.
6. Repair the pre-existing test-isolation defect described below.
7. Run the required whole-branch review, verification-before-completion, and  
finishing-a-development-branch workflow.
### Known full-suite failure and established root cause
The exact full-suite command currently fails only these three tests in  
`tests/test_dnc_enrichment.py`:
- `EntryPointTests.test_make_search_batch_main_fails_closed_before_paying_for_search`
- `EntryPointTests.test_enrich_leads_reports_a_missing_source_as_an_error_not_a_traceback`
- `EntryPointTests.test_score_leads_fails_closed_without_a_suppression_source`
Observed after Task 1: 694 tests, 3 failures.  
Observed after Task 2: 703 tests, the same 3 failures.
Root cause was isolated read-only:
- `EntryPointTests.setUp` clears `os.environ`.
- Each production entry point correctly calls `load_dotenv()`.
- The repo's real `.env` contains  
`SUPPRESSION_SOURCE=/home/lucascoh/operator-agent/STATE/outcomes.jsonl`.
- `load_dotenv()` repopulates that value, so the tests no longer represent  
“no suppression source” and incorrectly observe exit code 0.
- Patching `make_search_batch.load_dotenv`, `enrich_leads.load_dotenv`, and  
`score_leads.load_dotenv` to no-op for those three tests made all three pass.
This is test isolation, not a production compliance defect. Preserve production  
dotenv loading and fail-closed suppression. Fix the tests with TDD in a separate,  
scoped commit before final verification; do not weaken or bypass compliance.
### Airtable/operator constraints already established
- The `airtable-mcp` CLI is not installed in this environment, matching the  
saved Task 7 decision to provide a precise manual Airtable schema/UI runbook.
- No live Phase 1 schema, Phase 3 backfill, Phase 4 deletion, views, interfaces,  
or automation work has been performed.
- Phase 4 remains destructive and must be gated on the Task 1 snapshot/config.
- The unresolved split-test decision remains Lou's call:  
existing operator TEST 1 tests opener shape, while the referenced business  
plan says test only the ask. Keep generic `Split Test` / `Arm` fields; do not  
decide the register conflict automatically.

