export const meta = {
  name: 'consolidation-phases-3-4-5',
  description: 'Phases 3, 4 and 5 concurrently: CRM code swap, doc-04 descoped items, and docs truth-up',
  phases: [
    { title: 'Build', detail: 'five disjoint work-streams in parallel' },
    { title: 'Verify', detail: 'full suite + focused review against the locked decisions' },
  ],
}

const REPO = '/home/lucascoh/local-lead-machine'

const LIVE = `
LIVE AIRTABLE GROUND TRUTH — base \`appLbCDdUDSZf7qaK\`, queried directly 2026-07-29. Observed
facts, not documentation. Use them; do NOT add network calls.

  Prospects.Status            = New, Mockup Needed, Manual Review, Not Fit, Sent, Follow-Up, Closed
  Prospects.Outreach Channel  = Email, Phone, Instagram DM, SMS
  Prospects.Lead Priority     = A, B, C, D, Skip, New, "(440) 537-2459"   <- junk minted by typecast
  Prospects.Vertical          = hardscaping, landscaping, concrete, roofing, HVAC, plumbing, other,
                                epoxy, detailing, water_restoration, garage_doors, pressure_washing
  Sales Activities.Activity Type = Call, Email, Meeting, Follow Up, Demo, Note, Other
  Sales Activities.Outcome       = Completed, Scheduled, Rescheduled, Cancelled, No Show, Outcome Pending
  Deals.Deal Status           = Proposed, Deposit Pending, Deposit Paid, Contract Pending,
                                Contract Signed, In Progress, Launched, Retainer Active, Lost

  Prospects has 9 views: System, "00 - Sales Assignment", "10 - New Calls", "20 - Follow-Ups",
  "30 - Active Pipeline", "40 - Hot Opportunities", "50 - Closed Outcomes", "60 - Data Repair",
  "Grid 11". Tables: Prospects, Sales Activities, Deals, Tasks, Clients.
`

const COMMON = `
You are working in ${REPO} on branch \`main\`, HEAD 82a5716. The tree is CLEAN and GREEN:
\`.venv/bin/python -m unittest discover -s tests\` reports "Ran 779 tests ... OK".
Phases 0-2 of a consolidation are committed.

HARD RULES:
1. You are ONE of five agents editing this tree simultaneously. Edit ONLY files under "FILES YOU
   OWN". If you need another file, do everything else and report the blocked edit.
2. NEVER run a mutating git command (no add/commit/checkout/stash/restore/reset). Read-only git is
   fine. The orchestrator commits.
3. Do NOT run the full suite (others are mid-edit). Run ONLY your own targeted test modules.
4. **NEVER touch the live Airtable base.** No MCP Airtable write calls, no schema mutation, no
   record edits. This whole phase is CODE ONLY — a separate approved runbook does the base later.
   Deleting an Airtable field before the code stops writing it makes every upload 422; that is the
   single worst mistake available here, and it is one command away.
5. Never fix a problem by deleting or weakening the test that exposes it.
6. Match surrounding style. This repo's comments explain WHY a control exists, not what a line does.

Report: what changed and why, exact test names added/changed, your targeted test command and its
verbatim result, and anything blocked or judged a non-issue (with evidence).
`

phase('Build')

