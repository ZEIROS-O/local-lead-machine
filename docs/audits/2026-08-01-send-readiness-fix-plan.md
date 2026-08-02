# Fix plan — send-readiness audit + CRM capture review

Execution plan for two reviews that answer different questions:

- **[`2026-08-01-send-readiness-audit.md`](2026-08-01-send-readiness-audit.md)** — 23 verified
  findings on whether the machinery is *correct*. Becomes **Track A**.
- **[`2026-07-30-crm-capture-review.md`](2026-07-30-crm-capture-review.md)** — argues the
  machinery is not the constraint; the feedback channel is a zero-byte file. Becomes **Track B**.

They are almost entirely disjoint. Exactly one finding overlaps (`run_pipeline` stage 1).

## Context

The discovery half — Places → enrich → score → gate → packet → Airtable — is real, tested (961
tests) and defensible. The Verdict collapse is provably decision-neutral. Everything after
"packet lands in Airtable" has never been used once: 145 rows, zero `Sent`/`Follow-Up`/`Won`/
`Lost`, one `Last Contacted` against 32 phone touches, a zero-byte outcomes ledger.

Track A fixes what is broken. Track B fixes what is unmeasured. Track A's sequencing principle is
the audit's own — **fix the things that make the system lie to you first** — and Track B's is the
review's: **capture fails on a laptop CLI and succeeds on a phone.**

### The correction that reshaped this plan

`evals/accept_rate.py` **cannot return a number**, and repointing it off the deleted `Packet Tier`
column will not change that. 12 rows carry `Verdict=send`; every one is `Status=New`. Zero decided
proposals against a `MINIMUM_DECIDED` of 20 → `insufficient-sample`. The 3.8% in the audit was
fixture-shaped and never described this base.

Fixing the eval is still right — it is a live 422 today. Expecting a KPI out of it is not. **The
accept-rate KPI is a Track B deliverable, not a Track A one.**

### Decisions taken

| Question | Decision |
|---|---|
| `gates.yml` [L-1] | Split — base-reading gates to `ubuntu-latest`, corpus gates to a local `make gates` |
| Initial `Status` | Keep POSTing `New`. Leave `Mockup Needed` dead — a rep-texts-Hermes mockup flow will replace it |
| `backfill_packet_tier.py` | Delete |
| `scored_orphans` | Fail only on send/review-tier orphans; count-and-report reject/watch |
| `ANGLE_BLOCKER` | Delete |
| Eval corpus | `git mv` the 12 obsolete CSVs to `output/archive/` |

**Assumption to correct if wrong:** "the stale evidence is fine" is read as *keep `stale_evidence`
a failing category and clear it by rescoring* — not *suppress it*.

### Open conflicts — decide before the affected batch ships

1. **Who writes `Status`.** Track A Batch 1 makes `mark_sent`/`mark_outcome` the only writers.
   Track B's Airtable automation (Sales Activities row → stamp the Prospect) would make three.
   The review's own rule — "pick #3 or the inverse, never both, they'd loop" — now has a third
   party in it. **Blocks B3.**
2. **`FOLLOW_UP_CADENCE_DAYS` duplication.** An Airtable automation computing `today + 4` is a
   second implementation of `lead_machine/follow_up.py`. Either park the number in the base and
   have both read it, or accept the duplication and record it in the schema doc.
3. **Nothing that scores or gates goes in Airtable.** `sendability.admit()` is the only place a
   lead may be declared sendable. Track A Batch 6 makes `readiness_flags` fail closed; a formula
   recomputing sendability would be a fourth copy of the gate.

---

# Run split

Cut at the decision points, not at batch boundaries.

| Run | Contents | Why grouped |
|---|---|---|
| **A** | Track A Batch 0 + 1 | Small, narrow, and Batch 1 is the prerequisite for B2's backfill. Closes the only bug that can double-contact a prospect |
| **B** | Track A Batch 2 + 5 + 7 | Everything provable by `unittest discover`. No live base, no money, no judgment. The one run that earns a long hands-off session |
| **C** | Track A Batch 3 + 4 + 6, and all of Track B | Needs the base, your GitHub account, your money, your judgment. One batch per sitting |

