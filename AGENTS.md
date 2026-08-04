# Agent orientation — local-lead-machine

This repo finds local-service businesses with weak websites and scores them as outreach leads:
search → `enrich_leads.py` → `make_llm_batch` → score (`score_leads.py`) → `sendability.py` gate →
`make_packets.py` → Airtable. Core logic lives in `lead_machine/`.

**Two search front doors, and they are not interchangeable.** `make_search_batch.py --config
<search config>` is the current one — it takes a JSON search config with geo slices, paginates,
applies the ICP prefilter, dedupes against the batch and enriches in the same pass. It imports the
Places API layer (`text_search_paged`, `place_details`, `normalize_place`) from `places_search.py`,
so that module is live library code. `places_search.py`'s **CLI entry point** (`main()`) is a
separate thing and is marked LEGACY in its own docstring: it reads a hand-maintained
`data/searches.csv` that nothing produces, and it does not prefilter, dedupe or enrich.
`run_pipeline.py` stage 1 still shells out to that legacy CLI — a known
inconsistency, deliberately not fixed in `b601a92` because `make_search_batch` is not a drop-in
(different flags, derives its own paths, enriches inline, so stage 2 would double-scrape). Treat
`run_pipeline.py` as the older path and the search config as the current one. New search behaviour
belongs in `make_search_batch.py`.

## The roadmap (one machine, four nested loops)

This repo is the first instance of an automated outreach loop: **Discovery & Scoring (A)** →
**Asset Generation (B)** → **Sequencing & Reply-Routing (C)** → **Feedback (D)**. Build in dependency
order; each loop is independently revertible.

- **Phase A — Loop A (G1–G4 shipped; hardening continues):** find good leads + close the scoring
  loop. Design record → **[`docs/phase-a-backlog.md`](docs/phase-a-backlog.md)**.
- **Phase B — Loops B/C (not built):** asset generation (the moat) + outreach sequencing.
  Build order → **[`docs/phase-b-c-roadmap.md`](docs/phase-b-c-roadmap.md)**.
- **Phase C — Loop D (partly instrumented):** scheduling + outcome feedback.
- Strategic north-star (the *why*, end-state, EV, compliance, game-theory) →
  **[`docs/outreach-loop-strategy.md`](docs/outreach-loop-strategy.md)**.

### Non-negotiable invariants (apply to every loop)
- **Maker ≠ checker.** An independent verifier on an *objective* gate (`lead_machine/sendability.py`)
  decides what ships. No send without a real, named, verifiable leak. KPI = accept-rate; below ~50% the loop is losing.
- **Quality is a gate, not a throughput knob.** Automate the *personalized asset*, keep the *message* human-grade. Never optimize raw message volume.
- **Hard stops on the outside:** token/$ caps, opt-out suppression, reply-stops-sequence.
- **Fail-closed compliance**, email-first; never auto-blast cold SMS (TCPA) without consent.
- **🚦 Gate 0 — do NOT build autonomous *sending* before a first close.** The offer is unvalidated
  (zero closes); automating it scales rejection. Pre-fit, the loop's value is *learning rate, not
  revenue*. Finish Loop A, hand-build assets, send manually until ~3–5 closes prove the offer.

## Active work — read before touching scoring/enrichment/ICP/configs/pipeline

**[`docs/phase-a-backlog.md`](docs/phase-a-backlog.md)** is the design record for Loop A. Its four
groups (G1→G4) all shipped, so it is not a to-do list any more — but it is where the *reasoning*
behind scoring, enrichment, ICP, configs and the pipeline lives, and its "Landed since" section
carries the correctness rules added after G4. Read it before touching any of those. Keep each change
a small, independently-revertible batch.

Key in-flight facts (2026-07-29):
- **Seven registered verticals across fifteen metros**: hardscaping, car detailing, epoxy, plumbing,
  water restoration, garage doors, **roofing** (roofing and epoxy became real profiles in `c53b241`
  instead of silently resolving to hardscaping). Per-vertical terms live in
  `lead_machine/verticals.py` and per-metro place names in `market_profiles/*.json` — add verticals
  and metros as **data, not code**, via the `add-vertical` / `add-metro` skills. Nothing in scoring
  should name Cleveland or a single trade; `resolve_profile` and `infer_market` exist for that.
- **Term matching is left-boundary aware, not bare substring** (`c53b241`). Plain `in` matching read
  a term that was only the *tail* of a longer word as a hit, so registering `roofing` classified
  every *waterproofing* business as roofing. The boundary is left-only on purpose — lists are
  written singular and rely on `gutter` matching `gutters`. A collision-audit test fails if a new
  term reintroduces the class.
