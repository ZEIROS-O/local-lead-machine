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
  exit 2 names the missing choice and prints the UI steps. On 2026-08-03 Lou manually consolidated
  `Do Not Contact` → `Not Fit`, `Sent` → `Follow-Up`, and `Mockup Needed` → `New` in the live base;
  code's canonical vocabulary is now `New`, `Manual Review`, `Not Fit`, `Follow-Up`, `Won`, `Lost`,
  `Booked`. The remaining MVP additions (`Booked`, `Held`, and the setter roster) are code-declared
  but not verified live, so do not claim the checker currently exits 0. Background in
  [`docs/airtable_schema_required.md`](docs/airtable_schema_required.md); full record in
  [`docs/airtable_cutover_runbook_2026-07-30.md`](docs/airtable_cutover_runbook_2026-07-30.md).
- **Scoring runs through a provider chain** in `lead_machine/llm_client.py`: kimi (Moonshot API,
  `KIMI_API_KEY` / `KIMI_SCORING_MODEL`, default `kimi-k3`) → codex CLI (ChatGPT subscription) →
  claude CLI (subscription). Order via `LLM_SCORING_PROVIDER_CHAIN`; `anthropic` (Messages API,
  `ANTHROPIC_API_KEY`) remains an optional provider. Fallback happens only on provider
  unavailability — invalid output stays with the repair flow in `score_leads.py`. Every model the
  chain can resolve must have a price in `score_leads.LLM_PRICING_USD_PER_MILLION_TOKENS` or
  `resolve_projection_model()` aborts at cost estimation, before any call is made. A model the
  *provider reports having served* is different: the call is already paid for, so an unpriced
  served id never aborts — `resolve_served_model()` charges it at the priciest tier the chain
  could have reached and warns. It is charged there rather than at the run-level model because
  the run-level model is routinely a $0 subscription runner, and charging unknown paid spend at
  $0.00 is exactly how `MAX_LLM_USD_PER_RUN` stops binding.
  `[LLM PROVIDER]` in the run log says who actually served.
- **Determinism is pinned only where the model accepts it, and the default config is not pinned.**
  `llm_client` sends `temperature: 0` on the kimi and anthropic payloads, but omits it for model
  families that fix sampling server-side (`SAMPLING_FIXED_MODEL_PREFIXES`): `kimi-k3` (Moonshot
  fixes `temperature=1.0`/`top_p=0.95` and documents that they must be omitted) and
  `claude-sonnet-5` / `opus-5` / `opus-4-8` / `opus-4-7` / `fable-5` / `mythos-5` (sampling params
  removed from the Messages API — sending one is a 400). **Both current defaults are in that set**,
  so out of the box nothing is pinned; older ids (`kimi-k2`, `claude-sonnet-4-5`, `claude-sonnet-4-6`,
  `claude-opus-4-6`, `claude-haiku-4-5`) do get the pin. `LLM_DISABLE_TEMPERATURE_PIN=1` omits it
  everywhere, for a new model that rejects sampling before it is listed. The failure direction is
  deliberate: a 400 kills a batch mid-run, a missing pin only costs determinism.
- **The `codex` and `claude` subscription runners have no temperature knob at all.** They are CLI
  subprocesses driven by model + `reasoning_effort`, so no payload-level pin can reach them. Grades
  from a `subscription:*` provider are **not reproducible run-to-run** and re-scoring the same leads
  can move them across the A/B/C/Skip boundary. Do not treat a subscription-scored grade as stable;
  re-verify with `verify-batch` rather than assuming a re-run reproduces it.
  Verified rather than assumed, and cheap to re-verify: `codex exec --strict-config -c <key>=…`
  validates config keys *before* it makes any API call, so probing costs no quota. As of 2026-08-08
  `model_temperature`, `temperature`, `model_top_p`, `top_p`, `seed` and `model_seed` are each
  rejected as `unknown configuration field`; the only accepted knob is `model_reasoning_effort`,
  which the repo already wires through `resolve_runner_effort`. **There is therefore no determinism
  to reach on this path** — the §4.3 "14 of 25 grades moved with no code change" defect stays open
  for exactly the runs routed through `codex`, and that includes the Phase 3 backlog. This is a
  known, accepted limitation to decide at the Phase 3 gate, not a bug to fix in `llm_client`.