**Replan between runs.** Run A's result changes what Run B and C should be — in particular
whether Track B outranks the rest of Track A entirely.

---

# Track A — machinery correctness

Each batch is one commit, independently revertible, verified with
`.venv/bin/python -m unittest discover -s tests`.

## Batch 0 — Let CI tell the truth  *(L-2, L-3, #21)*

1. **`.github/workflows/tests.yml`** — add `lxml` to the install line. `enrich_leads.py` calls
   `BeautifulSoup(html, "lxml")` at six sites; it is in `.venv` (6.1.1) but never in CI, so ~10
   tests have ERRORed on every push. `tests/test_workflows.py:112` uses `issubset`, so adding a
   package does not break it.
2. **`evals/accept_rate.py:46`** — `DEFAULT_VERDICT_FIELD = verdict_module.VERDICT_FIELD`, and
   change the `proposed_verdict` default from `sendability.PACKET_TIER_SENDABLE` to `VERDICT_SEND`.
   Follow the import shape `evals/reproducibility.py:48` and `evals/completeness.py:30` already
   use. `completeness.py` deliberately keeps a separate `SCORED_VERDICT_FIELD = "Packet Tier"` for
   the *CSV* side — do not collapse those two.
3. **`tests/test_accept_rate_eval.py:25`** — build the fixture key from
   `verdict_module.VERDICT_FIELD` / `VERDICT_SEND` instead of the literal `"Packet Tier"`.
4. **Hoist the parity guard.** Replace the per-file assertions in `test_completeness_eval.py:60`
   and `test_reproducibility_eval.py:132` with one parametrised test over every module under
   `evals/` declaring `DEFAULT_VERDICT_FIELD`.

**Exit:** `Tests` green on main; `accept_rate.py` exits cleanly and reports
`insufficient-sample` instead of 422ing. **Not** a KPI number — see B1.

## Batch 1 — Stop the queue re-serving already-contacted leads  *(critical, #1)*

`upload_airtable.py:493` copies the CSV's `Status` into the payload without recording it in
`defaulted_fields`. `score_leads.py:1574` writes `"Status": "New"` on every scored row, and
`airtable_sync.plan_airtable_sync:427-428` only strips *defaulted* fields from a PATCH — so
re-uploading a scored CSV PATCHes `Status="New"` over whatever a human set. A dialled lead
returns to `QUEUE_FILTER_FORMULA` and is contacted twice; `Won`/`Lost`/`Not Fit` silently revert.

**Change:** in the `OPERATIONAL_FIELDS` loop in `csv_row_to_record`, append the field name to
`defaulted_fields` whenever the value came from the CSV — for both `Status` and `Outreach
Channel`, since `mark_sent.py` writes both. `lead_machine/verdict.py:19-22` already documents this
contract; this makes the code match it. POST behaviour is unchanged.

**Test:** a PATCH payload built from a scored CSV carries no `Status` and no `Outreach Channel`
key, while the POST payload for a new record still carries `Status: "New"`.

**Unblocks B2** — the backfill is unsafe until this lands.

## Batch 2 — Spend ceilings on the live front door  *(#3, #14, #13, #22, L-6, L-7)*

`make_search_batch.py` is the front door AGENTS.md calls current and `Makefile:56` runs, and it
never calls `getenv_int` — its only caps are CLI flags defaulting to `None`.

1. Seed caps from the environment in `collect_raw_rows` (`:613`) using the `getenv_int(...)` +
   `[STOP] Hit MAX_*` pattern in `places_search.py`. CLI flags may only *lower* the value.
2. Count **pages**, not searches, against `MAX_SEARCH_CALLS_PER_RUN` — each page is separately
   billable. `pages_fetched` already tracked at `:252`/`:660`.
3. Pick real defaults. `MAX_SEARCH_CALLS_PER_RUN=3` was written for the legacy CLI and is
   incompatible with any 66-query config. Correct `AGENTS.md:130-131` to match.
4. `max_enrich` defaults to `getenv_int("MAX_WEBSITES_PER_RUN", 40)` in `run_search_batch` (`:805`).
5. Return non-zero from `main()` when `raw_count == 0` or errors cover every search; print the
   first few errors to stderr, not only into the markdown report.
