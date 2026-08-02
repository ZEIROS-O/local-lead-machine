# local-lead-machine — send-readiness audit, 2026-08-01

Commit `61de7a9` (main, clean). Method: 8 direct gate/eval executions by the lead session,
then 6 subsystem finders whose 31 raw findings were each put to an adversarial verifier
instructed to refute. **23 confirmed, 8 refuted.** Severities below are the verifier's,
after correction — five finder severities were downgraded.

Findings the lead session verified directly are marked **[L]**; the rest come from the
verified fan-out. Every entry has a file:line and a reachable failure scenario.

## What is actually green

Verified by running it, not by reading a runbook:

- **961 tests pass locally** (`.venv/bin/python -m unittest discover -s tests`, 22.8s).
- **`airtable_required_choices.py check` exits 0** — `missing: []` across 17 select fields.
- **`query_ready_to_send.py` exits 0** and returns its 12 leads (9 garage_doors, 3 plumbing;
  10 A / 2 B; all `Status=New`).
- **The Verdict collapse is decision-neutral** — `verdict_drift: 0`, `snapshot_drift: 0`.
  The 9→1 judgment-surface collapse provably changed no decision.
- **`Packet Tier` is genuinely gone** from the base (`fields[]=Packet Tier` → 422
  `UNKNOWN_FIELD_NAME`), so the cutover completed as the runbook claims.
- **Secrets hygiene is clean.** `.gitignore:2-4` covers `.env` and `.env.*` with an
  `!.env.example` exception; `git ls-files` confirms only `.env.example` is tracked.

The pipeline logic is sound. Everything below is about the machinery *around* it.

---

## [L] Instrumentation — verified directly by the lead session

These are not in the numbered fan-out list below; they were established by direct execution.

**[L-1] 🟠 The `Airtable gates` workflow has never run, and currently cannot.**
`gh run list --workflow=gates.yml` returns zero runs, ever. All four prerequisites named in
the workflow's own setup comment are unmet: `0` self-hosted runners (it needs
`[self-hosted, linux, lpm]`), `0` repository variables (needs `LPM_SCORED_OUTPUT_DIR`),
`0` repository secrets (needs `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`). The entire daily suite —
required-choices, schema-report, completeness, reproducibility, gate-liveness, accept-rate —
exists as a file and has executed zero times. This is the root cause that let L-2 and L-3 land.

