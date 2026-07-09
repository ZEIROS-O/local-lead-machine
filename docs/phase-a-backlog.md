# Phase A — Find good leads + run without you

> **Status:** planned, not started (created 2026-06-20).
> **This is the active work for this repo.** Agents: read this before changing scoring,
> enrichment, ICP, search configs, or the pipeline. Work the groups **in order** (G1 → G4);
> respect the blocking links. Keep each group a small, reviewable, independently-revertible batch.

## Why this exists (the problem)

The pipeline is `places_search.py` → `enrich_leads.py` → `make_llm_batch.build_batch_prompt()`
→ **manual copy/paste into Claude** → `import_llm_score_bundle.py` → `score_leads.py`
→ `sendability.py` → `make_packets.py` → Airtable. Two things make it *not a loop*:

1. **Manual scoring breaks it.** It halts at `run_pipeline.py:185-189` waiting for a human to paste
   JSON scores. No automated step = no real loop.
2. **No buyer-money signal.** Enrichment detects *site weakness* but nothing about whether a business
   *has money + growth intent*. So the funnel surfaces broke contractors with bad sites
   ("doesn't find good leads").

Phase A fixes both and closes the loop: automate scoring via the Anthropic API, **switch the live
vertical from hardscaping to car detailing** (multi-metro-ready), add a buyer-intent signal + scoring
axis, and run the whole thing as one capped command with a per-run summary.

## Locked decisions

- **Vertical:** switch to **car detailing first**, targeting *high-ticket* shops (ceramic coating /
  paint correction / PPF / mobile detailing) — **not** basic/express car washes. Epoxy flooring is a
  planned fast-follow → adding a vertical must be a **data change, not code surgery**.
- **Geo:** start **Cleveland metro**, designed to expand (Columbus / Cincinnati / Pittsburgh). Search
  configs must be **metro-parameterized**.
- **Buyer signal:** **reliable-first** — CRM-stack + review-velocity are solid axes now; ads-scraping
  is best-effort and must **degrade gracefully** (never break the pipeline).
- **LLM:** **Claude Sonnet 4.6** (`claude-sonnet-5`), set via `.env` `LLM_SCORING_MODEL` (swappable).
- **API key:** `ANTHROPIC_API_KEY` lives in `.env`.

## Architecture note — two scoring systems, reconciled

Two scorers exist; keep them distinct:
- **Manual/API-LLM path** (`make_llm_batch` + `lead_machine/manual_scores.py`): the LLM returns
  subscores (`icp_fit`, `reputation_demand_proof`, `web_conversion_pain`, …). **Primary** path; G1 automates it.
- **Deterministic path** (`score_leads.py`: `score_business_strength` + `score_online_weakness`): rule-based, `--mode deterministic`.

Buyer-intent (G3) flows **two ways**: (a) the `buyer_intent` evidence block feeds `build_batch_prompt()`
so the LLM's subscores reflect it; **and** (b) a deterministic `money_signal` gate in `sendability.py`
+ a `score_buyer_intent()` axis in the deterministic scorer. The sendability money-gate is the
objective verifier enforcing "fixable gap × money signal" in **both** modes.

---

## G1 — Automate scoring (API-LLM) · backlog issue #1 · do first, no blockers

Smallest highest-value cut: kills the clipboard, adds automated verification, turns the pipeline into
a loop. Purely additive (new mode) → lowest regression risk.

**Tasks**
- [x] **`lead_machine/llm_client.py`** — `score_batch(prompt, *, model, max_tokens) -> dict` POSTs to
  the Anthropic Messages API (`https://api.anthropic.com/v1/messages`) via the existing `requests`
  dep (**no SDK**). Mirror the HTTP helper at `places_search.py:170` (`request_json`); headers
  `x-api-key`, `anthropic-version`; error-on-4xx. Read `ANTHROPIC_API_KEY` + `LLM_SCORING_MODEL` via
  `os.getenv` after `load_dotenv()` (pattern: `places_search.py:272`). Parse the model text as the
  score **bundle** `{"batch_id", "scores":[…]}` — exact shape
  `import_llm_score_bundle.validate_bundle_scores()` expects. Return `usage` tokens for cost tracking.