6. Delete `ExpandedSearch.max_results` (`:386`, never read); warn when `limit_per_query` sits
   alongside a `page_size` that overrides it.
7. Document the 19 missing env vars in `AGENTS.md:126-137` and the 4 missing in `.env.example`.

## Batch 3 — Suppression: one resolver, one writer, a loud bypass  *(#4, #15, #16, #17, #11)*

`compliance.load_configured_suppression_index` (`:225`) resolves `SUPPRESSION_SOURCE` →
`LPM_OUTCOMES_JSONL`; all four `hermes_queue` sites (`:532`, `:926`, `:1094`, `:1139`) read
`LPM_OUTCOMES_JSONL` or a hardcoded path only.

1. Have `hermes_queue` read the index through `compliance.load_configured_suppression_index`, plus
   a startup assertion that pipeline and mark-sent resolve the same file. Keep the *append* target
   separate — the same ledger takes `{"event": "sent"}` rows (`:1385`).
2. Add `opt_out` / `unsubscribed` to `mark_outcome.OUTCOME_EVENTS` (`:111`): append a ledger row
   with `record_id`/`email`/`phone`, set a terminal Status, clear `Next Follow-Up`.
   `docs/notion-plans-to-consolidate/01-airtable-crm-rebuild.md:313` already specifies it.
3. Print `[WARN] suppression index bypassed…` from the `ALLOW_MISSING_SUPPRESSION_INDEX` branch
   (`compliance.py:237`) and stamp the resolved source + index size into run receipts.
4. `mark_outcome` checks suppression after the GET at `:261` before re-arming. `mark_sent.py:139-145`
   is the shape to copy.
5. Port `mark_sent`'s idempotency check into `mark_outcome`, and reorder `closed_won` so the Deals
   POST (`:350`) lands before the Prospects Status PATCH (`:284`).

## Batch 4 — Make the evals capable of passing  *(L-1, L-4, L-5)*

1. **Archive the obsolete corpus** — `git mv` 12 files to `output/archive/`: 5 `scored_debug_*`,
   `scored_hardscaping{,_eastside_round3,_southwest_round4}.csv`, `scored_roofing.csv`,
   `scored_manual_existing.csv`, `scored_detailing_columbus_medium_001.csv`,
   `scored_detailing_cleveland_medium_001_api_tiered.before_131_fix.csv`. Measured:
   `blank_scored_keys` 265 → 0, zero unique leads lost (666 distinct keys either way). All 265
   blank keys and all 79 `corpus_conflicts` come from CSVs predating the `Place Key` / `Packet
   Tier` columns — every conflict is `'' vs <tier>`, a schema-era artifact.
2. **Split `scored_orphans` by tier** (`completeness.py:187`). Send/review-tier orphans fail;
   reject/watch orphans are counted and printed, mirroring `reproducibility.py:53`'s
   `REPORTED_CATEGORIES`.
3. **Delete `ANGLE_BLOCKER`** (`sendability.py:47`, `:547`) — the scorer always fills the angle
   field and nothing short-circuits ahead of it, so it cannot fire. Update
   `tests/test_gate_liveness_eval.py:126-137`.
4. **Fix the ~51 base-side rows** — `blank_base_keys` 23, `blank_base_verdicts` 21,
   `base_orphans` 7. `duplicate_base_keys` is 0.
5. **Rescore for `reproducibility`** — re-measure `stale_evidence` after the archive move.
6. **Restructure `gates.yml`** — cloud (`ubuntu-latest`, daily cron): required-choices,
   schema-report, accept-rate; needs only `AIRTABLE_PAT` + `AIRTABLE_BASE_ID` as repo secrets.
   Local (`make gates`): completeness, reproducibility, gate_liveness. Drop the self-hosted
   runner and the `LPM_SCORED_OUTPUT_DIR` repo variable. Correct
   `docs/verdict_cutover_runbook.md:270` [L-9].

## Batch 5 — Enrichment evidence honesty  *(#5–#10)*

These feed outreach claims, so correctness here is a compliance surface.

- **#5 `enrich_leads.py:1208`** — reuse `_classify_rendered_exception` (`:1231`); return
  `form_detection_method="rendered_unavailable"` and `contact/estimate_form_detected="Unknown"`;
  make `evidence.py:593-596` suppress the "No estimate/contact form detected" pain lines when the
  form state is Unknown.