- **Review floor 8, no review ceiling** (`82a5716`). `max_reviews` is gone as a concept: the one
  booked call to date (2026-07-28) was a 222-review garage-door business, which a ceiling would have
  thrown away. Maturity is a *band* (`buyer_signals.REVIEW_DEMAND_THRESHOLDS`), not a gate. The
  floor lives on two surfaces that must move together — `VerticalProfile.min_reviews` (score-time,
  read by `icp.hard_icp_gate`) and `vertical_profiles/*.json` (search-time, read by
  `generate_search_config.py`) — and a test fails if they disagree.
- **`sendability.admit()` is the only place a lead may be declared sendable.** It re-reads the ICP
  verdict from the row rather than trusting the caller's blocker list, because a stored
  `Sendable: Yes` from an earlier scoring run used to ride into the send queue after the lead
  acquired a skip. Any new sendable path goes through `admit`.
- **No Airtable write may set `typecast`** (`91c7131`). It minted a select choice for any string
  handed to it — that is how `Lead Priority` acquired an option named `(440) 537-2459`. A repo-wide
  AST scan in `tests/test_mark_outcome.py` fails the suite if any shipped module reintroduces it, so
  a new write path is covered without editing the test. The consequence is that **every select
  choice must be pre-created in the base by hand** — verified 2026-07-30, the Airtable API cannot
  add a choice to an existing select at all (`PATCH .../fields/{id}` takes `description` but 422s on
  `options.choices`, five body shapes tried). So **adding a value to
  `mark_outcome.REQUIRED_SELECT_CHOICES`, `mark_outcome.OPERATOR_REQUIRED_SELECT_CHOICES`,
  `queue_state.PROSPECT_STATUS_CHOICES`, or `LPM_SETTER_ROSTER` is half a change**: the base has to
  be edited by hand too, or that write path 422s. `airtable_required_choices.py check` catches it —
  exit 2 names the missing choice and prints the UI steps. The pre-MVP vocabulary was brought level
  on 2026-07-30; the MVP additions (`Do Not Contact`, `Booked`, `Held`, and the setter roster) are
  code-declared but not verified live, so do not claim the checker currently exits 0. Background in
  [`docs/airtable_schema_required.md`](docs/airtable_schema_required.md); full record in
  [`docs/airtable_cutover_runbook_2026-07-30.md`](docs/airtable_cutover_runbook_2026-07-30.md).
- **Scoring runs on the Anthropic Messages API** (`claude-sonnet-5`, `LLM_SCORING_MODEL` in `.env`,
  no SDK — uses `requests`). Spend caps price the resolved model from
  `score_leads.LLM_PRICING_USD_PER_MILLION_TOKENS`; an unpriced model fails the run rather than
  being costed as something else, so a new model needs a table entry.
- **`buyer_intent.money_signal` is tri-state** (`yes`/`no`/`unknown`). "unknown" means there was no
  crawlable site text to read a paid stack from, which is not the same claim as "does not spend".
  Only `yes` may count as truthy in a gate — `sendability.required_visible_pain_score` drops the
  mature-band threshold from 40 to 35 on a truthy value. `demand_signal` is deliberately separate.
- **Enrichment runs concurrently** (`ENRICH_WORKERS`, default 6; 3 when screenshots or rendered
  checks are on, since each launches its own Chromium) and streams records to disk as they finish.
  The per-lead ceiling scales with the worker count — it is wall-clock, and contention inflates it.
- **A lead's crawl is budgeted, and the budget is evidence.** `scrape_site` fetches the homepage
  plus at most `MAX_CANDIDATE_PAGE_FETCHES` candidate pages, in two passes: the pages the homepage
  links, then the `CONTACT_ESTIMATE_PATHS`-style guesses. **The fallback is gated on a linked page
  that actually resolved, not on one merely existing** — a HubSpot template emitting an unrendered
  `/f/contact/$hs_path` matches the contact keywords and 404s, and the guess it would have
  suppressed (`/contact`) was the page really serving the form. The guesses are taken round-robin
  across keyword groups so the eleven contact paths cannot starve the services/about/gallery
  probes. `fetch_url` retries 5xx but not 404 — a 404 is a deterministic answer. Cutting fetches
  only ever removes evidence, and every consumer treats missing evidence as a downgrade
  (`gallery_visibility_score` falls to `Weak`/`None`, `cta_visibility_score` to `Weak`, the
  `no_*_detected` flags fire). Keep it that way: a change here must never let *less* crawling
  produce a *better* grade.
