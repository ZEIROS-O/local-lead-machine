# GPT Suppression Remediation Plan

> Local snapshot copied from Notion on 2026-07-28T11:23:05.383Z.  
> Source: [GPT Suppression Remediation Plan](https://app.notion.com/p/3abc57f0654f809dab6aee9619efdfdb)

# Suppression Review Findings Remediation Plan
## Summary
Fix all four findings inside local-lead-machine: strengthen live identity matching, make scoring-v3 suppression  
profile-independent, isolate missing-config tests from .env, and apply the suppression preflight to legacy search.  
Preserve the existing operator contract that production opt-outs contain phone/email.
## Implementation Changes
### 1. Shared suppression identity hardening
- Write failing regressions for enriched records matched by record_id, lead_id, phone, and scraped email.
- Broaden record_suppression_identifiers(record: Mapping\[str, Any\]) -\> set\[str\] to understand:
	- Flat queue and Airtable records.
	- Nested google and website contacts.
	- Explicit record_id and lead_id.
- Preserve existing id handling for Airtable/flat records, but never interpret Google Place IDs as Airtable IDs.
- Keep record_matches_suppression(...) public signature unchanged; implement it through the shared identifier  
intersection.
- Verify the scorer drops these matches before token spend.
### 2. Missing-profile scoring-v3 parity
- Add one private suppression helper that combines:
	- Enrichment dnc_suppressed.
	- Truthy Suppressed, Opt Out, or Do Not Contact row fields.
- Use it in both the normal-profile and missing-profile branches.
- When the fallback contains any hard disqualifier, return disposition="reject"; otherwise retain manual-review.
- Add regressions for every row-field alias, enrichment-derived suppression, deduplication, and missing-profile  
rejection.
### 3. Entry-point and configuration hardening
- Make legacy places_search.py load the configured suppression index after argument parsing but before API-key  
validation or any paid Places call.
- On missing, unreadable, or malformed suppression input, emit \[ERROR\] … and return 1, matching active search,  
enrichment, and scoring.
- Leave the legacy command otherwise operational and keep --help available without configuration.
- Update .env.example comments to state that suppression is required for search, enrichment, and scoring; preserve  
SUPPRESSION_SOURCE → LPM_OUTCOMES_JSONL precedence and the explicit ALLOW_MISSING_SUPPRESSION_INDEX=1 bypass.
- Do not rewrite the real .env; its configured source already loads successfully.
### 4. Deterministic test isolation
- In EntryPointTests.setUp, stub load_dotenv() for make_search_batch, places_search, enrich_leads, and score_leads.
- Continue clearing environment variables explicitly so missing-source tests cannot inherit operator configuration.
- Assert each failing entry point refuses before search, crawl, scoring, network, or LLM functions are called.
## Test Plan
Run test-first for each batch, then execute:
.venv/bin/python -m unittest \\  
tests.test_dnc_enrichment \\  
tests.test_scoring_v3 \\  
tests.test_icp_lanes \\  
tests.test_lead_machine_contracts
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile \\  
score_leads.py \\  
places_search.py
git diff --check
Acceptance criteria:
- Stale clean records are dropped on current phone, email, record_id, or lead_id matches.
- Google Place IDs cannot create false record-ID suppression.
- Missing-profile scoring-v3 reports suppression and rejects the lead.
- All four entry points fail closed without suppression configuration.
- The full suite passes with the real configured .env present.
- No network, Anthropic, Airtable, or live write occurs during verification.
## Assumptions
- operator-agent remains unchanged because its writer already requires phone/email on every production opt-out.
- Record-ID matching is defense in depth, not a replacement for contact identifiers.
- No artifact migration or historical backfill is required.
- Do not commit, push, or alter the existing untracked search-config file.