- **#6 `review_velocity.py:397`** — zero confidence whenever `state != "present"`, not only on
  `stale_observation`. Today an `unknown` band keeps 0.75, exactly the `>= 0.75` boundary
  `scoring_v3.py:201-202` gates on.
- **#7 + #9 (one defect) `review_velocity.py:437-489`** — numerator is recency-filtered and
  timestamp-parseable; denominator counts every review with text. Count `in_window` inside the
  loop and divide by that; set `reviews_scanned` to the number actually classified; add an
  `undated_reviews` anomaly; return `state="unknown"` when every text review was undated; add
  `publishTime`/`publish_time` to `_review_timestamp` (`:726-733`).
- **#8 `review_velocity.py:482`** — apply the operational-category corroboration bar to reputation
  risks (≥2 distinct review_ids, or severe+corroborated), require `rating <= 3.0`, add negation
  handling to `PAIN_PATTERNS['integrity_or_safety']`.
- **#10 `external_signals.py:129`** — return `state: "failed"` when `task.status_code >= 40000`
  instead of folding it into "pending"; have `collection_summary` count failures separately.

## Batch 6 — Sendability re-derivation  *(#2)*

`readiness_flags` (`sendability.py:619`) reconstructs `Sendable=Yes` from the stored `Verdict`, and
`admit()`'s only additional input (`derived.hard_icp_skip`, `Raw Evidence`) does not exist in the
base. The documented re-derivation is a no-op on exactly the rows it was written for.

Make `readiness_flags` treat "cannot re-derive" as a blocker rather than clearance.

**Watch:** this can empty the send queue. Measure against the live 12 rows before merging; if it
zeroes them, persist the ICP verdict to the base as a `Hard ICP Skip` field instead.

## Batch 7 — Remaining lows  *(#12, #18, #19, #20, #23, L-8)*

- **Delete `backfill_packet_tier.py`** — plus `tests/test_backfill_packet_tier.py` and its entry
  in `Makefile:34`. Add `"Packet Tier"` to a deny-list checked by the same repo-wide AST scan that
  blocks `typecast` in `tests/test_mark_outcome.py`.
- **#18 `sendability.py:1298`** — emit a blocker when `known_evidence_ids(row)` is empty *and* the
  row asserts evidence refs or safe claims, instead of short-circuiting to valid.
- **#19 `review_velocity.py:279`** — replace the per-place `history()` loop with one grouped
  `SELECT`, hoist the call out of `score_v3` into the batch driver, wrap `_connect()` in
  `contextlib.closing`.
- **#23 `.claude/skills/run-lead-batch/SKILL.md:38-40`** — point at `make search CONFIG=...` for
  stages 1-2 and `run_pipeline --skip-search --skip-enrich` for the rest; fix `run_pipeline`'s
  docstring.
- **L-8 `AGENTS.md:87`** — note the OpenAI extraction path (`manual_lead_insert.py:730`).

---

# Track B — capture and feedback

From the CRM capture review. Track A makes the machine correct; this makes it *measurable*.
Nothing here is a bug fix — it is the missing half of the loop.

## B1 — Make the accept-rate KPI computable  *(review #3)*

The blocker is not the eval. It is that no outcome has ever been recorded: 32 phone touches, 1
`Last Contacted`, 0 `Sales Activities`, a 0-byte `outcomes.jsonl`. Until rows reach
`Sent`/`Follow-Up`/`Won`/`Lost`, `accept_rate.py` correctly reports `insufficient-sample`.

The review's prescription — **make one ledger derive the other** — is the fix. Two manual writes
at the step with the least discipline available loses to reality, and has.

Depends on B3 for the ergonomics. **Decide open conflict #1 first.**

## B2 — Backfill `ICP Lane` / `Opportunity Score`  *(review #2)*

Both fields are `0/145` since they were created 2026-07-30. `upload_airtable.py` writes them
(`:254-255`, `:270-271`); no batch has been uploaded since they existed. The Verdict collapse
shipped on top of them anyway.

**Blocked on Track A Batch 1** — re-uploading a scored CSV currently resets human-owned `Status`,
which is why no one has done this. After Batch 1 it is a safe re-upload.