- [x] **Caps** — `MAX_LLM_CALLS_PER_RUN`, `MAX_LLM_TOKENS_PER_RUN`, `MAX_LLM_USD_PER_RUN`. Mirror
  `places_search.py` `getenv_int` (lines 85-93) + `[STOP] Hit MAX_*` break pattern (lines 304-330).
  Track cumulative calls/tokens/$ across batches; estimate $ from Sonnet 4.6 pricing. On cap: stop
  issuing batches, write partial results.
- [x] **`--mode api-llm`** in `score_leads.py` (alongside `manual-llm` at `score_leads.py:1280`):
  build packets + `build_batch_prompt()` verbatim → `llm_client.score_batch()` (cap-gated) → write
  bundle through existing `import_score_bundle()` (`import_llm_score_bundle.py:119`, schema/validation
  unchanged) → fall through to existing `score_records_manual_run()` read/merge → identical CSV.
- [x] **`.env`** — add `ANTHROPIC_API_KEY`, `LLM_SCORING_MODEL=claude-sonnet-5`, the three `MAX_LLM_*` caps.

**Done when:** `score_leads.py --mode api-llm` yields the **same scored CSV** as the manual path, zero
copy-paste; `--mode manual-llm` still works as fallback.

**Verify:** unittest stubbing `llm_client.score_batch` to return a recorded bundle, asserting the
api-llm CSV is byte-identical to `manual-llm` fed the same bundle. Deterministic, no network.

---

## G2 — Switch live vertical to car detailing · backlog issue #3 · foundational, must land green

Extract hardscaping-hardcoded term sets into a **single per-vertical profile** so detailing (and later
epoxy) is a data change. **Highest regression risk** — guard with existing tests after every step.

**Tasks**
- [ ] **`lead_machine/verticals.py`** — `VerticalProfile` dataclass (`key, label, family, batch_terms,
  service_terms, high_ticket_terms, deny_terms, low_ticket_terms, franchise_denylist, exclude_terms,
  geo_terms`) + `VERTICAL_PROFILES` + `resolve_profile(vertical, *, source_query="",
  fallback="hardscaping")` (normalize like `schema.infer_vertical` at `schema.py:68`; never KeyError).
  **Hardscaping profile = current constants verbatim** (from `icp_lanes.HARDSCAPE_BATCH_TERMS`/
  `HARDSCAPE_SERVICE_TERMS`, `enrich_leads.HIGH_TICKET_TERMS`, `icp.FRANCHISE_DENYLIST`/
  `LOW_TICKET_TERMS`/`MEDICAL_AESTHETIC_TERMS`/`TARGET_GEO_TERMS`) → **zero behavior change**.
- [ ] **Detailing profile** — target: `ceramic coating, paint correction, ppf, paint protection film,
  auto/car/mobile detailing, ceramic pro, xpel, gtechniq, window tint, vehicle wrap, paint
  enhancement`; deny/low-ticket: `express wash, car wash tunnel, automatic/self-serve car wash, oil
  change, quick lube, gas station, auto parts, tire shop, body shop/collision, dealership`; franchise
  denylist: `mister car wash, take 5, tommy's express, zips, quick quack, delta sonic, …`;
  `exclude_terms = ∅`; `geo_terms` = Cleveland metro set.
