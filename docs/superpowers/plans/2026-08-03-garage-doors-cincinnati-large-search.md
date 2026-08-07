# Garage Doors Cincinnati Large Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and run a fresh, capped, large Cincinnati garage-door discovery/enrichment batch using the approved high-intent profile.

**Architecture:** Keep the change in the established vertical-as-data path. Update the garage-door JSON query/exclusion data and the canonical term registry, generate one full Cincinnati config, apply this batch's two-page depth, then run `make_search_batch.py` with explicit paid-call and enrichment caps.

**Tech Stack:** Python 3.10, stdlib `unittest`, JSON search/market profiles, Google Places via `requests`, existing concurrent enrichment pipeline.

## Global Constraints

- Use exactly eight approved high-intent query phrases.
- Use all fifteen geos from `market_profiles/cincinnati.json`.
- Keep rating floor 4.3, review floor 8, missing websites allowed, and no review ceiling.
- Use two pages per query/geo and explicit caps: 120 searches, 150 details, 150 enrichments.
- Stop at discovery and enrichment; do not score, generate packets, write Airtable, or send outreach.
- Preserve unrelated worktree changes and do not commit or push.

---

### Task 1: Protect the approved garage-door profile behavior

**Files:**
- Modify: `tests/test_generate_search_config.py`
- Modify: `vertical_profiles/garage_doors.json`
- Modify: `lead_machine/verticals.py`

**Interfaces:**
- Consumes: `generate_search_config.generate_config(...)` and `VERTICAL_PROFILES["garage_doors"]`.
- Produces: generated garage-door configs with the exact approved queries, canonical replacement boost, and named search-time exclusions.

- [ ] **Step 1: Write the failing test**

Add a Cincinnati full-config test that asserts literal query strings, all fifteen
geos, `min_rating == 4.3`, `min_reviews == 8`, replacement in `boost_terms`, and
the five named franchise/network strings in `exclude_terms`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_generate_search_config.GenerateSearchConfigTest.test_generate_garage_doors_cincinnati_full_uses_curated_intent_and_network_exclusions`

Expected: FAIL because `garage door replacement Cincinnati OH` is absent and the
generated query set/exclusion set does not match the approved literals.

- [ ] **Step 3: Implement the minimal profile change**

Set `vertical_profiles/garage_doors.json` to the eight approved query phrases,
add the named network exclusions, and add `garage door replacement` to
`GARAGE_DOOR_TERMS` in `lead_machine/verticals.py`.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m unittest tests.test_generate_search_config`

Expected: all generator tests pass.

### Task 2: Generate and verify the fresh full config

**Files:**
- Create: `search_configs/garage_doors_cincinnati_full_001.json`
- Potentially refresh without semantic change: `search_configs/geo_slices/cincinnati.json`

**Interfaces:**
- Consumes: the updated garage-door and Cincinnati profiles.
- Produces: a config accepted by `make_search_batch.SearchConfig` with batch name `garage_doors_cincinnati_full_001`.

- [ ] **Step 1: Generate the config**

Run: `.venv/bin/python generate_search_config.py --vertical garage_doors --market cincinnati --size full`

Expected: 8 queries, 15 geos, page size 20, initially generated full depth 3.

- [ ] **Step 2: Apply the approved batch-specific page depth**

Change only `max_pages_per_query` in the generated config from 3 to 2. Do not
change the generator's size defaults or other verticals.

- [ ] **Step 3: Validate config loading and expansion**

Run a read-only Python check through `load_search_config` and `expand_searches`.

Expected: batch `garage_doors_cincinnati_full_001`, 8 queries, 15 geos, 120
expanded searches, page size 20, two pages.

- [ ] **Step 4: Run focused contracts**

Run: `.venv/bin/python -m unittest tests.test_generate_search_config tests.test_search_workflow tests.test_lead_machine_contracts.LeadMachineContractsTest.test_hard_icp_gate_has_a_floor_and_no_ceiling tests.test_lead_machine_contracts.LeadMachineContractsTest.test_the_review_floor_bites_at_seven_and_admits_at_eight`

Expected: all tests pass.

### Task 3: Run and audit the capped batch

**Files:**
- Create: `data/raw/garage_doors_cincinnati_full_001.csv`
- Create: `data/deduped/garage_doors_cincinnati_full_001.csv`
- Create: `output/enriched_garage_doors_cincinnati_full_001.jsonl`
- Create: `reports/search_garage_doors_cincinnati_full_001.md`
- Update runtime state: `run_state/review_velocity.sqlite3`

**Interfaces:**
- Consumes: `search_configs/garage_doors_cincinnati_full_001.json`, configured Places key, and configured suppression source.
- Produces: discovery/enrichment artifacts and a checker-visible search report.

- [ ] **Step 1: Run the current search front door**

Run: `.venv/bin/python make_search_batch.py --config search_configs/garage_doors_cincinnati_full_001.json --batch garage_doors_cincinnati_full_001 --no-screenshots --max-searches 120 --max-details 150 --max-enrich 150`

Expected: suppression loads before paid calls; the command completes or reports a
specific fail-closed/runtime error while retaining streamed partial enrichment.

- [ ] **Step 2: Audit run telemetry and lead noise**

Read the report and artifacts; reconcile raw, deduplicated, ICP-pass, skipped, and
enriched counts. Inspect business names, skip reasons, franchise/network leakage,
missing/broken/no-website leads, enrichment errors, and search efficiency.

- [ ] **Step 3: Run final verification**

Run: `.venv/bin/python -m unittest discover -s tests`

Run: `git diff --check`

Expected: full test suite passes and changed source/config/docs contain no diff
whitespace errors.