const JOBS = [
  {
    label: 'p3a:upload-field-swap',
    prompt: `${COMMON}
FILES YOU OWN: \`upload_airtable.py\`, \`tests/test_upload_airtable_schema.py\`,
\`tests/test_airtable_schema_cutover.py\`, \`airtable_cutover_fields_2026-07.json\`

${LIVE}

TASK — swap the field set \`upload_airtable.py\` writes, so the code stops writing the 23 fields the
runbook will later delete, and starts writing the 3 new ones.

\`CSV_AIRTABLE_FIELDS\` (\`upload_airtable.py:185\`, 51 entries) contains **13** of the doomed fields:
Form Strength, Commercial Materiality, Recurring Consequence, Root Cause Confidence, Packageable
Solution, Value Mechanism, Beyond Free Fix, Self Fix Risk, Intentionality, Asset ID,
Asset Verifier Verdict, Evidence Snapshot, Recommended Action.
Remove those 13. The other 10 doomed fields are the \`Sales — *\` block, which code never writes.

The full deletion list is 23 = the 19 in \`airtable_cutover_fields_2026-07.json\` + 4 more
\`Sales — *\` fields not yet in that file: **Assigned Rep, Batch, Booked Out?, Ask Arm**. Add those
four to the cutover config so the runbook and \`airtable_schema_cutover.py\` operate on the real set.
Note \`Sales — Consent to Text\` is a CHECKBOX carrying data that must migrate into the canonical
\`Consent Posture\` field before deletion — make sure the cutover config/tooling records that
dependency rather than treating it as a plain drop.

ADD 3 fields. Producers, which I verified exist:
  - \`ICP Lane\`            <- \`icp_lanes.classify_icp_lane(record).lane\`, surfaced as \`icp_lane\`
                              at \`make_llm_batch.py:105\`
  - \`Opportunity Score\`   <- \`.score\`, surfaced as \`icp_lane_score\` at \`make_llm_batch.py:106\`
  - \`Selected Opportunity\` <- producer UNCONFIRMED. Investigate: read
                              \`docs/notion-plans-to-consolidate/04-opportunity-scoring-visual-audit.md\`
                              and check whether anything in the repo emits it.
    **Only add a field to CSV_AIRTABLE_FIELDS if a real producer exists.** A column the writer always
    leaves blank is worse than no column. If there is no producer, say so plainly, leave it out of
    the write path, and list it as a schema-only addition for the runbook.

Check how the write path handles a field present in the list but absent from a row, and make sure
dropping 13 fields does not change behaviour for rows that still carry them.

Do NOT delete anything in Airtable.

Run \`cd ${REPO} && .venv/bin/python -m unittest tests.test_upload_airtable_schema tests.test_airtable_schema_cutover -v\`.`,
  },
  {
    label: 'p3b:status-eight-values',
    prompt: `${COMMON}
FILES YOU OWN: \`lead_machine/queue_state.py\`, \`mark_outcome.py\`, \`mark_sent.py\`,
\`tests/test_queue_state_formula.py\`, \`tests/test_mark_outcome.py\`,
\`tests/test_send_queue_scripts.py\`, \`tests/test_readiness_migration.py\`

${LIVE}

TASK — split \`Closed\` into \`Won\` and \`Lost\`, taking Status from 7 live values to 8.

LANDMINE (from the plan, must be respected): **the Status change ships with \`queue_state.py\` or
"Ready to Dial" silently changes meaning.** \`lead_machine/queue_state.py:44-48\` hardcodes
\`QUEUE_STATUS_CLAUSE\` as \`Status ∈ {New, Mockup Needed}\`, and two test modules pin it. All of this
moves as one unit.

\`mark_outcome.py\` currently maps BOTH \`closed_won\` and \`closed_lost\` to \`TARGET_STATUS = "Closed"\`,
which is exactly the information loss the split fixes. Map them to \`Won\` and \`Lost\`.

CRITICAL SEQUENCING — the new choices do NOT exist in the base yet (see ground truth: Status has 7
values, no Won, no Lost), and Phase 1 removed \`typecast\`, so Airtable will 422 on the new values
until the runbook creates them. That is expected and correct: code first, base later. Make the
failure honest and self-diagnosing rather than a bare 422, and make sure the required vocabulary is
exported so a runbook generator can read it from the code rather than from prose.
\`mark_outcome.REQUIRED_SELECT_CHOICES\` already exists for this purpose — extend it, don't invent a
parallel mechanism.

Note \`mark_outcome\` is ALREADY inoperative against the live base for a separate reason Phase 1
documented: Sales Activities is missing the \`Activity Type\` values Reply/Close and ALL FOUR
\`Outcome\` values it writes. Your change adds Status Won/Lost to that same runbook prerequisite list.
Do not try to fix the base.

Also decide and justify: does anything downstream read \`Status == "Closed"\`? Grep before you split,
and update every reader — a half-split vocabulary is worse than either whole one.

Run \`cd ${REPO} && .venv/bin/python -m unittest tests.test_queue_state_formula tests.test_mark_outcome tests.test_send_queue_scripts tests.test_readiness_migration tests.test_hermes_queue -v\`.`,
  },
  {
    label: 'p4a:visual-audit-fields',
    prompt: `${COMMON}
FILES YOU OWN: \`enrich_leads.py\`, \`lead_machine/evidence.py\`, and a NEW test file
\`tests/test_visual_audit_fields.py\`.
EXPLICITLY NOT YOURS: \`run_pipeline.py\` (another agent owns it right now).

TASK — the 7 visual-audit score fields are permanently the string \`"Unknown"\`.

\`enrich_leads.py:173\` defines \`VISUAL_AUDIT_SCORE_FIELDS\` (visual_quality_score,
premium_perception_score, mobile_visual_score, hero_clarity_score, cta_visibility_score,
gallery_visibility_score, trust_proof_visibility_score). The ONLY writer is the all-\`Unknown\`
initializer at \`enrich_leads.py:482\`. \`lead_machine/evidence.py:76\` mirrors the list and \`:362\`
reads it. So every lead carries 7 dead fields that are fed to the LLM as noise on every scoring call.

Fill them with real values derived from evidence the enricher ALREADY collects — do not add new
network calls, new page fetches, or a vision model. Read what the enricher already has (screenshots
taken, page structure, detected elements, form strength, image/gallery presence, trust markers,
mobile viewport handling, etc.) and derive each score from it.

Design constraints:
- \`"Unknown"\` must remain reachable and honest: if the underlying evidence genuinely was not
  collected for a lead (no screenshot, site did not load), the field stays Unknown rather than
  taking a fabricated default. A confident-looking score derived from nothing is worse than Unknown.
- Whatever scale you choose, it must be consistent across all 7 and legible to the LLM prompt that
  consumes them. Check how \`evidence.py:362\` and the prompt builder use them before choosing.
- State plainly in your report which of the 7 you could ground in real evidence and which you could
  not, rather than forcing all 7.

Tests: put them in the new \`tests/test_visual_audit_fields.py\` (you own it, so no collision). Cover
a lead with rich evidence, a lead whose site did not load (must stay Unknown), and the invariant
that the two \`VISUAL_AUDIT_SCORE_FIELDS\` lists in \`enrich_leads.py\` and \`lead_machine/evidence.py\`
cannot drift apart.

Run \`cd ${REPO} && .venv/bin/python -m unittest tests.test_visual_audit_fields tests.test_enrichment_throughput tests.test_dnc_enrichment -v\`.`,
  },
  {
    label: 'p4b:wire-orphan-stages',
    prompt: `${COMMON}
FILES YOU OWN: \`run_pipeline.py\`, \`collect_external_signals.py\`, \`collect_review_signals.py\`,
\`tests/test_run_pipeline.py\`
EXPLICITLY NOT YOURS: \`enrich_leads.py\` and \`places_search.py\` (another agent owns enrich_leads;
places_search was just changed in Phase 1).

TASK — two pipeline stages exist as scripts but nothing runs them.
\`collect_external_signals.py\` and \`collect_review_signals.py\` are in the repo root and are invoked
by nothing (\`grep\` shows no caller). Wire them into \`run_pipeline.py\`.

Before wiring, work out what they actually need and produce:
- What input does each expect, and at what point in the pipeline does that input exist?
- What does each write, and does anything downstream read it? If the outputs are consumed by
  nothing, wiring them in just spends time and money — say so and report it rather than wiring a
  no-op. That is a legitimate outcome of this task.
- Do they make paid API calls? If so they need the same treatment every other paid stage has: a
  \`--skip-*\` flag consistent with the existing ones, and they must not run by default in a way that
  silently changes what a batch costs.

Follow the existing stage conventions in \`run_pipeline.py\` exactly — look at how \`--skip-search\`,
\`--skip-enrich\` and the other stages are declared, guarded, ordered, and reported.

Phase 1 note so you do not undo it: \`run_pipeline.py:283-294\` shells out to \`places_search.py\` as
stage 1, and \`places_search.main()\` now loads the suppression index and fails closed before the
first paid Places call. Do not disturb that ordering.

Run \`cd ${REPO} && .venv/bin/python -m unittest tests.test_run_pipeline tests.test_run_agent_pipeline -v\`.`,
  },
  {
    label: 'p5:docs-truth-up',
    prompt: `${COMMON}
FILES YOU OWN (docs only — no .py files at all):
\`docs/phase-a-backlog.md\`, \`docs/phase-b-c-roadmap.md\`, \`docs/airtable_schema_required.md\`,
\`AGENTS.md\`, \`CLAUDE.md\`, \`.env.example\`, \`.claude/skills/add-vertical/SKILL.md\`,
\`docs/worktree-reconciliation-2026-07-11.md\`

${LIVE}

TASK — all of these carry claims the code now contradicts. Correct them against what the code
actually does. Read the code; do not trust the docs, each other, or my summary below.

KNOWN FALSEHOODS to fix (there will be more — hunt for them):

1. \`docs/airtable_schema_required.md:92-96\` says the six Sales Activities values "are added by
   Airtable \`typecast\` on first write. That is the only mechanism available — the Meta API refuses
   to add choices to an existing select." Both halves are now wrong or unverified:
   - typecast was REMOVED from every write path (a repo-wide AST test now enforces that).
   - \`mark_outcome.RUNBOOK_DOC\` points operators at THIS FILE when a write fails, so they currently
     land on advice that contradicts the code that sent them.
   - The "Meta API refuses" claim is unverified: the PAT is \`permissionLevel: create\` and Airtable
     does support ADDING (not removing) choices on an existing single-select. Mark it as needing
     verification during the runbook; do not assert either way.
   Rewrite it to say these choices must be PRE-CREATED, are not all present as of 2026-07-29, and
   name exactly which are missing: Activity Type is missing **Reply** and **Close**; Outcome is
   missing **all four** of Replied / Meeting Booked / Won / Lost.
   This file also has an "exists?" column that is \`?\` on every row — fill it from the ground truth
   above, dated, and say plainly that anything still \`?\` was not observed. Do not invent a value for
   a field not in that block. Note the live \`Lead Priority\` junk choice \`(440) 537-2459\` as
   something the runbook should delete.

2. \`.claude/skills/add-vertical/SKILL.md:40\` still says \`min_reviews: 5\`. The floor is now **8** on
   both surfaces, and \`test_every_review_floor_matches_its_own_json_profile\` will FAIL on the next
   vertical added through that skill. Also check the skill does not still reference \`max_reviews\`,
   which no longer exists as a concept.

3. \`docs/worktree-reconciliation-2026-07-11.md:61\` still calls 10 the current epoxy floor.

4. \`AGENTS.md\` and \`run_pipeline.py\`'s stage description both call \`places_search.py\` the pipeline's
   stage 1, while \`places_search.py\`'s own docstring calls itself LEGACY and "not part of the
   current pipeline". \`run_pipeline.py:289\` does still call it, so the LEGACY docstring is the wrong
   one — but you do not own that .py file. Fix the AGENTS.md side and report the .py discrepancy.

5. \`docs/phase-a-backlog.md\` is the stated plan of record (per CLAUDE.md). Reconcile its status
   claims against what phases 0-2 actually landed: git log 82a5716, c53b241, b601a92, 91c7131,
   12a853b, c24b0fa, f431285. Do not mark anything done that you cannot verify in the code.

RULES FOR THIS TASK:
- Verify every claim you write against the code, and cite file:line in the doc where it helps a
  future reader check you.
- Do NOT invent status for work that has not happened. Phases 3 and 4 are being written RIGHT NOW by
  other agents in this same run — do not describe their outcomes; leave those items alone.
- Keep each file's existing structure and voice.
- Run no tests, but DO check whether any test asserts on these files' contents
  (\`grep -rn "airtable_schema_required\\|phase-a-backlog" ${REPO}/tests\`) and report what you find.`,
  },
]