- [ ] **Consume the profile** (signatures unchanged → call sites don't move):
  - `lead_machine/icp.py` `hard_icp_gate` (66-94): resolve per-lead; swap 4 module constants for
    `profile.*`. Reason strings unchanged (`franchise_or_chain`, `low_ticket_economics`).
  - `lead_machine/icp_lanes.py`: rename `hardscaping_batch`→`target_batch`,
    `hardscape_service_evidence`→`target_service_evidence`; store `profile` on `_LaneContext`; point
    `_is_target_batch`, `_service_fit_points`, `_strong_project_value_fit`, `_hard_blockers` at
    `ctx.profile.*`. **Resolution keyed off the record's `vertical`/`source_query`** (tests' `_record()`
    default is `vertical="hardscaping"`) — **no global "active vertical."**
  - `enrich_leads.py`: thread `high_ticket_terms=profile.high_ticket_terms` into `scrape_site`
    (default `None` → hardscaping list); resolve in `enrich_row` (~1187).
  - `score_leads.py`: resolve in `score_record` (950); `fit_ceiling` (312)/`score_business_strength`
    (378) read `profile.franchise_denylist`/`profile.high_ticket_terms`; `vertical_family` → `profile.family`.
- [ ] **Multi-metro configs** (`search_configs/`) — naming `<vertical>_<metro>_<size>.json`
  (`detailing_cleveland_full_001.json`, `detailing_cleveland_medium_001.json`). High-ticket queries;
  `exclude_terms` strip express/tunnel washes + lube + dealer service depts; `boost_terms` =
  high-ticket terms; `min_rating: 4.3` (match gate `icp.py:88`), `min_reviews` comes from
  `VerticalProfile`/profile JSON (default 10; detailing max-review cap = 130). **Metro expansion:**
  optional `geo_slices_ref` key in `make_search_batch.SearchConfig.from_dict` (117) loading
  `search_configs/geo_slices/<metro>.json` (shared array); inline `geo_slices` keeps working.

**Done when:** configs + ICP gate + lane terms target detailing; existing tests stay green (hardscaping
profile preserved); a detailing search returns ceramic-coating/PPF shops, not washes.

**Regression guard:** `tests/test_icp_lanes.py` asserts hardscaping behavior (e.g. score ≤ 64 cases,
~lines 169-233). They pass **only if** the hardscaping profile copies old term sets verbatim and
resolution is per-record. Run `python -m unittest tests.test_icp_lanes tests.test_lead_machine_contracts`
after each refactor step.

---

## G3 — Buyer-intent signal + scoring axis · backlog issues #2 + #4 · blocked by G2

**Tasks**
- [ ] **`lead_machine/buyer_signals.py`** — `build_buyer_intent(record, *, ads_check=False)` attaches
  `record["buyer_intent"]` (all keys always present):
  - `crm_booking_stack` / `has_paid_crm`: reuse `evidence._detect_from_text` against extended
    `evidence.THIRD_PARTY_TOOLS` (add ServiceTitan, Thryv + detailing tools Urable, Booksy, Square
    Appointments, Mobile Tech RX); mirror tokens into `enrich_leads.FORM_PROVIDER_TERMS`.
    `has_paid_crm = stack ∩ PAID_CRM ≠ ∅`. (Jobber/Housecall already detected.)
  - `review_velocity`: `velocity_band` (`live`/`steady`/`thin`) from existing Google `reviews` count +
    `rating`, `source="count_only"`. *Optional upgrade:* add `reviews` to
    `places_search.PLACE_DETAILS_FIELD_MASK` (49) + `review_recency` → recency-based `live`; gate
    behind a flag (higher Places SKU cost).
  - `ads` (best-effort, **default off** via `BUYER_INTENT_ADS_CHECK`): Google Ad Transparency by
    domain, Meta Ad Library by FB page (from `social_links`), LSA badge. Each in try/except, short
    timeout, failure → `"unknown"` + `ads.errors`; **never raises**.
  - `money_signal` = `has_paid_crm OR any ads=="yes" OR velocity_band=="live"`.
  - Attach in `enrich_leads.enrich_row` **after** `build_evidence_profile` (~1256).
