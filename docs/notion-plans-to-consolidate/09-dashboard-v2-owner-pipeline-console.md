# Dashboard V2 — Owner’s Pipeline Operations Console

> Local snapshot copied from Notion on 2026-07-28T11:26:03.789Z.  
> Source: [Dashboard V2 — Owner’s Pipeline Operations Console](https://app.notion.com/p/3abc57f0654f800eb226c642337ac302)

## Summary
Rebuild the dashboard for one audience: the owner/operator of the lead machine. It should explain what the scraper,  
enricher, scorer, gate, packet generator, and Airtable handoff are doing—not reproduce Airtable’s CRM views.
Borrow the useful operational patterns from Apollo and Clay—dense filtering, inspectable rows, explicit stages,  
configurable columns, and grouped alerts—while excluding contacts, sales stages, sequences, replies, and CRM workflow  
reporting. Apollo filters ([https://knowledge.apollo.io/hc/en-us/articles/4412665755661-Search-Filters-Overview](https://knowledge.apollo.io/hc/en-us/articles/4412665755661-Search-Filters-Overview)), Clay table  
columns ([https://university.clay.com/docs/table-columns-overview](https://university.clay.com/docs/table-columns-overview)), Clay alerts  
([https://university.clay.com/docs/table-alerts](https://university.clay.com/docs/table-alerts)).
V2 remains read-only but establishes the contracts needed to add safe pipeline buttons later.
## Information Architecture
### 1. Command Center
The default page answers “What is the machine doing right now?”
- Show canonical pipeline stages: Configured → Discovered → Enriched → Scored → Gate Evaluated → Packet Ready → Airtable  
Handoff Confirmed.
- Display unique lead identities as the primary count and artifact rows secondarily so duplicate processing is visible.
- Make every metric clickable into the exact batches or leads behind it.
- Separate gate outcomes into Sendable, Manual Review, Watchlist, and Rejected/No Packet.
- Add “Needs attention now,” grouped by batch and blocking stage:
	- Missing or malformed artifacts.
	- Identity drift and duplicate rows.
	- Unscored enriched leads.
	- Stale tiers or packets.
	- LLM cap stops and last-run errors.
	- Expired approvals.
	- Airtable warnings, skipped rows, or stale receipts.
- Add a current-state niche/market matrix showing discovered, enriched, scored, sendable, packet-ready, yield percentages,  
LLM cost, and health.
- Show active and blocked work first; move configured-but-unstarted batches into a separate capability inventory.
### 2. Batches
Create a complete operational inventory with preset views:
- All batches.
- Active.
- Unscored.
- Fully scored.
- Packet ready.
- Airtable ready.
- Finished.
- Blocked or stale.
- Configured but not started.
Each row shows niche, market, size, sequence, configured query count, stage, health, discovered/enriched/scored counts,  
unscored count, gate outcome mix, packet count, LLM usage, Airtable receipt state, latest activity, and recommended next  
command.
Batch detail retains the current artifact diagnostics but reorganizes them around:
1. Stage timeline and yields.
2. Current blockers and warnings.
3. Recommended next action with copy button.
4. LLM calls/tokens/cost against caps.
5. Gate and packet outcomes.
6. Airtable dry/live receipt reconciliation.
7. Collapsible artifact manifest and run-state details.
8. Leads belonging to the batch.
### 3. Lead Explorer
Add an owner-facing cross-batch operational table—not a CRM.
Built-in views:
- All enriched leads.
- Unscored.
- Scored.
- Sendable.
- Manual review.
- Watchlist.
- Blocked by evidence.
- Packet ready.
- Waiting for Airtable handoff.
Filters include batch, niche, market, scoring state, priority, packet tier, site status, money signal, commercial-fit  
state, contact eligibility, AI confidence, and blocker type.
Default columns:
- Business, city, niche, and source batch.
- Enrichment/scoring stage.
- Rating and reviews.
- Site status and website quality.
- Priority, total score, and confidence.
- Offer angle and visible-pain score.
- Send trigger and money signal.
- Commercial-fit and contact-eligibility states.
- Sendable, packet tier, blockers, and handoff state.
A deep-linkable side inspector shows score dimensions, pain reasons/categories, buyer-intent evidence, review-demand band,  
commercial case, safe claims, blockers, packet eligibility, batch memberships, schema version, artifact timestamps, and  
identity/conflict warnings.
Phone, email, outreach history, assignment, CRM status, and follow-up fields remain in Airtable and are not duplicated  
here.
### 4. System and Capabilities
Add a dedicated view showing what the software can currently do:
- Supported vertical profiles and aliases.
- Configured niches, markets, batch sizes, and search-query counts.
- Available pipeline stages and scoring mode/model.
- Places and LLM cap readiness without exposing secret values.
- Canonical versus ignored/debug artifact counts.
- Source freshness and last successful scans.
- Pending approvals and active run-state records.
- Dashboard/API version and build information.
- Privacy boundary and source-of-truth definitions.
Every ignored or unrecognized artifact must have an inspectable reason rather than disappearing silently.
## Data and API Changes
- Introduce a single LeadProjection joining canonical discovery, enrichment, scoring, tier, packet, usage, and run-state  
artifacts.
- Resolve identities by exact Place Key first. Use a marked weak fallback based on normalized business name and city; never  
silently merge ambiguous fallbacks.
- Preserve all batch memberships for leads appearing in multiple runs. Select the highest score-schema version and newest  
canonical scored artifact for the displayed qualification, while flagging conflicting values.
- Do not recompute scoring or sendability in the dashboard. Read the canonical outputs from the scoring and deterministic  
gate.
- Define stage membership precisely:
	- Discovered: stable identity in canonical deduped output.
	- Enriched: matching identity in enriched JSONL.
	- Scored: enriched identity matched to a canonical scored row.
	- Gate Evaluated: current tier/sendability fields exist.
	- Packet Ready: current packet-index row matches the current tiered result.
	- Airtable Handoff Confirmed: current clean local live-sync receipt matches packet inputs with zero skipped rows.
- Do not query Airtable records or reproduce CRM state. Airtable is represented only by local dry/live receipts, hashes,  
warnings, and counts.
- Add typed response models and explicit response allowlists.
- Extend the API additively with:
	- GET /api/leads for cross-batch filtering, sorting, and pagination.
	- GET /api/leads/\{lead_id\} for operational inspection.
	- GET /api/issues for grouped machine problems.
	- GET /api/capabilities for vertical/config/environment readiness.
	- snapshot_id, count basis, unique counts, row counts, and conversion fields on existing overview/batch responses.
- Build immutable dashboard snapshots and incremental per-batch caching so overview, batch, issue, and lead responses in  
one refresh cannot disagree.
- Pause polling when hidden/offline, cancel obsolete requests, and prevent overlapping scans.
## Hardening and Future Controls
V2 exposes no execution endpoints. It remains GET-only and cannot start paid work, run commands, or write to Airtable.
Prepare for a later control layer by returning structured available_actions metadata for each batch:
- Action key and human label.
- Current eligibility.
- Blocked reason.
- Estimated affected stage.
- Whether it is paid.
- Whether it writes externally.
- Whether it requires approval.
- Current copyable Hermes command.
A later V3 can turn these into buttons by wrapping the existing Hermes command layer—not arbitrary shell execution—with:
- An allowlisted action registry.
- One active job per batch.
- Dry-run preview before paid or live actions.
- Input hashes and expiring approval tokens.
- Explicit token/dollar caps.
- Two-step confirmation for scoring and live Airtable sync.
- Append-only audit receipts and streamed status/logs.
- No autonomous outreach sending.
Keep localhost binding and authenticated tunnel access. Add strict output allowlists, API no-store, no-index, permissions  
policy, request limits, safe error messages, and tests ensuring non-GET requests fail closed.
## Delivery Sequence
1. Operational data contract: implement lead projection, stable identity reconciliation, precise stage definitions,  
capability inventory, issue grouping, and typed APIs.
2. Owner console: build Command Center, Batches, Lead Explorer, System, drilldowns, column presets, and responsive  
navigation.
3. Reliability: add immutable snapshots, incremental scanning, polling controls, security headers, privacy tests,  
accessibility, and performance work.
4. Cutover: compare V1 and V2 counts side by side, explain every identity-versus-row difference, resolve unexplained drift,  
and then replace the V1 overview.
5. Future control project: only after V2 proves stage/action eligibility accurately, add approval-gated buttons through the  
existing Hermes command contracts.
Do not modify scoring, enrichment, sendability, packet generation, Airtable write behavior, or CRM schema as part of V2.
## Tests and Acceptance
- Test stable identity matching, cross-batch duplicates, weak identities, collisions, conflicting score versions, malformed  
artifacts, stale tiers/packets, state-count drift, and receipt-hash mismatches.
- Assert every aggregate reconciles exactly to its drilldown.
- Assert all canonical configs/artifacts appear as a batch or have an explicit ignored reason.
- Test every preset/filter/sort/pagination combination and deep-linked lead inspection.
- Test stale/error/empty/loading states, keyboard navigation, responsive layouts, and polling cancellation.
- Assert phone, email, openers, raw evidence, secrets, and CRM workflow fields never leak through the API.
- Assert all POST/PUT/PATCH/DELETE requests return a safe rejection.
- Keep make dashboard-test, make dashboard-build, and the full Python suite green.
- Acceptance requires the owner to identify all active batches, unscored work, gate outcomes, errors, current capabilities,  
spend/cap state, and the correct next command without opening raw repository artifacts.
## Assumptions
- The dashboard is exclusively for the owner/operator; reps work in Airtable.
- Airtable remains the authoritative boundary after handoff.
- V2 reports current operational truth only; historical trends wait for an append-only event source.
- “Read-only” prohibits pipeline and Airtable mutations; browser preferences and in-memory caches are allowed.
- Future buttons are a separate control-layer milestone, not part of the V2 implementation.