- **Scoring is the only path that sends prospect data off this machine**, and the payload is built
  in exactly one place — `build_llm_scoring_record` (`score_leads.py:2711`). It carries business
  name, **street address**, **phone**, URLs, and up to 8 raw 300-char windows of the prospect's own
  page text selected by `LLM_SCORING_SNIPPET_TERMS` — a list that includes `contact`, `review` and
  `testimonial`, so the windows routinely contain an owner's name and email or a customer's name.
  The default chain is **subscription-only, codex first** (`("codex", "claude")`) so that an
  unattended run can neither bill anything nor send that payload to a third-party API. It was
  kimi-first until 2026-08-08, which shipped all of it to Moonshot on any unmodified run. `kimi` and
  `anthropic` are still valid via `LLM_SCORING_PROVIDER_CHAIN` — **putting either in the chain is a
  deliberate act**: a paid provider after a `subscription:*` runner is the unbounded-spend hazard
  (quota exhaustion falls through and bills per batch) and reopens the PII path. Full breakdown in
  [`docs/pii-boundary.md`](docs/pii-boundary.md); read it before changing the chain or adding a
  snippet term.
- **Two Anthropic payload features are deliberately NOT wired, and both are traps.** They read like
  free wins, so the reasoning is recorded here to stop a future pass from "finishing" them.
  *`thinking: {"type": "disabled"}`* — not a universal opt-out. It is a hard 400 on `fable-5` and
  `mythos-5` at any effort, and a 400 on `opus-5` at effort `xhigh`/`max`; three of those ids are
  already in `SAMPLING_FIXED_MODEL_PREFIXES`, so an unguarded add would kill batches on the repo's
  own listed models. Worse for this workload specifically: with thinking disabled, `opus-5` is
  documented to leak `<thinking>` tags into the visible response — and `_parse_score_bundle` parses
  that response as strict JSON, so the failure mode is a batch-wide `InvalidLlmResponse`, not a
  degraded answer. Leave thinking on and use `reasoning_effort` to control spend.
  *`cache_control` prompt caching* — caching is a **prefix** match, and
  `build_api_llm_scoring_prompt` opens with `# API LLM Lead Scoring - {batch_id}`. A per-batch id at
  position 0 invalidates the whole prefix, so a breakpoint would write a fresh entry per batch and
  never read one. Enabling it means moving the stable rubric ahead of the batch id and records —
  which rewrites the prompt every lead is scored against, and therefore moves grades and the
  reproducibility corpus. That is a scoring change to schedule ahead of a backlog run, not a cleanup.
- **`buyer_intent.money_signal` is tri-state** (`yes`/`no`/`unknown`). "unknown" means there was no
  crawlable site text to read a paid stack from, which is not the same claim as "does not spend".
  Only `yes` may count as truthy in a gate — `sendability.required_visible_pain_score` drops the
  mature-band threshold from 40 to 35 on a truthy value. `demand_signal` is deliberately separate.
- **Enrichment runs concurrently** (`ENRICH_WORKERS`, default 6; 5 when screenshots or rendered
  checks are on, since each worker thread holds its own Chromium) and streams records to disk as
  they finish. The per-lead ceiling scales with the worker count — it is wall-clock, and contention
  inflates it. Both defaults are measured, and the two paths scale in opposite directions: Chromium
  renders out-of-process so browser work genuinely parallelises, while the network path's parsing is
  GIL-bound, so 6 there is a defensible middle rather than an optimum — raising it to 10 measured
  *slower*. The numbers, and what they do and don't license, are in `resolve_enrich_workers`; don't
  retune either default from first principles.