## B3 — `Queue State` field + a "Today" console  *(review recs #1, #2)*

1. Add the `Queue State` formula field, generated — never hand-written — by
   `.venv/bin/python -m lead_machine.queue_state --formula`. The base and the code cannot then
   fork the readiness rule. Note the post-collapse formula is simpler than when the review was
   written: one field, `Verdict`, not three.
2. An interface: record-list filtered on `Queue State = Ready to Dial` (12 rows today), detail
   pane showing Specific Observation, Safe Outreach Claims, Opener, Mockup URL, Phone — with
   Status, Outreach Channel, Last Contacted and Notes editable inline. Works on a phone. This is
   the difference between logging a send in 4 seconds and not logging it.

Currently: 0 interfaces, 0 automations, 5 views, no `Queue State` field.

## B4 — Reconstruct the lost history  *(review "next" #1)*

The 32 phone touches and the 2026-07-28 booked call exist in neither ledger. `STATE/README.md`
mandates `catalyst` / `pre_handle_objection` / `died_at_stage` on a `call_held` row and names the
catalyst pattern as the outreach trigger Loop A should target — with no other way to discover it.
Retroactive and lossy, but worth more than another 500 scored leads.

## B5 — Truth up the contract docs  *(review #5)*

Four verified errors in `docs/airtable_schema_required.md`, which is declared as the code's
authority: the `Sales — *` block on Prospects (gone — those fields are on Sales Activities), nine
views (live: five, none of the documented names), `Tasks` awaiting hand-step H3 (no such table),
and the omission of two live fields, `Lead Source` and `Notes`.

Worst of all, cross-repo: operator-agent `SKILLS/send-queue/SKILL.md:48` documents the query as
`Sendable=Yes + Packet Eligible=Yes + Status not in {Sent, Closed}` — three columns that no longer
exist. That is the contract text a different repo operates on. Line 69 already carries a Verdict
table, so the file was half-updated and step 3 was missed.

(`queue_state.py:47-52`'s stale "Closed" comment is **already fixed** — the collapse rewrote it.)

## B6 — Decide the inert inventory and the vertical spread  *(review #1, #4)*

133 of 145 rows carry a non-`send` verdict and will never re-enter the queue without a re-score.
Five verticals split already-nonexistent outcome data five ways; the one booked call came from
garage doors, the smallest segment at 13 rows.

Both are business decisions, not code. They belong on this list so they get decided rather than
left to sit.

---

# Verification

Per batch:

```bash
.venv/bin/python -m unittest discover -s tests    # 961 tests, ~23s
```

Bare `python -m unittest` discovers zero tests here and reports OK — always use `.venv`.

End-to-end, after Batch 4:

```bash
.venv/bin/python airtable_required_choices.py check     # exit 0, missing: []
.venv/bin/python upload_airtable.py --schema-report     # exit 0
.venv/bin/python evals/accept_rate.py                   # clean exit, insufficient-sample
.venv/bin/python evals/completeness.py --output-dir output
.venv/bin/python evals/reproducibility.py --output-dir output
.venv/bin/python evals/gate_liveness.py --output-dir output
.venv/bin/python query_ready_to_send.py                 # 12 leads, unchanged
```

Targeted:

- **Batch 1** — plan a sync from a scored CSV against an existing record; assert the PATCH payload
  has no `Status` / `Outreach Channel` key and the POST payload still carries `Status: "New"`.
  Then confirm `query_ready_to_send.py` still returns 12.
- **Batch 2** — `make search CONFIG=... MAX_SEARCHES=2`; confirm a `[STOP]` line and a stop at 2
  pages. Unset the env vars and confirm the new defaults bind. Break the Places key; confirm
  non-zero exit.
- **Batch 3** — append an `opt_out` via the new verb; confirm pipeline and hermes resolve the same
  file and both suppress. Confirm `ALLOW_MISSING_SUPPRESSION_INDEX=1` prints the `[WARN]`.
- **Batch 6** — run `readiness_flags` read-only over the live rows first. If contact-ready drops
  to 0, stop and take the persist-ICP-verdict route.
- **Track B** — the only real verification is a row reaching `Sent`, then `accept_rate.py`
  returning a number instead of `insufficient-sample`.
