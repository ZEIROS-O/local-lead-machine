# Scheduled Review Velocity and Longitudinal Foundation

> Local snapshot copied from Notion on 2026-07-28T11:33:25.192Z.  
> Source: [Scheduled Review Velocity and Longitudinal Foundation](https://app.notion.com/p/3abc57f0654f80959dcefe77878703a6)

## Summary
Enable real 14-day review velocity for the 98 existing garage_doors__cleveland businesses without rerunning  
discovery, enrichment, or outreach. Add bounded Google count observations, durable run receipts, lifecycle-aware  
identity comparison, and an unattended Hermes schedule.
Review velocity remains demand evidence only: it cannot become money_signal, relax sendability, activate Score v3,  
or trigger outreach.
## Implementation Changes
### 1. Observation state and provenance
- Extend ReviewSnapshotStore through a non-destructive, idempotent migration:
	- Preserve every existing snapshot and the legacy source interface.
	- Add explicit provider, captured_by, and run_id provenance.
	- Add cohort-membership and observation-run tables.
	- Use atomic due-member claims with a two-hour stale-claim recovery window to prevent concurrent duplicate  
observations.
- Back up run_state/review_velocity.sqlite3 before live migration.
- Bootstrap a frozen garage_doors__cleveland cohort from the 98 distinct Place IDs observed on July 28, 2026. Do not  
silently include later records.
- Set each member’s next_due_at to its latest successful observation plus 14 full days.
- Store only Place ID, aggregate review count, rating, timestamps, cohort, and provenance—never the raw Google  
response.
### 2. Provider and command interfaces
Add a provider-neutral ReviewCountProvider interface and Google adapter with:
ReviewCountObservation:  
place_key  
review_count  
rating  
observed_at  
provider
The Google request must use the minimal field mask id,userRatingCount,rating, existing HTTP/retry conventions, and  
sanitized errors.
Add this CLI:
.venv/bin/python observe_review_velocity.py bootstrap \\  
--cohort garage_doors__cleveland \\  
--vertical garage_doors \\  
--market cleveland \\  
--as-of 2026-07-28T23:59:59Z
.venv/bin/python observe_review_velocity.py status \\  
--cohort garage_doors__cleveland
.venv/bin/python observe_review_velocity.py run \\  
--cohort garage_doors__cleveland              # dry-run
.venv/bin/python observe_review_velocity.py run \\  
--cohort garage_doors__cleveland --execute
.venv/bin/python observe_review_velocity.py probe \\  
--place-id \<known-place-id\>                    # network read, no persistence
Contracts:
- Dry-run performs zero HTTP calls and zero database writes.
- run --execute performs zero HTTP calls when nothing is due.
- Exit 0 for PASS or NOOP, 1 for operational/coverage failure, and 2 for policy, configuration, or budget HOLD.
- Receipts go under run_state/review_observation_runs/\<date\>/\<run_id\>.json and include coverage, attempts, failures,  
snapshot writes, price assumption, estimated cost, timestamps, and SHA-256.
- Never print API keys, raw responses, or sensitive environment values.
Hard defaults:
REVIEW_OBSERVATION_MIN_INTERVAL_DAYS=14  
REVIEW_OBSERVATION_WORKERS=4  
MAX_REVIEW_OBSERVATIONS_PER_RUN=100  
MAX_REVIEW_OBSERVATION_HTTP_ATTEMPTS=110  
MAX_REVIEW_OBSERVATION_USD_PER_RUN=2.20  
ALLOW_GOOGLE_REVIEW_COUNT_RETENTION=0
Maintain a priced-provider table and fail closed if the selected provider is unpriced. The \$2.20 cap assumes the  
current worst-case \$0.02 per Places Details Enterprise request, without relying on free allowance; re-verify against  
Google’s current pricing ([https://developers.google.com/maps/billing-and-pricing/pricing](https://developers.google.com/maps/billing-and-pricing/pricing)) before activation.
### 3. Reporting, scoring boundary, and lifecycle correctness
- After every due run, generate a cohort report containing:
	- due, attempted, succeeded, failed, and coverage;
	- count increases, unchanged counts, and decreases;
	- reviews per 30 days, cohort percentiles, velocity bands, unknowns, and anomalies.
- Reuse the existing ≥14-day velocity algorithm and ≥20-member cohort rule.
- Treat decreases as anomalies requiring inspection, never as negative demand evidence.
- New enrichment/scoring runs consume the stored history normally.
- Keep Score v3 in shadow and assert that velocity affects demand_signal only—not tri-state money_signal or  
sendability.
Add a provider-independent Place-ID set reducer and persistence contract:
new_candidate  
confirmed_present  
missing_candidate  
confirmed_missing  
reappeared
Rules:
- Compare Place-ID sets, never output row counts or ordering.
- Require the same cell/config hash; a changed search definition starts a new baseline.
- Require two consecutive sightings before confirmed_present.
- Require two consecutive absences before confirmed_missing.
- Use “observed” terminology, never claim a business was born, opened, or closed.
- Implement and test the reducer now, but do not activate its network discovery producer in this rollout because  
Google Text Search results are not guaranteed to be consistent between identical requests. Google Text Search  
documentation ([https://developers.google.com/maps/documentation/places/web-service/text-search](https://developers.google.com/maps/documentation/places/web-service/text-search))
Replace the roadmap’s row-count change detector with this contract. Reconcile the stale Phase A status and document  
the observer, policy gate, runbook, receipts, rollback, and deferred experiments.
### 4. Policy gate and Hermes scheduling
Create a machine-readable policy decision record for retaining aggregate Google review counts. It begins status:  
pending; neither the implementer nor scheduler may mark it accepted implicitly. Google explicitly permits Place ID  
retention, but aggregate review counts do not have the same clear exemption, which is why activation remains  
explicit. Google Maps Platform policies  
([https://developers.google.com/maps/documentation/places/web-service/policies](https://developers.google.com/maps/documentation/places/web-service/policies))
In operator-agent:
- Add a repo-owned [operator-review-velocity.sh](http://operator-review-velocity.sh/) runtime wrapper using the configured LPM_REPO and LPM’s .venv/bin/  
python.
- Register it with the existing runtime installer and contract checks.
- Add a no-agent Hermes job named review velocity observer:
	- schedule: 45 5 \* \* \* in the host’s New York timezone;
	- workdir: /home/lucascoh/operator-agent;
	- Telegram delivery: existing owner target;
	- remain paused until the policy record is accepted and ALLOW_GOOGLE_REVIEW_COUNT_RETENTION=1.
- Suppress Telegram output for NOOP, while still leaving a receipt and Hermes last_run_at.
- Deliver due-run summaries and errors. Confirm the existing 08:15 lateness watchdog detects a missed or failed  
observer fire.
- Keep this job isolated from top-up, the morning queue, Airtable write-back, and all sending behavior.
Rollback is: pause the Hermes job, set the cohort inactive, and unset the acknowledgement flag. Do not delete  
history automatically; any policy-driven purge requires a separate approved deletion plan.
## Test and Rollout Plan
1. Unit and contract tests
	- Idempotent migration preserves the current snapshot API and all rows.
	- Bootstrap selects exactly the frozen 98-member cohort.
	- Due calculation observes 14 full days.
	- Dry-run/no-op makes no network calls.
	- Policy, pricing, request, and dollar caps fail closed.
	- Only transient HTTP failures retry, without exceeding 110 attempts.
	- Atomic claims prevent concurrent duplicate work and recover stale claims.
	- Partial API failures remain due; successful members advance their due date.
	- Database write failure always fails the run.
	- Count decreases surface as anomalies.
	- Place-ID ordering does not create changes; repeat appearance/absence and reappearance produce the specified  
lifecycle states.
	- Velocity cannot create money_signal or make a lead sendable.
2. Regression commands
.venv/bin/python -m unittest \\  
tests.test_review_observer \\  
tests.test_scoring_v3 \\  
tests.test_search_workflow \\  
tests.test_lead_machine_contracts
.venv/bin/python -m unittest discover -s tests
Run the Operator suite and runtime contract check from its repo:
python3 -m unittest discover -s tests  
python3 scripts/operator_contracts.py check --runtime
1. Activation sequence
	- Run one non-persisting live probe to validate the Google field mask.
	- Approve the policy record, set the acknowledgement flag, install the runtime wrapper, and enable cron.
	- Require one natural scheduled NOOP fire; a manual trigger is not production proof.
	- Because the seed observations were recorded July 28 and cron runs at 05:45 ET, expect the first fully due fire  
on August 12, 2026.
	- Call velocity operational only after that natural fire produces:
		- at least 89 of 98 second observations;
		- zero snapshot-write failures;
		- estimated spend no greater than \$2.20;
	- Failed members retry on the next daily fire without re-querying successful members.
## Assumptions and Deferred Work
- Preserve the current Airtable cutover changes and the untracked garage-door search config; this implementation  
does not modify them.
- No commit, push, Airtable mutation, autonomous outreach, or sending is included.
- DataForSEO remains a future provider adapter for deeper timestamped reviews; it is unnecessary for forward count-  
delta velocity.
- Geo-grid discovery requires a separate pilot showing at least 15% improvement in qualified unique Place IDs per  
dollar.
- Any future “newly observed” discovery activation requires at least 90% human-verified precision across its first  
50 flags.
- Mailbox verification, deeper review-text ingestion, primary Score v3 activation, and autonomous sequencing remain  
separate gated changes.