- **The browser is per worker thread; the context is per lead.** Playwright's sync API is bound to
  the thread that created it, so `thread_browser()` keeps the Chromium in a `threading.local` and
  `close_worker_browsers` hands each worker its own closing task at shutdown — the main thread
  cannot close a worker's browser. Three rules follow, all pinned by
  `tests/test_browser_pooling.py`:
  - **Every context is closed on the way out, including the early-return and exception paths.** The
    browser now outlives the lead, so a leaked context carries one lead's cookies, storage and
    service workers into the next lead on that thread.
  - **The mobile pass gets its own context, not a second page.** `is_mobile` is a context option;
    sharing a context renders the mobile shot as a narrow desktop and moves `mobile_visual_score`.
  - **A browser that is disconnected — or that can no longer open a context — is discarded, not
    reused.** One exhausted Chromium would otherwise misreport every remaining lead on that
    thread as a *site* failure ("Unknown", no mobile screenshot), which costs far more than the
    launch it saved.
- **The render settle has a ceiling *and*, where it matters, a floor.** `settle_rendered_page`
  waits on `wait_for_load_state("networkidle", …)` in place of the unconditional 1.5s sleep each
  rendered page used to pay. `RENDER_SETTLE_TIMEOUT_MS` is deliberately that same 1.5s, and a
  settle that times out adds nothing on top — so a site with a chat or ad widget that never idles
  spends exactly what it used to, and a quiet page returns in a fraction of it. (Measured on 50
  leads: a 2.5s ceiling plus a fallback made the never-idle tail up to 6s/lead *slower* than the
  sleep it replaced.) But `detect_rendered_form` passes `RENDER_FORM_SETTLE_FLOOR_MS` and keeps
  the full window: it reads evidence out of the DOM rather than screenshotting it, and it only
  runs when the static crawl found no form — so a shorter look reports "no form" about a site
  whose form is injected late, and that absence is worth +20/+18/+12 of pain. Cutting a wait
  short is never neutral on the path where something is *read*.
- **A lead's crawl is budgeted, and a skipped fetch can make a lead *more* sendable.** This is the
  counterintuitive one. `score_leads.score_online_weakness` sells the pain, so **absence adds
  points**: `no_form_detected` is +8 when corroborated, a missing services page is +5 *and* is
  itself a corroborator — and a higher `visible_pain_score` clears
  `sendability.VISIBLE_PAIN_BLOCKER`. So "we crawled less" and "this site is broken" are the same
  input downstream. (The visual grades in `lead_machine/evidence.py` do move the other way; do not
  generalise from them.) Every fetch the crawl declines has to be one that could not have changed
  the answer:
  - `scrape_site` runs two passes — the pages the homepage links, then the
    `CONTACT_ESTIMATE_PATHS`-style guesses. A group's guesses are skipped only once it has
    **answered what the group is for**. For the contact group that bar is `has_terminal_form`:
    an estimate form graded *Strong*, the same thing `evidence.strong_estimate_form` stops
    charging pain for. Anything weaker still scores (+20 no estimate form, +18 weak, +12 basic),
    so a weaker find does not prove a better page isn't one URL away. **Do not use
    `forms_found`** — `detect_form_signals` grades any page with a `<button>`, `<input>` or
    `<iframe>` as Weak rather than None, so a hamburger menu satisfies it and a soft 404 ends the
    search. (A HubSpot template emitting an unrendered `/f/contact/$hs_path` matches the contact
    keywords and 404s; the guess it would have suppressed, `/contact`, was the real form.)
  - Both passes take pages round-robin across keyword groups, and pass 1 reserves a slot per group
    for pass 2 — otherwise a nav of per-city contact links drains the budget and `weak_services`
    fires about a page that was never requested.
  - `MAX_CANDIDATE_PAGE_FETCHES` (env-tunable, floored at the ladder length) is a backstop against
    the pathological shape, not a trim. When it bites, the crawl sets
    `candidate_page_budget_exhausted`, and if the contact group never got its terminal answer the
    form fields are written **`Unknown` rather than `No`** — a search we cut short did not
    establish absence, and `Unknown` is the one value the pain profile ignores.
    `tests/test_enrichment_fetch_budget.py` pins the property directly: truncation must never
    raise `visible_pain_score` above what an unlimited crawl produces.
  - `fetch_url` retries 5xx but not 404 — a 404 is a deterministic answer.
  - **A lead's candidate pages can be fetched concurrently (`PAGE_FETCH_WORKERS`, default 1 =
    serial).** The fetches overlap; the parsing and the slot naming stay serial and in
    *submission* order, because `_candidate_page_name` numbers `estimate`, `estimate_2`, … by
    insertion into `pages` and naming off completion order would reshuffle every lead with two
    contactish pages. It ships off on measurement, not on doubt: it is worth 1.42x on an
    otherwise idle box, but both shipped paths already crawl 5–6 *leads* at once, which fills the
    network waits it would have filled — measured flat on the browser path for ~320 MB more peak
    PSS. Turn it up only for a run whose lead-level concurrency cannot saturate the box (a
    topup, a handful of leads, one lead being debugged). Widths 1–8 produce identical pages and
    graded fields; `resolve_page_fetch_workers` carries the numbers.
  - The pooled `requests.Session` is per worker thread but handed back per lead
    (`reset_fetch_session`, called from `enrich_row`): reuse is worth having across one site's
    pages and worth nothing across leads, while the cookie jar and open sockets should end with
    the crawl that opened them.