- [ ] **`score_buyer_intent(record) -> (int, [evidence])`** in `score_leads.py` reading the block.
- [ ] **Single `SCORE_WEIGHTS` block** in `score_leads.py` holding **all** weights (existing axes +
  buyer-intent sub-weights + priority thresholds). Reweight to keep a 100-pt envelope (e.g. strength
  40→30, weakness 40→35, buyer-intent 15, offer 20). Fold `money_signal` into
  `fit_ceiling`/`classify_priority` (money lets a high-review business hold a tier higher; no money +
  bad site caps at C).
- [ ] **`sendability.py` retune** — new `MONEY_SIGNAL_BLOCKER` in `EVIDENCE_BLOCKERS`. **Relax:**
  `money_signal AND concrete_issue` → skip `VISIBLE_PAIN_BLOCKER`; count toward `has_demand_signal`.
  **Tighten:** no money + only-signal-is-bad-site → `MONEY_SIGNAL_BLOCKER`. Surface `Money Signal` +
  `Buyer Intent Signals` columns; if pushed to Airtable, add to `upload_airtable.py` field allowlist.
  Feed the `buyer_intent` summary into `build_batch_prompt()` evidence so LLM subscores see it.

**Done when:** every enriched lead carries `buyer_intent` with ≥ CRM + review-velocity (ads
best-effort); A-grade = fixable-gap × money-signal; spot-check 10 leads — broke/no-intent drop to C/Skip.

**Verify:** unit-test `build_buyer_intent` (ServiceTitan in text → `has_paid_crm`; `ads_check=False` →
all "unknown", no network). Sendability tests: (a) weak-form + money_signal → Sendable=Yes; (b) bad
site + no money → `MONEY_SIGNAL_BLOCKER`, Sendable=No.

---

## G4 — Close the loop: one capped command + summary · backlog issue #5 · blocked by G1

**Tasks**
- [ ] Default `run_pipeline.py` to `--mode api-llm`; remove the manual-wait stop (`run_pipeline.py:185-189`)
  when in api-llm mode (keep it for `manual-llm`).
- [ ] Fold the Airtable push (`upload_airtable.py` / `lead_machine/airtable_sync.py`) into the pipeline
  tail, **gated by `sendability`** + the **hard budget cap** (`MAX_LLM_*` from G1).
- [ ] **Per-run summary** (extend counts at `run_pipeline.py:251-269`): leads in · A/B count · sendable
  count · **$ spent / tokens / LLM calls vs caps** · packet + Airtable row counts.

**Done when:** one command produces sendable packets + Airtable rows, no manual step, under a token/$
cap — the first real loop (passes the 4-condition test: repeats weekly · automated verification ·
budget cap absorbs waste · senior-engineer tooling).

**Loop-contract mapping:** trigger = the single command (scheduling is Phase C); state =
`output/*.jsonl|*.csv` + scores_dir + summary; verifier = G1 parity test + `sendability` money/quality
gate (judged on evidence, not the LLM's "done"); halt = `MAX_LLM_CALLS/TOKENS/USD_PER_RUN` + existing
search/details caps.

---

## End-to-end verification

1. `python -m unittest` green after **each** G2 refactor step (watch `test_icp_lanes.py`).
2. G1 parity unittest (stubbed `score_batch`): api-llm CSV == manual-llm CSV.
3. `build_buyer_intent` unit test (CRM detect; `ads_check=False` → no network).
4. Sendability tests: weak-form+money → Sendable; bad-site+no-money → blocked.
5. Live smoke: `python run_pipeline.py --batch detailing_cleveland_medium_001 --vertical detailing`
   (api-llm) — runs straight through under cap, returns detailing shops (not washes), emits packets +
   Airtable rows + $-spend summary, `buyer_intent.ads.errors` stay non-fatal.

## Out of scope (Phase A)
- Phase B (outreach) and Phase C (scheduling / feedback loop).
- Epoxy vertical (fast-follow; the `verticals.py` + multi-metro config design makes it a data add).
- Full first-class ads-scraping (best-effort / off by default per "reliable-first").