- **Review velocity needs two observations ≥14 days apart** in `run_state/review_velocity.sqlite3`
  (`REVIEW_SNAPSHOT_DB`). `enrich_row` banks one per lead per run; until a batch has been run twice,
  `velocity_band` is legitimately `unknown`.

## The write-back half (post-send CRM)

The pipeline ends at Airtable; three small scripts move a lead through the funnel afterwards, and
every one of them logs something a human already did. **None of them send anything.**

- `query_ready_to_send.py` — read-only queue of leads passing the readiness contract.
- `mark_sent.py` — `Status=Sent`, `Last Contacted`, `Outreach Channel`, and arms `Next Follow-Up`
  one cadence step out.
- `mark_outcome.py` — everything after that: `replied` / `meeting_booked` → `Follow-Up` (re-arms the
  cadence), `closed_won` → `Won`, `closed_lost` → `Lost` (both clear it), plus a
  `Sales Activities` row and, on a win, a `Deals` row.

The cadence itself lives in `lead_machine/follow_up.py` (`FOLLOW_UP_CADENCE_DAYS`, default 4) so the
two writers cannot disagree about it. Table names and the exact select vocabularies these scripts
write are in [`docs/airtable_schema_required.md`](docs/airtable_schema_required.md).

Two read-only tools guard the base against the code drifting ahead of it. Both exited 0 for the
pre-MVP schema on 2026-07-30. The MVP select additions have not been created or verified live; a
non-zero exit means the base has fallen behind and a write path is about to 422:

- `airtable_required_choices.py check` — does the base offer every select choice the code writes?
- `airtable_schema_cutover.py verify <snapshot>` — are the retired fields gone and the record count
  unchanged? `prepare` writes the snapshot, backup and deletion checklist first; it is GET-only.

## Conventions
- Config/secrets via `.env` (`python-dotenv`); env vars include `GOOGLE_PLACES_API_KEY`,
  `AIRTABLE_PAT` (with `AIRTABLE_API_KEY` as a fallback in some scripts), `AIRTABLE_BASE_ID`,
  `AIRTABLE_PROSPECTS_TABLE`, `AIRTABLE_SALES_ACTIVITIES_TABLE` (default `Sales Activities`),
  `AIRTABLE_DEALS_TABLE` (default `Deals`), `AIRTABLE_RECEIPTS_PATH`, `ANTHROPIC_API_KEY`,
  `LPM_SETTER_ROSTER` (ordered comma-separated active setters; assignment is stable for new leads
  and never overwrites an existing assignee),
  `LLM_SCORING_MODEL`, the `MAX_LLM_*` caps, the Places caps (`MAX_SEARCH_CALLS_PER_RUN` 3,
  `MAX_DETAILS_CALLS_PER_RUN` 50, `MAX_TOTAL_PLACES_PER_RUN` 50, `MAX_WEBSITES_PER_RUN` 40),
  `ENRICH_WORKERS`, `BUYER_INTENT_ADS_CHECK` (off), `FOLLOW_UP_CADENCE_DAYS` (4), and
  `REVIEW_SNAPSHOT_DB`.
  **Compliance requires `SUPPRESSION_SOURCE` (or `LPM_OUTCOMES_JSONL`) to be set** or search,
  enrichment and scoring all fail closed with `[ERROR]` and exit 1. `ALLOW_MISSING_SUPPRESSION_INDEX=1`
  bypasses it loudly; there is deliberately no silent empty-index fallback, because an empty index
  disables every opt-out.
- HTTP via `requests` (see `places_search.request_json`). No new heavy deps without reason.
- Tests are stdlib `unittest` in `tests/`. Run `.venv/bin/python -m unittest discover -s tests`
  (bare `python -m unittest` discovers 0 tests here, and the system interpreter has none of the
  deps — a dozen modules fail to import; esp. cover `tests.test_icp_lanes` +
  `tests.test_lead_machine_contracts`) after changes to scoring/ICP/enrichment.
- Per-run caps follow the `getenv_int(...)` + `[STOP] Hit MAX_*` pattern in `places_search.py`.

## Skills
Per the loop build-order (reliable manual run → **make it a skill** → wrap in a loop → schedule), this
repo wants project skills in `.claude/skills/<name>/SKILL.md`. Candidates + rationale →
**[`docs/skills.md`](docs/skills.md)**.