- **Place Details are fetched concurrently too** (`PLACES_DETAILS_WORKERS`, default 6, clamped to 8),
  replacing the old serial loop and its per-call 0.15s pacing sleep. Two things are load-bearing and
  must not be "simplified" away: **`--max-details` is reserved atomically** through
  `make_search_batch.DetailsBudget` (every Place Details call is billed, and the old
  `if count >= cap: ... count += 1` is a check-then-act that over-fetches the moment a yield point
  lands between the two), and **rows are collected in submit order, not completion order** — raw rows
  go straight to `data/raw/<batch>.csv` and `dedupe_search_rows` is first-wins, so completion order
  would silently change which duplicate survives. Workers only make the network call; every shared
  structure is mutated on the calling thread, the same contract as `enrich_rows_concurrently`.
- **Review velocity needs two observations ≥14 days apart** in `run_state/review_velocity.sqlite3`
  (`REVIEW_SNAPSHOT_DB`). `enrich_row` banks one per lead per run; until a batch has been run twice,
  `velocity_band` is legitimately `unknown`.

## The write-back half (post-send CRM)

The pipeline ends at Airtable; three small scripts move a lead through the funnel afterwards, and
every one of them logs something a human already did. **None of them send anything.**

- `query_ready_to_send.py` — read-only queue of `Status=New` leads passing the readiness contract.
- `mark_sent.py` — records the completed contact, moves `Status=New` → `Follow-Up`, writes
  `Last Contacted` and `Outreach Channel`, and arms `Next Follow-Up` one cadence step out.