const results = await parallel(JOBS.map(j => () => agent(j.prompt, { label: j.label, phase: 'Build' })))

log(`Phases 3/4/5 returned ${results.filter(Boolean).length}/${JOBS.length}`)

phase('Verify')

const VERDICT = {
  type: 'object',
  additionalProperties: false,
  required: ['suiteGreen', 'suiteSummary', 'findings', 'residualRisks'],
  properties: {
    suiteGreen: { type: 'boolean' },
    suiteSummary: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['severity', 'file', 'summary', 'evidence'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          file: { type: 'string' },
          summary: { type: 'string' },
          evidence: { type: 'string' },
        },
      },
    },
    residualRisks: { type: 'array', items: { type: 'string' } },
  },
}

const reports = JOBS.map((j, i) => `### ${j.label}\n${results[i] || '(nothing returned)'}`).join('\n\n')

const adversary = await agent(`Verify phases 3, 4 and 5 of a consolidation in ${REPO}.
HEAD is 82a5716; all of this work is uncommitted. Baseline before it: "Ran 779 tests ... OK".

FIRST, run the full suite and report it verbatim:
\`cd ${REPO} && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -40\`
plus \`git diff --stat\` and \`git status --short\`. Report exact Ran/OK/FAILED lines and any traceback.

THEN review adversarially. Inspect the real diff; do not trust the reports below.

Check against the LOCKED decisions:
1. **NOTHING was mutated in the live Airtable base.** This was code-only. Any evidence of an
   Airtable write, schema change, or record edit is a CRITICAL finding.
2. \`upload_airtable.py\` no longer writes the 13 doomed fields, and the cutover config covers all 23
   (19 original + Assigned Rep, Batch, Booked Out?, Ask Arm). A field was added to the write path
   ONLY if a real producer exists — a column the writer always leaves blank is a finding.
   \`Sales — Consent to Text\` is a checkbox whose data must migrate to \`Consent Posture\`; confirm
   that dependency is recorded, not silently dropped.
3. Status is 8 values (Closed split into Won/Lost) and \`lead_machine/queue_state.py\` moved WITH it.
   If queue_state still hardcodes the old set, "Ready to Dial" has silently changed meaning — that
   is critical. Confirm every reader of \`Status == "Closed"\` was updated; a half-split vocabulary
   is worse than either whole one.
4. The 7 visual-audit fields are filled from evidence the enricher ALREADY had — no new network
   calls, no vision model. \`"Unknown"\` must still be reachable for a lead whose site did not load;
   a fabricated default is a finding. Check the two \`VISUAL_AUDIT_SCORE_FIELDS\` lists cannot drift.
5. The two orphan stages are either wired following \`run_pipeline.py\`'s existing stage conventions
   (skip flags, ordering, cost reporting) or explicitly reported as no-ops worth not wiring. Either
   is acceptable; a silently-wired paid stage that runs by default is not.
6. Docs now match the code. Spot-check the specific claims: the typecast section of
   \`docs/airtable_schema_required.md\`, \`min_reviews: 8\` in \`.claude/skills/add-vertical/SKILL.md\`,
   and that nothing marks phase 3/4 work as done that is not actually in the diff.

Then hunt for what broke:
- Phase 1's suppression fail-closed guards and Phase 2's ICP admission must still hold. Re-verify:
  a row carrying \`derived.hard_icp_skip=true\` must still fail \`sendability.is_contact_ready\` and
  must not get \`Packet Tier = "Sendable Packet"\`.
- Any test weakened or deleted rather than updated.
- Any file edited that no agent owned.
- Any comment, docstring or test NAME that is not true of the code.
- Any doc claim asserting an outcome that is not in the diff.

Reports (CLAIMS, not facts):

${reports}

No edits. No mutating git commands. No Airtable calls.`,
  { label: 'verify:345', phase: 'Verify', schema: VERDICT, effort: 'high' })

return { adversary, reports }
