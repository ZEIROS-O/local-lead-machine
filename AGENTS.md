# Agent orientation — local-lead-machine

This repo finds local-service businesses with weak websites and scores them as outreach leads:
`places_search.py` → `enrich_leads.py` → `make_llm_batch` → score (`score_leads.py`) →
`sendability.py` gate → `make_packets.py` → Airtable. Core logic lives in `lead_machine/`.

## The roadmap (one machine, four nested loops)

This repo is the first instance of an automated outreach loop: **Discovery & Scoring (A)** →
**Asset Generation (B)** → **Sequencing & Reply-Routing (C)** → **Feedback (D)**. Build in dependency
order; each loop is independently revertible.

- **Phase A — Loop A (active, ~80% built):** find good leads + close the scoring loop.
  Plan of record → **[`docs/phase-a-backlog.md`](docs/phase-a-backlog.md)**.
- **Phase B — Loops B/C (not built):** asset generation (the moat) + outreach sequencing.
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

**[`docs/phase-a-backlog.md`](docs/phase-a-backlog.md)** is the current plan of record. It is split
into 4 sequenced groups (G1→G4) with tasks, "Done when", and verification. Work groups in order;
respect the blocking links; keep each group a small, independently-revertible batch.

Key in-flight facts (2026-06-20):
- **Live vertical is migrating hardscaping → car detailing** (high-ticket: ceramic coating / paint
  correction / PPF / mobile detailing — NOT basic car washes). Epoxy is a planned fast-follow, so
  per-vertical terms are being centralized in `lead_machine/verticals.py` — add verticals as **data, not code**.
- **Scoring is being automated** off the manual clipboard onto the Anthropic Messages API
  (Sonnet 4.6, `LLM_SCORING_MODEL` in `.env`, no SDK — uses `requests`).
- A **buyer-intent / money signal** is being added to enrichment + scoring + the sendability gate.

## Conventions
- Config/secrets via `.env` (`python-dotenv`); env vars include `GOOGLE_PLACES_API_KEY`, `AIRTABLE_*`,
  and (new) `ANTHROPIC_API_KEY`, `LLM_SCORING_MODEL`, `MAX_LLM_*` caps.
  **Compliance requires `SUPPRESSION_SOURCE` (or `LPM_OUTCOMES_JSONL`) to be set** or it will fail closed.
- HTTP via `requests` (see `places_search.py:170` `request_json`). No new heavy deps without reason.
- Tests are stdlib `unittest` in `tests/`. Run `python -m unittest discover -s tests`
  (bare `python -m unittest` discovers 0 tests here; esp. cover `tests.test_icp_lanes` +
  `tests.test_lead_machine_contracts`) after changes to scoring/ICP/enrichment.
- Per-run caps follow the `getenv_int(...)` + `[STOP] Hit MAX_*` pattern in `places_search.py`.

## Skills
Per the loop build-order (reliable manual run → **make it a skill** → wrap in a loop → schedule), this
repo wants project skills in `.claude/skills/<name>/SKILL.md`. Candidates + rationale →
**[`docs/skills.md`](docs/skills.md)**.