- `mark_outcome.py` — everything after that, starting from `Follow-Up`: `replied` /
  `meeting_booked` stay `Follow-Up` (and re-arm the
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

## Where things live

The checkout holds code, search configs, and vertical/market profiles. Local operational state is
gitignored: `output/`, `screenshots/`, `runs/`, `run_state/`, `data/`, and `manual_leads/`. Move
completed packet and review-queue runs into `runs/`; keep the current live run at the repo root.
Bulk history lives outside the checkout at `~/local-lead-machine-archive/`, and retired secret
backups live at `~/.config/local-lead-machine/backups/`. The active `.env` remains in the checkout
and must be owner-readable only.

## Conventions
- Config/secrets via `.env` (`python-dotenv`); env vars include `GOOGLE_PLACES_API_KEY`,
  `AIRTABLE_PAT` (with `AIRTABLE_API_KEY` as a fallback in some scripts), `AIRTABLE_BASE_ID`,
  `AIRTABLE_PROSPECTS_TABLE`, `AIRTABLE_SALES_ACTIVITIES_TABLE` (default `Sales Activities`),
  `AIRTABLE_DEALS_TABLE` (default `Deals`), `AIRTABLE_RECEIPTS_PATH`, `ANTHROPIC_API_KEY`,
  `LPM_SETTER_ROSTER` (ordered comma-separated active setters; assignment is stable for new leads
  and never overwrites an existing assignee),
  `LLM_SCORING_MODEL`, the `MAX_LLM_*` caps (`MAX_LLM_CALLS_PER_RUN` 60, `MAX_LLM_TOKENS_PER_RUN`
  800k, `MAX_LLM_USD_PER_RUN` $10, `LLM_SPEND_CONFIRM_USD` $3 — all resized 2026-08-05 off the
  2026-06-20 build-time values, which had become the binding throughput limit at 5 calls/run;
  **`MAX_LLM_USD_PER_RUN` is the real backstop**, the calls/tokens ceilings are blunt proxies),
  `LLM_SCORING_CONCURRENCY` (4; the safe value is provider-specific — CLI runners are bounded by
  local CPU/memory, `kimi`/`anthropic` by per-org tokens-per-minute), the Places caps
  (`MAX_SEARCH_CALLS_PER_RUN` 30, `MAX_DETAILS_CALLS_PER_RUN` 400, `MAX_TOTAL_PLACES_PER_RUN` 400 —
  the search and total-places caps are legacy-CLI only, but **`make_search_batch.py` now reads
  `MAX_DETAILS_CALLS_PER_RUN`**: `collect_raw_rows` used to build `DetailsBudget(None)`, i.e. no
  ceiling at all on billed Place Details, and the perf range removed the per-call sleep and added
  6-way concurrency, so an unattended run could bill ~6× faster with no brake. Unspecified now means
  400, never unlimited; an explicit `--max-details` still wins, and a *negative* env value restores
  unlimited. Note 400 truncates the two big `_full` configs — `cleveland_hardscaping_full_001`
  (~2,300 places) and `garage_doors_cincinnati_full_001` — to a warning plus skip-remaining, so
  raise it deliberately for those), `MAX_WEBSITES_PER_RUN` (200; this one
  **discards leads** past the ceiling rather than stopping short, so undersizing it is a silent yield
  loss), `ENRICH_WORKERS`, `PLACES_DETAILS_WORKERS` (6, max 8),
  `BUYER_INTENT_ADS_CHECK` (off), `FOLLOW_UP_CADENCE_DAYS` (4), and
  `REVIEW_SNAPSHOT_DB`.
  **Compliance requires `SUPPRESSION_SOURCE` (or `LPM_OUTCOMES_JSONL`) to be set** or search,
  enrichment, scoring **and `query_ready_to_send.py`** all fail closed with `[ERROR]` and exit 1.
  `ALLOW_MISSING_SUPPRESSION_INDEX=1` bypasses it loudly; there is deliberately no silent
  empty-index fallback, because an empty index disables every opt-out.
  The ledger is also the *only* record of an opt-out: `mark_outcome.py --event opt_out`
  (alias `unsubscribed`) appends to it via `compliance.append_suppression_event` and sets
  `Status = Not Fit`, with no source-status gate — a rep hears "take me off your list" on the
  first dial, at `Status = New`. `Do Not Contact` is deliberately not a Status; the ledger is
  where every stage already reads. Suppression matches on **contact channels**, not Place Key,
  so an opt-out on a record with no `Phone` and no `Email` records `suppression_scope:
  "record_only"`, warns, and will *not* survive re-discovery under a new record — pass
  `--phone`/`--email` to make it stick.
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