**[L-2] 🟠 `evals/accept_rate.py` cannot read the base at all.**
`evals/accept_rate.py:46` still sets `DEFAULT_VERDICT_FIELD = "Packet Tier"` and filters on
`PACKET_TIER_SENDABLE`. That column was deleted in cutover step 4, so every run 422s. Commit
`dc3bbae` says "point **both** gates at Verdict" — there were **three** Airtable-reading
evals. `tests/test_accept_rate_eval.py:25` pins `"Packet Tier"` in its fixture, so the suite
stays green over the break. AGENTS.md names accept-rate *the* KPI ("below ~50% the loop is
losing"); it has been un-computable since the cutover. The last fixture-shaped run printed
**3.8% (1 accepted / 26 decided), `below-floor`**.

**[L-3] 🟠 CI `Tests` is red on main — missing `lxml`.**
`enrich_leads.py:333` calls `BeautifulSoup(html, "lxml")`, but `.github/workflows/tests.yml`
installs `beautifulsoup4 playwright python-dotenv PyYAML tldextract` and never `lxml`. About
ten tests in `test_lead_machine_contracts` and `test_visual_audit_fields` ERROR in CI while
passing locally. The push gate has never guarded anything.

**[L-4] 🟡 `evals/completeness.py` can never pass, by construction.**
`ok = not any(finding_counts.values())` over seven categories, one being
`scored_orphans: 551` — leads the verdict runbook (line 289) *explicitly decided* not to
import. Current: `scored_orphans 551 · blank_scored_keys 265 · corpus_conflicts 79 ·
blank_base_keys 23 · blank_base_verdicts 21 · base_orphans 7`.

**[L-5] 🟡 `reproducibility` and `gate_liveness` are red on already-accepted debt.**
reproducibility exits 2 on `stale_evidence: 80` / `unverifiable: 30`; gate_liveness exits 2
with `ANGLE_BLOCKER` firing 0 times across 1,297 rows. Combined with L-4: every eval is red
every day for reasons already accepted. That is alarm fatigue with a cron on it — **a gate
that cannot go green carries the same signal as one that cannot go red.**

**[L-6] 🟡 AGENTS.md's env-var list reads as exhaustive and is not.**
19 vars are read by code but absent from `AGENTS.md:126-137`, including four that spend money
(`MAX_NEW_DATA_USD_PER_RUN`, `MAX_SEMRUSH_API_UNITS_PER_RUN`, `LLM_SPEND_CONFIRM_USD`,
`DATAFORSEO_ESTIMATED_USD_PER_REVIEW_TASK`) and four paid-API credentials (`DATAFORSEO_LOGIN`,
`DATAFORSEO_PASSWORD`, `SEMRUSH_API_TOKEN`, `GOOGLE_PAGESPEED_API_KEY`). Someone provisioning
from the documented contract gets live spend paths whose caps they never knew to set.
Compounds finding #3 below.

**[L-7] ⚪ `.env.example` is missing 4 vars the code reads** — `OPENAI_API_KEY`,
`OPENAI_EXTRACTION_MODEL`, `LLM_SPEND_CONFIRM_USD`, `LOCAL_LEAD_MACHINE_ROOT`.

**[L-8] ⚪ `AGENTS.md:87` says scoring runs on the Anthropic Messages API; an OpenAI path
also exists.** `lead_machine/manual_lead_insert.py:730` defaults to `gpt-4.1-mini` for
extraction. Opt-in behind `--llm` and off the main pipeline, so a doc gap, not a defect.

**[L-9] ⚪ `docs/verdict_cutover_runbook.md:270` claims "Gates exit 0" — false.**
It cites sub-metrics (`verdict_drift 0`, `matched 35`) rather than exit codes. Only
required-choices and schema-report exit 0; the other four exit 2.

### Operational state (not defects)

- The 12-lead queue is **100% `Outreach Channel: Phone`**, `Email` blank on 8/12, with one
  scraped `your@email.com` placeholder. AGENTS.md's email-first compliance posture is not
  workable on this queue as it stands.
- **Corpus is going stale**: newest scored CSV is 2026-07-28; last morning-queue run 2026-07-30.
- `Consent Posture` and `Owner Name` are blank on all 12; `Mockup URL`/`Video URL` blank
  (Loop B unbuilt — expected).

---

## 🔴 Critical (1)

### 1. Re-uploading a scored CSV PATCHes human-owned Status back to "New", returning already-contacted leads to the dial queue

**Where:** `upload_airtable.py:492` · *silent-data-loss*

**What:** `OPERATIONAL_FIELDS = ["Status", "Outreach Channel"]` copies the CSV's `Status` verbatim into the payload and never records it in `defaulted_fields`. `score_leads.py:1574` writes `"Status": "New"` on every scored row, so every scored CSV carries a non-blank `Status`. On the PATCH path `airtable_sync.plan_airtable_writes` only strips *defaulted* fields (`lead_machine/airtable_sync.py:427-428`), so `Status: "New"` is sent and overwrites whatever the human set.

**Failure:** A lead is uploaded, dialled, and `mark_sent.py` sets Status=Sent / Last Contacted / Next Follow-Up. The operator later re-runs the batch (`run-lead-batch`, or re-uploads `output/scored_garage_doors_cleveland_medium_002_manual.csv` after a scoring tweak). The upsert matches the existing record by Place Key and PATCHes `Status="New"`. The record now has Verdict=send + Status=New, which is exactly `queue_state.QUEUE_FILTER_FORMULA` (`AND({Verdict}='send',OR({Status}='New',{Status}='Mockup Needed'))`), so `query_ready_to_send.py` serves the lead again the next morning and it is contacted a second time. The same PATCH silently reverts `Won` / `Lost` / `Not Fit`: a closed-won prospect goes back to New, so the Deals row is orphaned and `evals/accept_rate.py`'s `ACCEPTED_STATUSES = {Sent, Follow-Up, Won, Lost}` stops counting the accept — the KPI AGENTS.md calls the loop's pass/fail reads artificially low.

**Verifier correction:** None - severity critical stands. This is the strongest finding of the set: it silently re-contacts a prospect (a real compliance surface, not just a data smell), reverts Won/Lost/Not Fit, and corrupts the accept-rate KPI that AGENTS.md names as the loop's pass/fail. The minimal fix is to treat Status as write-once - append it to defaulted_airtable_fields whenever it is being written from the CSV, or drop OPERATIONAL_FIELDS from PATCH payloads entirely and let mark_sent/mark_outcome be the only writers, as verdict.py already documents.

**Fix:** Treat `Status` as human-owned on the update path: append `"Status"` to `defaulted_fields` whenever the value came from the CSV rather than from a computed rule (or, more simply, always pop `Status` from a PATCH payload and only include it on POST). Note the same change makes the currently-dead `Mockup Needed` / `Manual Review` branches at upload_airtable.py:553-559 reachable — today they never fire because the CSV always supplies a non-blank `New`, so manual-review leads land in the base as `New`. Add a regression test asserting that a PATCH payload built from a scored CSV contains no `Status` key.


## 🟠 High (3)

### 2. The 9 live send-queue rows pass on a stored Verdict snapshot; admit()'s ICP re-read is structurally inert on every Airtable row

**Where:** `lead_machine/sendability.py:619` · *correctness*

**What:** `readiness_flags` reconstructs `Sendable=Yes` from the stored `Verdict`, and the only thing `admit()` adds on top is `hard_icp_skip_blockers`, whose two inputs (`derived.hard_icp_skip` and `Raw Evidence`) do not exist as fields in the Prospects base — so the "re-run the admission so the ICP verdict can withdraw it" guarantee is a no-op on exactly the rows it was written for, and no other sendability rule is re-derived at all.

**Failure:** A Prospects row whose `Verdict` cell reads `send` (written by an older scoring run, an older schema, or a hand edit in the Airtable UI) is read by `query_ready_to_send.py:154` / `make_packets.py:224`. `readiness_flags` maps verdict→tier `Sendable Packet` (line 618), calls `admit(row, ())`, which returns zero blockers because `row` has no `derived` key and `_raw_evidence(row)` is `{}` (line 419-423), so `Sendable=Yes`, `Packet Eligible=Yes`. `contact_readiness_failures` then skips every cross-check because the row carries a Verdict (line 663-664). The lead enters the send queue although `evaluate_sendability` on the very same row returns 6 blockers. Verified live, read-only: 9/100 sampled base rows are `is_contact_ready==True`, all 9 by stored `Verdict=send`; 0/100 rows can produce an ICP blocker at all.

**Verifier correction:** Downgraded from critical to high. The mechanism is confirmed, but the practical blast radius is bounded: Verdict is machine-written by the scorer on the same run that computed the ICP verdict, nothing auto-sends (AGENTS.md Gate 0), and a human dials the queue. The defect is the silent loss of a documented safety re-derivation, not an active mis-send today. The honest framing is 'the cutover deleted the columns admit() reads, so the choke point degraded to a pass-through on base rows' - fix is to persist the ICP verdict (or the hard-skip reasons) as a base field, or to make readiness_flags fail closed when the row carries a Verdict but none of admit()'s inputs.

**Fix:** Either persist the ICP verdict to the base (a `Hard ICP Skip` / `Raw Evidence` field) so `admit()` has something to read back, or make `readiness_flags` refuse to reconstruct `Sendable=Yes` from a stored Verdict on a row that carries none of the evidence the gate needs — i.e. treat "cannot re-derive" as a blocker rather than as clearance.

### 3. The current search front door (make_search_batch.py) enforces none of the MAX_*_PER_RUN Places caps — one `make search` can bill ~50x the documented ceiling

**Where:** `make_search_batch.py:613` · *-*

**What:** MAX_SEARCH_CALLS_PER_RUN / MAX_DETAILS_CALLS_PER_RUN / MAX_TOTAL_PLACES_PER_RUN are read only by places_search.main() (the LEGACY CLI). make_search_batch.py — the front door AGENTS.md calls "the current one" and the one the Makefile runs — never calls getenv_int at all; its only caps are optional CLI flags that default to None.

**Failure:** Operator runs `make search CONFIG=search_configs/garage_doors_cleveland_medium_001.json` (Makefile:56) with MAX_SEARCHES/MAX_DETAILS unset — they default empty at Makefile:12-13, so no --max-searches/--max-details is forwarded. collect_raw_rows then runs all 66 expanded query×geo searches (11 queries x 6 geo slices), 2 pages each = up to 132 billable Text Search calls, and issues a Place Details call for every unique place id — up to 2,640. The documented budget for the same run is 3 searches / 50 details / 50 places. At Places (New) list prices (~$32/1k Text Search Pro, ~$17-20/1k Details) that is a ~$50+ invoice for a run the operator was told costs about $1, with no [STOP] line and nothing in the report flagging it. epoxy_cleveland_medium_005 is worse: 72 searches, 144 pages, 2,880 places.

**Verifier correction:** Real, but high rather than critical, and the framing 'ignores the documented caps' is slightly off. MAX_SEARCH_CALLS_PER_RUN=3 was written for the legacy `places_search.py` CLI reading a 12-row data/searches.csv; a 3-call ceiling is incompatible with any shipped 66-query geo-sliced config, so the honest statement is that the current front door has NO default spend ceiling at all and the documentation describing one is stale for this path. That is still a real money defect — an operator who follows the skill's pre-flight ('confirm the per-run caps are set') gets no protection and no [STOP] line — but it requires a deliberate `make search` on a chosen config rather than firing on its own, and the invoice is tens of dollars, not a runaway loop. The fix is a `getenv_int`-backed default on `max_searches`/`max_details` in make_search_batch, plus correcting the AGENTS.md/SKILL.md cap numbers to values a real config can live with.

**Fix:** In collect_raw_rows, seed the caps from getenv_int ("MAX_SEARCH_CALLS_PER_RUN", "MAX_DETAILS_CALLS_PER_RUN", "MAX_TOTAL_PLACES_PER_RUN") and let the CLI flags only lower them, following the `getenv_int(...)` + `[STOP] Hit MAX_*` pattern AGENTS.md:143 already mandates. Count pages_fetched against MAX_SEARCH_CALLS_PER_RUN (each page is a separate billable call), not searches.

### 4. hermes_queue resolves the suppression ledger from LPM_OUTCOMES_JSONL only — SUPPRESSION_SOURCE is ignored at send time

**Where:** `lead_machine/hermes_queue.py:532` · *-*

**What:** `compliance.load_configured_suppression_index()` (pipeline side) reads `SUPPRESSION_SOURCE` first and falls back to `LPM_OUTCOMES_JSONL`. All four hermes resolution sites read `LPM_OUTCOMES_JSONL` or `morning_queue.json`'s hardcoded path and never look at `SUPPRESSION_SOURCE`. The two halves of the machine can therefore be gated on two different ledgers with no check that they agree.

**Failure:** The operator follows AGENTS.md:134 ("Compliance requires `SUPPRESSION_SOURCE` (or `LPM_OUTCOMES_JSONL`)") and sets only `SUPPRESSION_SOURCE=/path/lpm_suppression.jsonl`, leaving `LPM_OUTCOMES_JSONL` unset and `morning_queue.json:14` at its committed default `/home/lucascoh/operator-agent/STATE/outcomes.jsonl`. An `opt_out` appended to lpm_suppression.jsonl correctly blocks search, enrichment and scoring. But `_load_suppression_index` (532), `_resolved_suppression_path` (926) and `_read_outcome_rows` (1094) all resolve the *operator-agent* file, which has no such row — so the morning queue keeps the lead (`_is_suppressed` returns False at line 781), `_require_current_suppression_clear` clears it (1057), and `mark_sent.py` is invoked with `--suppression-source` pointing at the wrong ledger, so its last-line-of-defence re-read clears it too. The opted-out business is marked Sent. Because the operator-agent path exists on this machine, the miss is silent — no degraded-queue error fires.

**Verifier correction:** Real at high, with one scoping note the claim should carry: hermes_queue's ledger is dual-purpose — it is both the suppression source and the append target for `{"event": "sent"}` rows (:1385), which is why it resolves the operator-agent path rather than SUPPRESSION_SOURCE. So the fix is not simply 'read SUPPRESSION_SOURCE too'; it is to read the suppression index through compliance.load_configured_suppression_index (or assert the two resolve to the same file and fail closed if not) while keeping the append target separate. Also worth stating plainly: the ledger is currently 0 bytes, so today the send-side suppression index is empty for every configuration, and no lead has yet been marked Sent through this path — the breach is fully reachable but has not yet occurred.

**Fix:** Give hermes_queue the same resolution order as `compliance.load_configured_suppression_index` (`SUPPRESSION_SOURCE` -> `LPM_OUTCOMES_JSONL` -> config), or better, have hermes call that function so there is one resolver; and add a startup assertion that the path the pipeline gated on and the path mark-sent gates on are the same file.


## 🟡 Medium (13)

### 5. detect_rendered_form swallows every browser exception and returns an unmarked "no form" verdict, which becomes the outreach claim "No estimate form detected"

**Where:** `/home/lucascoh/local-lead-machine/enrich_leads.py:1208` · *silent-exception-swallowing*

**What:** A crashed/unavailable Chromium in the rendered form check is indistinguishable from a browser that looked and genuinely found nothing; the record carries no anomaly, no flag and no status, and the resulting "No estimate form detected" / "No contact form detected" pain claims flow into the packet.

**Failure:** A prospect's site loads its estimate form via a JS embed (HubSpot/Typeform/Jotform). Static parsing yields form_strength_rank 0, so the gate at enrich_leads.py:1100-1106 fires the rendered check. `p.chromium.launch()` raises (browser binary not installed on the runner, sandbox denial, OOM, `Executable doesn't exist` — anything that is not PlaywrightTimeoutError). Line 1208-1209 returns the untouched `_empty_form_signals()`; `merge_form_signals` (line 697) only overwrites when `candidate_rank > current_rank`, and 0 > 0 is false, so nothing changes and nothing records that the check never ran. `flags` gets `no_form_detected`, and lead_machine/evidence.py:593-596 then adds 20 points "No estimate form detected" + 14 "No contact form detected" as named pain. The packet tells a business its site has no estimate form when it does — an unsafe, falsifiable outreach claim, and it happens to every JS-form lead in the run at once, not one lead.

**Verifier correction:** Downgraded from high to medium. `rendered_form_check` defaults to False (enrich_leads.py:936, 1382) and is an explicit CLI opt-in, so this is dormant unless the operator turns it on; and the gate only fires when static parsing already found form_strength_rank <= 1, so the packet claim would be equally wrong with the check disabled - the defect is that the mitigation fails silently, not that it introduces a new false claim. Fix: reuse `_classify_rendered_exception` here and set an anomaly / `form_detection_method='unavailable'` so downstream can suppress the pain claim.

**Fix:** Mirror the site-status path, which already does this correctly: `_classify_rendered_exception` (enrich_leads.py:1231) turns a browser failure into `rendered_site_status="Unknown"` / "Browser-rendered check unavailable". Have `detect_rendered_form` return `form_detection_method="rendered_unavailable"` plus `contact_form_detected/estimate_form_detected = "Unknown"` on the `except Exception` path, and make evidence.py suppress the "No estimate/contact form detected" pain lines when the form state is Unknown.

### 6. build_review_velocity reports confidence 0.85 on an observation whose state and band are both "unknown", buying a free +1 on the evidence_confidence blocking gate

**Where:** `/home/lucascoh/local-lead-machine/lead_machine/review_velocity.py:397` · *evidence-defaulted-not-observed*

**What:** Confidence is only zeroed for a stale observation; when the cohort is too small to produce a band the function returns state="unknown", velocity_band="unknown", score=5 — but keeps the raw source confidence (0.85 timestamp_bootstrap / 0.65-0.99 snapshot_delta), and scoring_v3._score_evidence_confidence awards a point for confidence >= 0.75.

**Failure:** Repro run against the live modules: a lead with six timestamped reviews and a cohort of only 3 rates returns `state=unknown band=unknown conf=0.85 score=5`, and `_score_evidence_confidence` returns 1. `evidence_confidence` is a blocking gate — scoring_v3.py:201-202 appends `insufficient_evidence_confidence` when it is below `profile.minimum_evidence_confidence`. So a lead sitting exactly one point under the floor is admitted on a signal that reached no conclusion at all. This is not hypothetical: run_state/review_velocity.sqlite3 holds 98 place_keys and **0** of them have two snapshots, so `cohort_rates` returns `[]` on every call and MIN_COHORT_SIZE=20 can never be met — every lead in the current base takes this branch. Airtable simultaneously shows `Review Velocity Band = unknown` next to `Review Velocity Confidence = 0.85` (scoring_v3.py:284-286).

**Verifier correction:** Two corrections. (a) The confidence is 0.75 for the timestamp_bootstrap source, not 0.85 - which matters because 0.75 is exactly the `>= 0.75` threshold, so it is a boundary hit, not a comfortable margin. (b) The 'unknown band' state itself is expected and documented: AGENTS.md lines 98-100 say review velocity needs two observations >= 14 days apart and that `velocity_band` is legitimately `unknown` until a batch has been run twice, so the empty snapshot DB is not the bug. The bug is narrow: confidence and band are allowed to disagree. Severity medium, not high - the fix is one line (zero the confidence whenever state == 'unknown', not only when stale).

**Fix:** Zero the confidence whenever `state != "present"`, not just on `stale_observation`: `confidence=float(observation.get("confidence") or 0.0) if state == "present" else 0.0`. Alternatively gate scoring_v3.py:397 on `velocity.get("state") == "present" and float(velocity.get("confidence") or 0) >= 0.75`.

### 7. mine_review_pain silently discards every review whose timestamp key or format it does not recognise, then still reports state="present", reviews_scanned=N and owner_response_rate=0.0 with no anomaly

**Where:** `/home/lucascoh/local-lead-machine/lead_machine/review_velocity.py:437` · *silent-collection-failure*

**What:** A review with an unparseable or unrecognised timestamp is `continue`d before it is ever classified or counted as owner-responded, but it is still counted in `reviews_scanned`; a total timestamp-parse failure therefore produces a confident "we read N reviews and found no operational pain, and the owner answers 0% of them".

**Failure:** Repro: 40 reviews all reading "They never called back and no one answered the phone.", each with an owner response, timestamped with Google Places API (New) `publishTime` — a key `_review_timestamp` (line 726-733) does not know, since it only checks `timestamp`/`datetime`/`time`/`review_date`. Output: `state=present scanned=40 verified=[] risks=[] resp_rate=0.0 conf=0.7 anomalies=[]`. Every review was thrown away, yet the profile claims 40 were scanned, asserts zero review pain, and asserts a 0% owner response rate on a business that answers every review. The same happens for any provider that returns relative dates ("3 months ago") — `_as_datetime` returns None and the review vanishes without a trace. Downstream, scoring_v3.py:157-163 files this as a real observation and scoring_v3.py:288 writes an empty `Review Pain Categories` to Airtable, so a genuinely high-pain lead reads as clean.

**Verifier correction:** Downgraded from high to medium, and the trigger needs restating. The current provider is DataForSEO (`collect_review_signals.py:170` writes `review_items` from `external_signals` items, source `dataforseo_google_reviews_newest`), which does emit a `timestamp` key - so the total-wipeout variant requires a provider swap to Places API (New) or a relative-date source, not today's data. What is unconditionally live is the per-review variant: any individually unparseable or out-of-window review is dropped from the numerator but kept in `reviews_scanned` and in the response-rate denominator, with no anomaly. That is the same root cause as claim 11; report the two together as one defect (numerator and denominator drawn from different populations, and discarded reviews never recorded).

**Fix:** Count skipped reviews and surface them: track `undated = sum of reviews with text but no parseable timestamp`, append an `undated_reviews` anomaly, set `reviews_scanned` to the number actually classified (not the number with text), and return `state="unknown"` when `undated == text_review_count`. Also add `publishTime` and `publish_time` to `_review_timestamp`.

### 8. reputation_risk_categories is set from a single unqualified regex hit — one sarcastic 5-star review labels a business "integrity_or_safety" at confidence 0.9 in Airtable

**Where:** `/home/lucascoh/local-lead-machine/lead_machine/review_velocity.py:482` · *unsafe-claim*

**What:** Operational categories require two distinct reviews (or severe + corroborated), but reputation-risk categories require nothing: one review matching `scam|fraud|stole|theft|dishonest` anywhere, at any star rating, sets the category, and the "verifier" is a near-copy of the classifier regex over the same text so it always agrees.

**Failure:** Repro: a single 5-star review reading "I was worried this was a scam but they were fantastic, best detailer in town!" yields `reputation_risk_categories=['integrity_or_safety']`, `confidence=0.9`, `evidence_refs=['review:r1:integrity_or_safety']`. scoring_v3.py:289 writes `"Review Reputation Risks": "integrity_or_safety"` to the named business's Airtable record and scoring_v3.py:116/167 carries it on the ScoringV3Result. A real, named local business is permanently tagged with a fraud/safety risk in the CRM on the basis of a review that praises it — and if that field is ever surfaced in packet copy it is a defamatory claim. The maker/checker separation the repo relies on is nominal here: `verify_review_pain` (review_pain_verifier.py:67-74) runs `\bscam\b` against the same string the classifier already matched with `scam|fraud|...`, so PASS is near-automatic.

**Verifier correction:** Keep medium. Two things temper it: the value is written to a CRM field for internal reads, not into packet copy (no shipped code path puts Review Reputation Risks in outreach text), and AGENTS.md Gate 0 means nothing auto-sends. The concrete fix is to apply the same corroboration bar to reputation risks as to operational categories, plus a rating guard (a >= 4-star review should not source a fraud tag) and negation handling in PAIN_PATTERNS['integrity_or_safety'].

**Fix:** Apply the same recurrence bar to reputation risks that `verified` already applies to operational categories (>=2 distinct review_ids, or severe+corroborated), and require `rating is not None and rating <= 3.0` before a review can contribute an integrity/safety risk. Longer term the verifier needs an independent signal (rating, owner response, structural corroboration) rather than a paraphrase of the classifier regex.

### 9. owner_response_rate divides a recency-filtered numerator by an unfiltered denominator, understating the rate by up to 10x

**Where:** `/home/lucascoh/local-lead-machine/lead_machine/review_velocity.py:489` · *evidence-defaulted-not-observed*

**What:** `responded` is incremented only for reviews inside the `recency_days` window (default 180, profile-configurable up to 730) that also have a parseable timestamp, while `text_review_count` counts every review with text regardless of age — so the ratio is structurally wrong whenever the sample spans more than the recency window.

**Failure:** Repro: 30 reviews, every single one answered by the owner — 3 within 180 days, 27 older. Output `scanned=30 resp_rate=0.1`. Ground truth is 1.0. The value is persisted in the `Scoring V3 JSON` blob written to Airtable (scoring_v3.py:296) and in the `review_pain` observation payload (scoring_v3.py:159), so an operator or a future packet writer reading it concludes "this business ignores 90% of its reviews" about a business that answers all of them — a reputation-management pitch built on a number the business's own Google profile disproves in one click.

**Verifier correction:** Severity medium is right, but this and claim 9 are one defect, not two: the numerator is drawn from the recency-filtered, timestamp-parseable subset and the denominator from the full list. Fix both ends together - count `responded` and the denominator over the same filtered population, and expose the discard count (an `unparseable_review_timestamps` anomaly) so `reviews_scanned` stops overstating what was actually read.

**Fix:** Compute the denominator over the same filtered population as the numerator: count `in_window` inside the loop and use `responded / in_window`, or drop `owner_response_rate` from the profile until it is computed over the full sample on both sides.

### 10. DataForSEOReviewsClient.get_reviews masks permanently failed tasks as "pending", so paid review tasks are silently abandoned

**Where:** `/home/lucascoh/local-lead-machine/lead_machine/external_signals.py:129` · *silent-exception-swallowing*

**What:** `get_reviews` calls `_dataforseo_tasks(..., allow_pending=True)`, which skips the `status_code >= 40000` failure check; a task that errored out permanently comes back with `result: null`, falls into the empty-`result` branch, and is reported as `state: "pending"` — indistinguishable from a task that is still running.

**Failure:** `collect_review_signals.py submit` posts 25 tasks at ~$0.20 each ($5 reserved and settled against MAX_NEW_DATA_USD_PER_RUN). Five of them fail permanently server-side — e.g. status 40501/40201 for an invalid or delisted `place_id`, which is exactly the case for a Places record that has gone stale. `collect` (collect_review_signals.py:133) calls `get_reviews`, gets `{"state": "pending", "status_code": 40501, ...}` for each, and `merge_review_records` (collect_review_signals.py:150-153) keeps only `state == "complete"`, dropping them without comment. The `[COLLECTED] pending=5` line tells the operator to poll again, forever; the $1.00 is spent and the five leads silently keep whatever stale review evidence they had. `ExternalSignalError` — the mechanism built for exactly this — is never raised.

**Verifier correction:** Keep medium, but the money framing is overstated. The spend is bounded by MAX_NEW_DATA_USD_PER_RUN and the loss is the per-task cost of the failed subset, not an unbounded leak; the durable harm is operational - a poll loop that never terminates and five leads silently retaining stale review evidence with no signal that anything failed.

**Fix:** In `get_reviews`, after `tasks = _dataforseo_tasks(data, allow_pending=True)`, inspect `task.get("status_code")` and return `state: "failed"` (with the status message) when it is >= 40000, rather than folding it into the empty-result "pending" branch. Have `collection_summary` count and print failures separately so the operator sees spend that produced nothing.

### 11. mark_outcome closed_won leaves the base half-written and unrecoverable when the Deals create fails

**Where:** `mark_outcome.py:350` · *partial-write*

**What:** `apply_mark_outcome` does three sequential writes with no transaction and no compensating action: PATCH Prospects Status (line 284), POST Sales Activities (line 317), POST Deals (line 350). The pre-write `preflight_select_values` only guards *select choices*; any other failure on the Deals POST aborts after the first two writes have already landed. Retry is then impossible because line 270 rejects the record: `normalize_airtable_status(fields.get("Status")) not in OUTCOME_SOURCE_STATUSES` and Status is now `Won`.

**Failure:** Operator runs `mark_outcome --event closed_won --deal-setup-price 2500`. The Prospects PATCH sets Status=Won and clears Next Follow-Up; the Sales Activities row is created; the Deals POST then fails for any non-select reason — a 429 from Airtable's 5 req/sec limit (three writes fire back-to-back with no throttle and `_request_json` has no retry at all), a transient 500, or an `INVALID_VALUE_FOR_COLUMN` on `Setup Price`. The prospect is now `Won` with an activity saying Won, but there is **no Deals record**, so the revenue ledger has no entry for the only kind of event that produces money. Re-running the exact same command exits 1 with "current Airtable Status is not eligible for closed_won; expected Sent or Follow-Up", so the operator cannot recover with the tool; the only fix is hand-editing Status back to Sent in the UI. The symmetric hazard on the open events is a duplicate ledger: after `replied`, Status=Follow-Up which *is* in OUTCOME_SOURCE_STATUSES, so a re-run after a network timeout on the response silently creates a second Sales Activities row and re-arms Next Follow-Up a second time.

**Verifier correction:** Downgraded from high to medium. This is a manual, human-invoked, one-lead-at-a-time command with a visible traceback on failure, and the recovery cost is one hand-edit in the Airtable UI - not silent, not automated, not scaled. Nothing is lost that a human cannot see happened. The fix worth doing is the cheap one: port mark_sent's `_exact_requested_state` idempotency check so a re-run after a partial write resumes instead of being rejected, and reorder so the Deals create precedes the Prospects Status flip.

**Fix:** Give mark_outcome the idempotent-recovery shape mark_sent already has: before the eligibility check, look for an existing Sales Activities row linked to this prospect with the same event + `occurred_on`; if the terminal Status is already the target and the ledger row exists, return `already_applied` instead of erroring. Reorder so the Deals create happens *before* the Prospects PATCH for `closed_won` (the terminal status is what blocks retry, so it should land last), and either add bounded retry on 429/5xx or surface a receipt naming exactly which of the three writes landed so a partial state is recoverable by hand.

### 12. backfill_packet_tier.py PATCHes `Packet Tier`, a column the live base no longer has — and its test pins the deleted field name so the suite stays green

**Where:** `backfill_packet_tier.py:238` · *stale-test-pins-broken-write-path*

**What:** `backfill_packet_tier.py` is a shipped, `--live`-capable Airtable writer whose entire payload is the single field `Packet Tier`, which the verdict cutover deleted from the Prospects table. `tests/test_backfill_packet_tier.py:160` asserts the payload equals exactly `["Packet Tier"]` against a fake session, so the test can only ever confirm the broken behaviour.

**Failure:** Operator runs `.venv/bin/python backfill_packet_tier.py --live` against the current base. `fetch_records` returns records whose `fields` dict has no `Packet Tier` key, so every record's tier reads as empty -> every record with a resolvable CSV tier lands in the `write` bucket (the `already_set` branch becomes unreachable). The first PATCH of 10 records sends `{"fields": {"Packet Tier": "Sendable Packet"}}` and Airtable answers 422 UNKNOWN_FIELD_NAME for the whole batch; `_patch_with_retry` treats a permanent status as fatal and raises RuntimeError, so the run aborts with zero writes and no receipts. Even if the column were re-added, the values it writes (`Sendable Packet` / `Manual Review Packet` / `Watchlist` / `No Packet`) are not members of the live `Verdict` vocabulary (`send`/`review`/`watch`/`reject`), so it cannot be retargeted by a rename alone. The full 961-test suite passes throughout, because the only test of the write path asserts the deleted field name.

**Verifier correction:** Severity should be medium, not high. The failure mode is fail-loud and zero-damage: Airtable answers 422 UNKNOWN_FIELD_NAME, `_patch_with_retry` treats a permanent status as fatal and raises, so the run aborts with no writes and no partial state. Nothing is silently mis-written, no money moves, and the script is a one-shot repair tool, not part of `make search` / `make batch` / gates. The real cost is dead-on-arrival tooling that an operator can only discover by running it. The claim's secondary point — that the values written (`Sendable Packet` etc.) are not members of the live `Verdict` vocabulary, so a rename alone cannot retarget it — is correct and is the reason the fix is 'delete it', not 'rename the constant'.

**Fix:** Either retire `backfill_packet_tier.py` (delete it and drop it from Makefile:34), or retarget it: `TIER_FIELD = lead_machine.verdict.VERDICT_FIELD` and map each resolved tier through `sendability.TIER_VERDICTS` before writing. In `tests/test_backfill_packet_tier.py`, replace the literal `"Packet Tier"` with `verdict_module.VERDICT_FIELD` so the test tracks the column instead of pinning its old name.

### 13. A search run where every Places call fails writes empty artifacts, prints [DONE] and exits 0

**Where:** `make_search_batch.py:656` · *-*

**What:** collect_raw_rows swallows every text-search exception into an `errors` list and continues; run_search_batch then dedupes/prefilters/enriches zero rows and main() returns 0 unconditionally, so a totally failed run is indistinguishable from a successful empty one to any caller, Makefile target, skill or cron.

**Failure:** The Places key is revoked, over quota, or billing lapses. All 66 searches raise (429 RESOURCE_EXHAUSTED / 403). The run prints `result count: 0` per search, writes an empty data/deduped/<batch>.csv and an empty output/enriched_<batch>.jsonl over any previous file, prints `[DONE] Raw results: 0` and `[NEXT] make batch BATCH=... `, and exits 0. `make search && make batch` chains straight into scoring an empty batch; a scheduled run reports success and the operator sees an empty send queue with no failure signal.

**Verifier correction:** Severity medium, not high, and 'indistinguishable from a successful empty one' overstates it for a human. The errors are not swallowed silently: they are collected and written into the run report (make_search_batch.py:975-976 emits one bullet per error), and stdout shows `result count: 0` for every search plus `[DONE] Raw results: 0`. A human watching the run sees the failure immediately. The real defect is narrower and still worth fixing: the exit code is the only thing a non-human caller can read, and it is 0, so `make full-from-search`, any `&&` chain, and any future cron cannot distinguish 'no results' from 'every call 4xx/5xx'. The fix is to return non-zero (or refuse to overwrite artifacts) when every search errored.

**Fix:** In main(), return a non-zero exit when raw_count == 0, or when errors covers every search run (e.g. `len(errors) >= len(queries_run)`), and print the first few errors to stderr rather than only into the markdown report. A zero-result search is either a broken key or a broken config — neither is success.

### 14. MAX_WEBSITES_PER_RUN is enforced only in enrich_leads.main(); the inline enrichment inside make_search_batch ignores it

**Where:** `make_search_batch.py:804` · *-*

**What:** The enrichment throughput/spend cap lives in enrich_leads.main (line 1762, `[STOP] Hit MAX_WEBSITES_PER_RUN` at 1773). make_search_batch enriches in the same pass by calling enrich_row directly, so on that path the number of sites scraped is bounded only by the optional `--max-enrich` flag, which defaults to None.

**Failure:** A 6-geo x 11-query config yields, say, 600 ICP-passing rows. `make search` (no MAX_ENRICH set, Makefile:56) hands all 600 to enrich_rows_concurrently at 6 workers with a 180s per-lead timeout — up to ~5 hours of scraping against 600 third-party sites from one IP, versus the 40 the documented cap allows. With --rendered-site-check or screenshots on, each worker launches its own Chromium. The operator gets no [STOP] line because the only cap on this path was never passed.

**Verifier correction:** Severity medium rather than high: unlike the Places caps this one costs no money — website scraping is not billed — so the exposure is wall-clock (a 6-worker pass over hundreds of sites with a 180s per-lead ceiling), Chromium process count when --rendered-site-check or screenshots are on, and outbound request volume from one IP. It is also bounded downstream of the Places spend rather than being an independent multiplier: the enrich set is whatever survived the ICP prefilter. Worth fixing by reading MAX_WEBSITES_PER_RUN as the default for `max_enrich` so the [STOP] contract holds on both front doors, but it is a throughput/politeness defect, not a billing one.

**Fix:** Default max_enrich to getenv_int("MAX_WEBSITES_PER_RUN", 40) in run_search_batch and let --max-enrich only lower it, emitting the same `[STOP] Hit MAX_WEBSITES_PER_RUN=` line so the two entry points report the same contract.

### 15. mark_outcome.py re-arms the follow-up cadence on an opted-out lead — it never loads or checks the suppression index

**Where:** `mark_outcome.py:282` · *-*

**What:** `mark_outcome.py` is the only writer that advances a lead after send, and it imports nothing from `lead_machine.compliance` except `normalize_airtable_status`. It never loads the suppression index and never matches the record against it, so a documented opt-out does not stop it from setting `Status=Follow-Up` and arming `Next Follow-Up` one cadence step out.

**Failure:** The suppression ledger named by `SUPPRESSION_SOURCE` contains `{"event": "opt_out", "record_id": "recX"}` (or a phone/email row matching recX) because the prospect replied "take me off your list" on 2026-08-01. The operator logs the reply honestly: `mark_outcome.py --record-id recX --event replied --occurred-at 2026-08-01T...`. The only precondition checked is `normalize_airtable_status(fields["Status"]) in {"sent", "follow-up"}` (line 270), which passes. The PATCH at line 288 writes `Status=Follow-Up` and `Next Follow-Up=2026-08-05`. The opted-out business is now on the operator's due list for a second touch, and every subsequent `mark_outcome` re-arms it again. Nothing downstream saves it: the morning queue only admits `New`/`Mockup Needed` and `mark_sent.py` refuses a `Follow-Up` status, so the second contact is executed by the human straight off the `Next Follow-Up` date with no machine gate in the path at all.

**Verifier correction:** Severity medium rather than high. mark_outcome sends nothing; the worst outcome is a `Next Follow-Up` date appearing on the operator's due list for a lead that should be suppressed, and the second contact is then a human decision made by the same person who just read the opt-out reply. It is a missing defence-in-depth layer on an internal CRM writer, not an automated re-contact. Note also the tension with the next claim: for this to bite, a suppression row must already exist in the ledger, which no shipped CLI can create. The right fix is small — have mark_outcome load the configured index and refuse (or force the closing branch) when the record matches — and it is the natural place to also add the opt-out event the next finding is about.

**Fix:** In `apply_mark_outcome`, after the GET at line 261, call `load_configured_suppression_index()` and `record_matches_suppression({"id": record_id, "fields": fields}, index)`; on a match raise `MarkOutcomeError` (or force `next_follow_up = None` and a terminal status) rather than re-arming. `mark_sent.py:139-145` already does exactly this shape of check and is the model to copy.

### 16. No shipped module can record an opt-out — the suppression ledger has readers everywhere and no producer

**Where:** `mark_outcome.py:111` · *-*

**What:** `compliance.SUPPRESSION_EVENTS` recognises six event names (`opt_out`, `opted_out`, `unsubscribe`, `unsubscribed`, `suppressed`, `stop`) and four pipeline stages fail closed if the ledger is missing — but nothing in the repo ever *writes* one of those rows. `OUTCOME_EVENTS` is exactly `{replied, meeting_booked, closed_won, closed_lost}`, and `hermes_queue` only appends `"event": "sent"`. "Reply stops sequence" is an unimplementable instruction with the shipped CLIs.

**Failure:** A prospect replies "unsubscribe / stop contacting me". The operator has two representable events: `--event replied`, which re-arms the cadence (see the previous finding), or `--event closed_lost`, which clears `Next Follow-Up` but writes no ledger row. Either way the outcomes JSONL that `SUPPRESSION_SOURCE`/`LPM_OUTCOMES_JSONL` points at gains nothing, so from the next `make_search_batch` / `enrich_leads` / `score_leads` run the business is still unsuppressed: it is re-discovered from Places, enriched, scored, packeted and uploaded, and any record of it that is still in `New`/`Mockup Needed` (or a duplicate record created under a new Place lookup) is eligible for the morning queue and for `mark_sent.py`, whose live suppression re-read finds no matching identifier. That is a second contact after a documented opt-out — the CAN-SPAM/TCPA exposure the AGENTS.md invariant at line 38 names.

**Verifier correction:** Severity medium, not high, and it is a missing feature rather than a broken guard — which changes the fix. The ledger is deliberately an external, shared JSONL (SUPPRESSION_SOURCE / LPM_OUTCOMES_JSONL, defaulting to the operator-agent STATE file), whose row format compliance.py parses generously; an operator can suppress a business today by appending one line by hand, and all four pipeline stages will honour it immediately. So the exposure is 'the documented invariant has no ergonomic implementation and the hand-workaround is undocumented', not 'opt-outs cannot be honoured'. The claimed breach chain also requires the hand-append never to happen. Fix by adding an `opt_out` event to mark_outcome that both clears Next Follow-Up and appends a suppression row carrying the record_id, phone and email — which would close this and the previous finding together.

**Fix:** Add an `opt_out` (and `unsubscribed`) event to `mark_outcome.py` that (a) writes a ledger row carrying `record_id`, `email` and `phone` to the resolved suppression source, (b) sets a terminal Status and clears `Next Follow-Up`, and (c) is the documented handler for any stop-reply. Docs/notion-plans-to-consolidate/01-airtable-crm-rebuild.md:313 already specifies this design ("`opt_out` activity -> sync writes an `opt_out` row ... that row *is* SUPPRESSION_SOURCE"); it was never built.

### 17. ALLOW_MISSING_SUPPRESSION_INDEX disables every opt-out with no output at any call site — the documented "loud" bypass is silent

**Where:** `lead_machine/compliance.py:237` · *-*

**What:** `load_configured_suppression_index()` returns an empty `set()` for the bypass flag without printing, logging or recording anything, and none of its four call sites emit a warning either — they print only in the `except ComplianceError` branch. `record_matches_suppression` then short-circuits `if not suppression_index: return False`, so every stage reports a clean compliance pass that is indistinguishable from a real one.

**Failure:** `.env` carries `ALLOW_MISSING_SUPPRESSION_INDEX=1` (a state docs/notion-plans-to-consolidate/02-repo-remediation-closeout.md:175 records as having actually occurred: "each `main()` returns 0 after loading `ALLOW_MISSING_SUPPRESSION_INDEX=1` from `.env`"), and `load_dotenv()` puts it in the environment on every run. `make_search_batch`, `enrich_leads`, `score_leads` and `places_search` then run with an empty index: `record_matches_suppression` returns False for every record at compliance.py:312-314, no `dnc_suppressed` reason is ever attached, opted-out businesses are enriched, scored, packeted and uploaded to Airtable, and the run's stdout and receipts contain nothing that distinguishes this from a run with a fully loaded index. The operator has no signal that suppression is off.

**Verifier correction:** Severity medium is right, but tighten the framing: the bypass is loud at configure time (you must set an explicitly-named env var) and silent at run time (nothing distinguishes a bypassed run from a fully-indexed one in stdout or receipts). That run-time silence is the actual defect, and it matters most because .env is loaded automatically on every run, so a flag set once for a single run persists invisibly. The claim correctly flags that it could not read .env to confirm the current value, so this remains conditional on the flag being set — keep it stated as a condition. Minimal fix: print a one-line `[WARN] ALLOW_MISSING_SUPPRESSION_INDEX=1 — suppression disabled for this run` at the return, and record the index size in the run receipts so a bypassed run is auditable after the fact.

**Fix:** Print a `[WARN] suppression index bypassed via ALLOW_MISSING_SUPPRESSION_INDEX — every opt-out is inactive for this run` from the bypass branch, stamp the resolved source path (or the literal string `bypassed`) plus the index size into each run's receipt/summary JSON, and have the eval/gate scripts fail on a bypassed run.


## ⚪ Low (6)

### 18. INVALID_REF_BLOCKER is disabled whenever the row has no known evidence IDs, so fabricated evidence refs satisfy the "real, named, verifiable leak" invariant

**Where:** `lead_machine/sendability.py:1298` · *compliance*

**What:** `has_invalid_refs` returns `False` when `known_refs` is empty, i.e. the absence of a ground-truth evidence set is read as "all refs are valid". The two remaining evidence gates do not compensate: `EVIDENCE_COUNT_BLOCKER` counts unvalidated strings, and `has_evidence_backed_opener` only asks that some claim carry a non-empty `evidence_refs` list.

**Failure:** A lead whose enrichment produced no `evidence_items` (crawl blocked, JS-only site, or an Airtable/legacy row where the Raw Evidence blob is gone) is scored, and the LLM emits two invented refs such as `ev_form_missing_01` / `ev_cta_02` plus an opener citing them. `known_evidence_ids(row)` is empty → line 1298-1299 short-circuits → no `INVALID_REF_BLOCKER`; `len(evidence_refs) >= 2` passes; `has_evidence_backed_opener` passes. The lead can be admitted with zero verifiable evidence, breaching the AGENTS.md invariant "No send without a real, named, verifiable leak". 113/1297 scored rows currently have an empty known-ref set, and 100% of base rows do.

**Verifier correction:** Downgraded from medium to low. The failure scenario is not demonstrated end-to-end - it requires the LLM to invent refs AND the row to clear roughly eight other blockers that draw on the same missing evidence. Report it as a hardening item (fail closed, or emit an explicit `unvalidatable_refs` blocker, when known_evidence_ids(row) is empty on a row that claims refs), not as an active breach.

**Fix:** Treat an empty `known_refs` as a blocker in its own right when the row also asserts evidence refs or safe claims ("evidence references cannot be verified"), rather than short-circuiting to valid.

### 19. ReviewSnapshotStore.cohort_rates is recomputed per lead and opens one sqlite connection (plus CREATE TABLE / CREATE INDEX) per place_key, producing thousands of connections for a guaranteed-empty result

**Where:** `/home/lucascoh/local-lead-machine/lead_machine/review_velocity.py:279` · *efficiency*

**What:** `cohort_rates` loops over every distinct place_key in the cohort calling `self.history()`, and `history()`/`_connect()` open a brand-new sqlite3 connection and re-run the DDL each time; scoring_v3.py:124-128 constructs a fresh store and calls it once per scored lead, with no per-run cache.

**Failure:** Scoring a 50-lead garage_doors/cleveland batch against the current run_state/review_velocity.sqlite3 (98 distinct place_keys, all in that one cohort) performs 50 x (1 DISTINCT query + 98 history() calls) = ~4,950 sqlite connections, each executing `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` before its SELECT. Because no place_key in the DB has two snapshots, `_velocity_from_snapshots` returns None for all 98 and `rates` is `[]` every single time — the entire scan is guaranteed to produce nothing. It grows as O(leads x places): once the store accumulates a few thousand place_keys this becomes the dominant cost of a scoring run, and each connection is left to refcount-close rather than being explicitly closed (`with sqlite3.Connection` commits, it does not close), so any future change that holds a reference leaks file descriptors under ENRICH_WORKERS concurrency.

**Verifier correction:** Low is correct, and it should be filed as efficiency, not correctness - the output is right, only the cost is wrong. The fd-leak half of the claim is speculative (`with sqlite3.connect(...)` commits rather than closes, but CPython refcounting closes it at scope exit); drop that and keep the concrete part: cache cohort_rates per (vertical, market) for the run, or push the aggregation into a single SQL query instead of N history() round trips.

**Fix:** Compute the cohort once per (vertical, market) per run and pass it down — hoist the `ReviewSnapshotStore(...).cohort_rates(...)` call out of `score_v3` into the batch driver, and inside `cohort_rates` replace the per-place `history()` loop with a single `SELECT place_key, observed_at, review_count FROM review_snapshots WHERE vertical=? AND market=? ORDER BY place_key, observed_at` grouped in Python. Add `contextlib.closing(...)` around `_connect()` in both `history` and `cohort_rates`.

### 20. backfill_packet_tier.py PATCHes the deleted `Packet Tier` column — every run 422s, and it now plans a write for the entire base

**Where:** `backfill_packet_tier.py:47` · *deleted-column-write*

**What:** `TIER_FIELD = "Packet Tier"` and `apply_backfill` sends `{"fields": {TIER_FIELD: entry["tier"]}}` (line 234-245). `Packet Tier` was deleted from the base by step 4 of the verdict cutover, so the PATCH is an unknown-field 422. Worse, the read side now reads `current = _clean(fields.get(TIER_FIELD))` (line 141) which is always blank post-cutover, so the `if not current: plan["write"].append(entry)` branch (line 160-161) puts *every* resolvable record into the write plan.

**Failure:** An operator notices blank tiers / an empty queue and reaches for the script the docstring advertises ("Repair the Airtable `Packet Tier` column from scored CSVs"). `--plan` reports ~1300 records needing a write — a completely fabricated number, since the column no longer exists — and `--apply` fires a 10-record PATCH that Airtable answers with `422 UNKNOWN_FIELD_NAME: "Packet Tier"`. `_patch_with_retry` (line 187-208) does not retry a 422, so the run aborts on the first batch with a raw HTTP error and no hint that the field was deleted on purpose. Nothing in the repo references this script except its own test, so no test or caller catches the breakage.

**Verifier correction:** Downgraded from high to low. It is dead code with a documented do-not-run: `grep -rn backfill_packet_tier --include=*.md --include=*.py --include=*.yml .` returns exactly two hits, its own test and docs/notion-plans-to-consolidate/02-repo-remediation-closeout.md:19, which explicitly says "Do not run ... backfill_packet_tier.py --live". Nothing schedules or imports it, and --plan is read-only so the fabricated count costs nothing but confusion. It fails loudly on the first batch rather than writing anything wrong. The right disposition is deletion (or a hard exit-2 preflight naming the cutover), not a high-severity bug.

**Fix:** Either delete the script (its job was superseded by `Verdict`, which `upload_airtable.csv_row_to_record` now writes directly) or retarget it at `Verdict` / `Verdict Reasons` via `verdict.verdict_for_tier`, and make it fail fast with a named error when the target field is absent from the base schema. Whichever is chosen, add `"Packet Tier"` to a shipped deny-list checked by the same repo-wide AST scan that already blocks `typecast` in `tests/test_mark_outcome.py`, so any future module naming a retired column fails the suite.

### 21. The field-contract assertion that would have caught accept_rate's drift exists for two of the three base-reading evals and was never extended to the third

**Where:** `tests/test_accept_rate_eval.py:25` · *missing-contract-test*

**What:** `tests/test_completeness_eval.py:60` and `tests/test_reproducibility_eval.py:132` both assert `module.DEFAULT_VERDICT_FIELD == verdict_module.VERDICT_FIELD`. `tests/test_accept_rate_eval.py` has no equivalent assertion and instead hardcodes `"Packet Tier"` in its fixture — which is the structural reason accept_rate silently kept pointing at a deleted column while the other two were migrated in dc3bbae.

**Failure:** The verdict column is renamed again (or a fifth base-reading eval is added). `evals/completeness.py` and `evals/reproducibility.py` are updated because their tests fail loudly; `evals/accept_rate.py` is not, because nothing fails. The daily `Report the accept-rate KPI` step in gates.yml then 422s on `fields[]=<old name>` for every run, and because that step deliberately writes its own summary line rather than a PASS/FAIL wrapper (gates.yml, `Report the accept-rate KPI`), the accept-rate KPI — the repo's stated go/no-go metric per AGENTS.md ("KPI = accept-rate; below ~50% the loop is losing") — is simply never reported, with no green/red signal distinguishing "broken" from "too few decisions to judge". This is the exact failure that already happened once.

**Verifier correction:** Severity should be low, not high. This is a test-coverage asymmetry, not an independent defect — the live consequence (accept_rate 422s every run, its test pins the deleted name) is already the known background item. The incremental content here is only 'the guard that would prevent recurrence exists in 2 of 3 places', which is a hardening suggestion. Also note the stated failure scenario is a re-run of a failure that has already happened rather than a new reachable one, and the accept-rate KPI is currently unreportable anyway because the gates workflow has never executed (0 runners/vars/secrets).

**Fix:** Add `self.assertEqual(accept_rate.DEFAULT_VERDICT_FIELD, verdict_module.VERDICT_FIELD)` to `tests/test_accept_rate_eval.py`, and build the fixture key from `verdict_module.VERDICT_FIELD` rather than the literal `"Packet Tier"`. Better: hoist the assertion into one parametrised test over all modules under `evals/` that declare `DEFAULT_VERDICT_FIELD`, so a new eval is covered without editing the test.

### 22. limit_per_query is dead at search time — every query fetches page_size x max_pages, double the declared limit

**Where:** `make_search_batch.py:386` · *-*

**What:** expand_searches sets ExpandedSearch.max_results = config.limit_per_query, but collect_raw_rows passes `max_results=search.total_max_results` (page_size * max_pages) to text_search_paged. ExpandedSearch.max_results is never read anywhere, so the config field that reads like the per-query cap has no effect and every query returns up to 2x it.

**Failure:** Every shipped config sets limit_per_query: 20, page_size: 20, max_pages_per_query: 2. An operator reading the config expects <=20 places per query and budgets 66 x 20 = 1,320 Place Details calls; the code fetches 40 per query, so 66 x 40 = 2,640 — exactly double the Details spend implied by the config. Lowering limit_per_query to throttle a run changes nothing at all (it is only clamped to <=20 at load, make_search_batch.py:168) unless page_size happens to be absent, which no current config is.

**Verifier correction:** Real as a dead-field/dead-knob trap, but low severity, and the headline 'double the declared limit / double the Details spend' is a misreading. The code is explicit that the per-query total is governed by `max_results_per_query`, defaulting to `page_size * max_pages` (:377), and the comment at :170 states limit_per_query survives only as the legacy per-CALL page size fallback. Fetching 40 places from a config that says page_size 20 / max_pages_per_query 2 is the declared behaviour, not an overrun — there is no second, contradicting total cap being violated. Restate as: `ExpandedSearch.max_results` is dead code and `limit_per_query` is a required config field that silently controls nothing whenever page_size is present, so an operator who edits it to throttle a run gets no change and no warning. Fix by deleting the field or making the loader reject/warn on a limit_per_query that page_size overrides.

**Fix:** Either delete ExpandedSearch.max_results, or make total_max honour it: `total_max = config.max_results_per_query or min(config.limit_per_query, page_size * max_pages)`. Whichever way, make the field name and the billed behaviour agree, and say in the config schema doc which knob actually bounds spend.

### 23. The documented run-lead-batch command cannot run stage 1 for any of the 34 search configs

**Where:** `run_pipeline.py:332` · *-*

**What:** run_pipeline stage 1 shells out to the legacy places_search CLI and derives its search file as data/searches_<batch>.csv. The run-lead-batch skill tells the operator to pick a config from search_configs/ and run `run_pipeline.py --batch <config_name>`, but no data/searches_<config_name>.csv exists for any config, so the run dies before the first stage.

**Failure:** Operator follows .claude/skills/run-lead-batch/SKILL.md and runs `python run_pipeline.py --batch detailing_cleveland_medium_001 --vertical detailing`. Line 332 resolves searches to data/searches_detailing_cleveland_medium_001.csv, line 395 finds it missing and raises `[ERROR] Search CSV not found`. The documented recovery path — pointing --searches at the one file that does exist, data/searches.csv — is worse: it holds 12 hardscaping and roofing Cleveland/Akron queries, so the run would search the wrong vertical and wrong geo under a batch named for a detailing config, then score it with --vertical detailing. Stage 1 also applies none of the config's geo slices, ICP prefilter or dedupe.

**Verifier correction:** Severity low, not medium. The failure is loud, immediate and pre-flight: a SystemExit with the exact missing path, before any API call, any spend, or any artifact write. Nothing wrong is produced or decided. AGENTS.md:14-18 already documents run_pipeline as the older path and states the places_search stage-1 inconsistency is deliberately unfixed; what is actually defective is the SKILL.md, which points the operator at the legacy entry point with config-shaped arguments instead of at `make_search_batch.py --config`, the front door AGENTS.md calls current. Fix the skill (or make run_pipeline resolve a search_configs/ name), not the pipeline. The claim's 'worse recovery path' — pointing --searches at data/searches.csv and silently searching hardscaping under a detailing batch name — is a real trap but is speculative operator behaviour, not something the code does.

**Fix:** Make the skill (and run_pipeline's docstring) stop implying a search config is a valid --batch for run_pipeline: either have run_pipeline take --config and delegate stage 1+2 to make_search_batch (skipping its own enrich stage so nothing double-scrapes), or have the skill run `make search CONFIG=...` for stages 1-2 and run_pipeline --skip-search --skip-enrich for the rest.


## Refuted — do not act on these

Recorded so they are not re-raised by the next audit.

- **_review_demand_band reads the band out of Raw Evidence instead of the row, so every row without a Raw Evidence blob bands as "thin"** — The cited code is accurate but the fallback is correct for the shape it is given, not a bug. `buyer_signals._review_profile` (lines 441-453) reads `record.get('Rating') or record.get('rating') or record.get('google_rating') or google.get('rating')` and the same chain for Reviews - i.e. it is written to accept the enriched-record blob whose reviews live under `google.reviews`, which is exactly what `_raw_evidence(row)

- **evaluate_verdict recomputes "reject" for 100% of live base rows, including all 9 stored sends** — The measurement is right and the conclusion is wrong: it restates a deliberate, documented decision as a defect. `lead_machine/airtable_sync.py:57-65` names `SUPERSEDED_BY_VERDICT = (Packet Tier, Commercial Fit State, Contact Eligibility, Asset State, Promise State, Send Blockers, Skip Reason)` - those columns were removed from the base on purpose by the verdict cutover, so of course a re-derivation from the base ret

- **An unrecognized review-band string fails open: the mature 40-point pain bar only matches the literal "mature"** — The code fact is accurate - `required_visible_pain_score` (sendability.py:1203-1213) special-cases only the literal 'mature' and otherwise falls through to the review-count buckets, and MANUAL_REVIEW_BANDS is {early_proof, ideal_growth}. But there is no live producer of the legacy vocabulary: `grep -rn 'Ideal 15-250\|Whale 500' --include=*.py .` returns nothing. `buyer_signals._review_profile` (lines 455+) emits only

- **RATING_BLOCKER cannot fire on a missing or unparseable rating (`if rating and rating < 4.0`)** — The code fact is right (`_rating` returns 0.0 via `_parse_float`, confirmed: `s._rating({'Rating':''})` -> 0.0, and `0.0 and ...` short-circuits), but the corpus evidence does not reproduce and the path is unreachable. I re-ran the count over every output/scored_*.csv using the module's own `_rating`/`_review_count`: `no rating: 2 | no rating & reviews>=5: 0 | ...& Sendable=Yes: 0` - not the claimed 43. Even in princ

- **gate_liveness's pass condition excludes `outside_replay`, so 3 of 26 blockers — including both Airtable-side admission gates — can be dead forever without the gate ever failing** — The code reading is accurate (`evals/gate_liveness.py:46-47`, `:262` computes `failed` over FINDING_CATEGORIES only) but the defect is not. Two disproofs. (1) The exclusion is structural and correct, not a hole: the eval replays scored CSV rows through `evaluate_sendability`. `MANUAL_CARD_UNVERIFIED_BLOCKER`, `PACKET_TIER_NOT_SENDABLE_BLOCKER` and `SCORER_WITHHELD_BLOCKER` live on paths that take an Airtable record o

- **tests.yml's dependency list has no equality contract — the workflow test uses `issubset`, so a missing CI dependency can never fail it** — The quoted code is accurate (`tests/test_workflows.py:112-121` uses `issubset`, other assertions in the file use `assertEqual`) but the causal claim is wrong, and with it the finding. Switching `issubset` to `assertEqual` would NOT catch the lxml gap or any future one: an equality assertion over the install-line tokens compares the workflow against a hardcoded literal in the test, so it fails only when someone edits 

- **No Makefile target runs any eval, so the four gates are only reachable through a workflow that has never executed** — The facts check out — `grep -rn evals Makefile` returns nothing, `make test` is unittest only, `make compile` is a 20-module py_compile list, and the only callers of the four evals are the `run:` steps in `.github/workflows/gates.yml` — but this is not a defect with a failure scenario. `make test` returning green is a true statement about the unit suite; it never claimed to run the gates, and no doc says it does. The

- **hermes mark-sent's SMS-consent and status guards are no-ops on the bare-record-id path, and its dry-run approval attests to checks that never ran** — The code reading is accurate — `_require_sms_consent` (hermes_queue.py:1067-1072) and `_require_queue_status_eligible` (:1083) both filter on `("consent_posture" in item or "Consent Posture" in item)` / `("status" in item or "Status" in item)`, and the bare-token branch at :1215 synthesizes items with only n/record_id/business/vertical, so both guards match zero items — but there is no reachable failure, and the clai

---

# The fix plan

Ordered so that each batch is independently revertible and verified with
`.venv/bin/python -m unittest discover -s tests`, per the repo convention.

The sequencing principle: **fix the things that make the system lie to you first.** Until
CI runs and the KPI computes, no later batch can be shown to have worked.

## Batch 0 — Let CI tell the truth  *(~30 min, no product risk)*

1. Add `lxml` to `.github/workflows/tests.yml`'s install line. [L-3]
2. Repoint `evals/accept_rate.py` at `Verdict` / `send`; fix the `tests/test_accept_rate_eval.py:25`
   fixture; add the `VERDICT_FIELD` parity assertion that `test_completeness_eval.py:60` and
   `test_reproducibility_eval.py:132` already have and this one lacks. [L-2, #22]
3. Get a real accept-rate number off the live base.

**Exit:** `Tests` green on main; accept-rate prints a live figure.
**Decision needed:** what to do about `gates.yml` [L-1] — see Decisions below.

## Batch 1 — Stop the queue re-serving already-contacted leads  *(critical, #1)*

Pop `Status` / `Outreach Channel` from PATCH payloads in `upload_airtable.py` and let
`mark_sent` / `mark_outcome` be their only writers — which is what
`lead_machine/verdict.py:19-22` already documents as the contract. Add a regression test
asserting a PATCH payload built from a scored CSV carries no `Status` key.

**Watch:** this makes the currently-dead `Mockup Needed` / `Manual Review` branches at
`upload_airtable.py:553-559` reachable for the first time. Confirm that is intended before
merging — today every manual-review lead lands in the base as `New`.

**Until this ships: do not re-upload a scored CSV for a batch already in the base.**

## Batch 2 — Put a spend ceiling on the live search front door  *(#3, #14, #21)*

`collect_raw_rows` seeds caps from `getenv_int("MAX_SEARCH_CALLS_PER_RUN", ...)` etc., with
CLI flags able only to lower them, following the `[STOP] Hit MAX_*` pattern AGENTS.md:143
mandates. Count *pages fetched*, not searches — each page is separately billable.

**Note:** `MAX_SEARCH_CALLS_PER_RUN=3` was written for the legacy `places_search.py` CLI and
is incompatible with any 66-query geo-sliced config. Pick a real default for this path rather
than importing the stale one. Document the 19 undocumented env vars while here. [L-6, L-7]

## Batch 3 — Suppression: one resolver, one writer, a loud bypass  *(#4, #15, #16, #17)*

Four findings, one root cause — the suppression system has readers everywhere and no writer.

1. Have `hermes_queue` resolve through `compliance.load_configured_suppression_index` instead
   of its own `LPM_OUTCOMES_JSONL`-only lookup, or assert at startup that both resolve to the
   same file. [#4]
2. Add a verb that can actually *record* an opt-out. [#16]
3. `ALLOW_MISSING_SUPPRESSION_INDEX` prints `[WARN] suppression bypassed` and writes it to the
   run receipt instead of silently returning an empty set. [#17]
4. `mark_outcome` checks suppression before re-arming the follow-up cadence. [#15]

## Batch 4 — Make the evals capable of passing  *(L-4, L-5)*

Give `completeness` and `gate_liveness` an accepted-debt allowlist so red means *new*. Either
delete `ANGLE_BLOCKER` or widen the corpus to exercise it. Rescore the 80 `stale_evidence`
rows to clear `reproducibility`.

## Batch 5 — Enrichment evidence honesty  *(#5–#10)*

The `review_velocity` / `enrich_leads` cluster: stop reporting confidence 0.85 on an
observation whose state and band are both `unknown`; stop swallowing browser exceptions into
an unmarked "no form"; stop setting a reputation-risk category off one unqualified regex hit.
These feed outreach claims, so correctness here is a compliance surface.

## Batch 6 — Sendability re-derivation  *(#2)*

Make `readiness_flags` treat "cannot re-derive" as a blocker rather than as clearance, so a
stored `Verdict=send` on a row carrying none of the evidence cannot clear `admit()`.

## Batch 7 — Remaining lows

`backfill_packet_tier.py` (#20, #12), `limit_per_query` dead field (#23), the
`run_pipeline.py` stage-1 config mismatch (#24), sqlite cohort_rates recompute (#19).

---

## Decisions that need Lou

1. **`gates.yml` [L-1]** — provision the self-hosted `lpm` runner + repo variable + secrets,
   or restructure the workflow to run the corpus-free gates on `ubuntu-latest` and leave the
   corpus ones as a local `make` target? The second is cheaper and gets *something* running today.
2. **Batch 1's side effect** — is making `Mockup Needed` / `Manual Review` reachable wanted?
3. **The accept-rate number** — if a live read confirms ~3.8%, that is the loop failing its own
   stated pass condition, and it outranks every code fix on this page.
