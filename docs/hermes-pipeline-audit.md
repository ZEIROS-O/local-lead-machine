# Hermes/Telegram-safe Pipeline Audit

**Date:** 2026-06-22
**Branch audited:** phase-a-g1-api-llm
**Scope:** read-only audit of the Hermes/Telegram lead pipeline (`/run detailing cleveland medium`) against the desired end state. No files modified.

**Verification basis:** read of all entrypoints + `lead_machine/` modules; `git status --short` (only WIP edits); `python -m unittest discover tests` → **186 passed, OK**; `python -m py_compile` of the four entrypoints → **OK**; `git diff --check` → clean. No paid LLM, no Airtable live, no search run.

> Note: suite is now **186 tests** (an earlier handoff note said 171).

---

## Desired end state (reference)
Text from phone: `/run detailing cleveland medium` → safely: (1) resolve niche/location/config, (2) create/reuse safe batch name, (3) search Google + Places (+ exa later), (4) dedupe + enrich, (5) estimate LLM cost without paid call, (6) refuse if projected > max, (7) confirm before paid scoring, (8) resume from existing `*_api.csv` rows, (9) avoid stale scores-dir bugs, (10) score with API LLM only when confirmed, (11) refresh packet tiers without LLM, (12) generate packets, (13) sync Sendable to Airtable only when explicitly requested, (14) optionally sync Manual Review separately, never as outreach-ready, (15) clean Hermes-readable status + summary.

---

## 1. Working now (verified)
- **Mode/flow gating** (`run_agent_pipeline.py:62-65, 644-726`): search → score-dry → paid → packets → airtable partitioned by mode sets. `score-dry-run` never reaches paid branch (`:665-666`).
- **Mandatory dry-run before paid** (`:653-687`): `estimate_llm_cost()` first, parse `projected_usd`, then spend. (#5–#7)
- **Hard ceiling non-overridable** (`:669-673`): `projected > --max-llm-usd` refuses even with `--yes`. Confirm threshold `min(0.50, max_llm_usd)` (`:674-679`).
- **Dry-run = no Anthropic call** (`score_leads.py:3042-3044`). Covered by `test_dry_run_llm_cost_does_not_call_llm`.
- **Scored-CSV preference** `_api_tiered` → `_api` → plain (`run_agent_pipeline.py:58, 249-255`). Covered.
- **Score modes default to `scored_<batch>_api.csv`** (`:277-282`).
- **Packets refuse 0-row scored CSV** unless `--allow-empty-scored` (`:701-705`). Covered.
- **Stale scores-dir guard** (`score_leads.py:3051-3060`) + `--fresh-scores-dir` clears JSON/`_api_bundles` (`:3048-3050`, `clear_scores_dir` `:2623`); paid runs always pass it (`:391`). (#9)
- **Lockfile** with dead-PID reaping (`:97-130`). Covered.
- **Airtable defaults safe**: `--airtable` default `skip` (`:606-610`); only called on `dry-run`/`live` (`:717-726`); default sendable-only (`:716`). (#13) Covered ×2.
- **.env not fighting `--max-llm-usd`**: wrapper sets `os.environ["MAX_LLM_USD_PER_RUN"]` (`:682`); child `load_dotenv()` does not override existing env (python-dotenv `override=False`), so wrapper value wins.
- **Buyer-intent ads HTTP gated OFF during scoring**: `ensure_buyer_intent(record, ads_check=False)` default (`buyer_signals.py:92`); live `requests.get` to Google Ads Transparency / Facebook (`:321, :332`) only fire with `ads_check=True` (enrichment), NOT scoring/refresh/dry-run.
- **CSV field-limit raised** in `make_packets.py:130`, `refresh_packet_tiers.py:24`, `run_agent_pipeline.py:470`.

---

## 2. NOT working / will fail

### P0 — `score_leads.py` resume crashes on large CSV fields (KNOWN BUG, STILL UNFIXED)
`read_csv_rows` (`score_leads.py:1401-1405`) does NOT call `csv.field_size_limit`. The scored CSV's `Raw Evidence` column holds full enriched-record JSON, routinely > 128 KB default field limit. Resume reads it at `:3008` and `:3047` → `_csv.Error: field larger than field limit` before scoring. Breaks #8. `grep -c field_size_limit score_leads.py` → **0**.

### P0/P1 — `upload_airtable.py` crashes on real scored CSVs (same cause)
`read_csv_records` (`:265-280`) reads scored/tiered CSV (with `Raw Evidence`) with no field-limit raise. `grep -c field_size_limit upload_airtable.py` → **0**. Both `--dry-run` and `--live` crash on large evidence. Breaks #13/#14 in practice.

### P1 — `--include-manual-review-airtable` over-syncs everything
When `sendable_only=False`, `read_csv_records` (`:274-279`) appends EVERY row — including `Watchlist`/`No Packet` — no tier filter. Violates #14 (would push non-prospects). Also wrapper `count_airtable_rows` (`run_agent_pipeline.py:495-504`) counts only Sendable+Manual → printed "Airtable synced" UNDERCOUNTS actual writes (misleading status).

### P1 — captured dry-run failures lose child stderr
`run()` (`run_agent_pipeline.py:79-91`) with `capture=True` prints stdout, drops stderr; on failure raises `PipelineRefusal("Command failed …")` with no child output. Only `capture=True` caller is the dry-run estimate (`:460`). Known issue "print child stdout/stderr on failure" only PARTIALLY addressed (non-captured stages inherit terminal, visible).

---

## 3. Unsafe for Hermes/Telegram
- P0 field-limit crashes turn unattended `/run` into hard failure at resume + airtable steps.
- P1 lost stderr → dry-run failure unexplained.
- Status lines otherwise clean/greppable (`[PIPELINE]/[COST]/[SCORING]/[PACKETS]/[AIRTABLE]/[DONE]/[REFUSE]`) (#15) — but Airtable count line can be wrong.

## 4. Unsafe for Airtable
- Over-sync of Watchlist/No-Packet in manual-review mode (P1).
- Crash on large `Raw Evidence` (P0/P1), even `--dry-run`.
- Mitigating: live gated behind `--airtable live` + requires `AIRTABLE_BASE_ID/PROSPECTS_TABLE/PAT` (`:432-437`), validates fields pre-write (`:445-447`), upserts by Place Key → phone (`:245-253`). Default skip safe.

## 5. Can accidentally spend money
- LOW risk. Triple gate sound (hard ceiling, confirm+`--yes`, child-side `MAX_LLM_USD_PER_RUN` re-check `score_leads.py:3063-3074`). Paid unreachable without prior dry-run. Watch: `--limit` default 25, batch 5 — cost scales with `--limit` (knob, not hole).

## 6. Can accidentally duplicate/rescore
- Resume dedupe robust (`stable_lead_keys_from_*` `:1428-1515`: place_key/place_id/maps_url/name+phone). But resume read crashes (P0) → pushes operator to fresh full rescore (cost + dup risk) as workaround.
- Batch forking (`resolve_batch:213-231`) prevents clobber by forking `_002`. Good.

---

## 7. Safest manual workflow (until P0 fixed)
```bash
source .venv/bin/activate
# 1. Search + enrich (no LLM)
python run_agent_pipeline.py --vertical detailing --market cleveland --size medium --mode search-only
# 2. Dry-run first (no paid call)
python run_agent_pipeline.py --vertical detailing --market cleveland --size medium --mode score-dry-run
# 3. Paid scoring — FRESH batch only (avoids resume read crash), tight cap
python run_agent_pipeline.py --vertical detailing --market cleveland --size medium \
    --mode score-confirmed --max-llm-usd 0.50 --yes --overwrite
# 4. Packets (no LLM)
python run_agent_pipeline.py --vertical detailing --market cleveland --size medium --mode packets-only
# 5. Airtable dry-run, sendable-only (default). Do NOT use --include-manual-review-airtable yet.
python run_agent_pipeline.py ... --mode packets-only --airtable dry-run
```
Avoid: resuming a partial batch; any `--airtable` on a batch whose scored CSV has large `Raw Evidence` (crashes until P0 fixed).

---

## 8. Files/functions needing changes

| Priority | File:func | Change |
|---|---|---|
| **P0** | `score_leads.py` add `raise_csv_limit()`, call in `read_csv_rows` (`:1401`) + `main()` start | Stop resume crash; mirror `make_packets.py:130-134` |
| **P0/P1** | `upload_airtable.py` raise `csv.field_size_limit` before `read_csv_records` (`:265`) | Stop Airtable crash |
| **P1** | `upload_airtable.py:read_csv_records` (`:274-279`): when `only_sendable` False, filter to `Packet Tier == "Manual Review Packet"` + tag distinguishing Status | Fix #14 over-sync |
| **P1** | `run_agent_pipeline.py:run` (`:79-91`): include `result.stderr`/stdout in failure message | Fix child-output surfacing |
| **P1** | `run_agent_pipeline.py:count_airtable_rows` (`:495-504`): align with actual upload filter | Status truthfulness |
| **P2** | `run_agent_pipeline.py`: set `MAX_LLM_USD_PER_RUN` env before dry-run (`:657`) | Defense in depth |

---

## 9. Test gaps

**Missing (no coverage):**
- The P0 field-limit crash itself — no test reads a scored CSV with `Raw Evidence` > 128 KB. `test_run_agent_pipeline.py` mocks `rap.run` (`:64-72`), so `score_leads`/`upload_airtable` never run with real CSV I/O.
- `--include-manual-review-airtable` filtering — no test asserts which rows actually upload when `sendable_only=False`. `test_airtable_only_sendable_excludes_non_sendable_csv_rows` (contracts `:519`) covers only the sendable path.
- Subprocess-failure surfacing — no test asserts child stderr/stdout appears in `[REFUSE]`.

**Weak / pass-but-miss-real-failure:**
- `test_api_llm_resume_*` (contracts `:2292, :2372, :2439`) use tiny records → `Raw Evidence` small → the >128 KB failure never exercised. False confidence in #8.
- `test_run_agent_pipeline.py::test_score_dry_run_resumes_from_api_csv_*` writes a 4-column CSV (`_write_scored:46-62`), no `Raw Evidence`. Same blind spot.
- Wrapper tests assert command shape but mock execution → can't catch real-data crashes.

---

## 10. Proposed implementation plan (NOT yet implemented)

**Group 1 — P0 CSV field-limit** (smallest, highest-impact)
1. Shared `raise_csv_limit()` invoked in `score_leads.read_csv_rows` + `main` start, and `upload_airtable.read_csv_records` + `main` start.
2. Regression test: scored CSV with >128 KB `Raw Evidence`; assert api-llm resume + `upload_airtable --dry-run` succeed. `python -m unittest`.

**Group 2 — P1 Airtable manual-review correctness**
3. Gate non-sendable path to `Packet Tier == "Manual Review Packet"`; stamp `Manual Review` Status.
4. Align `count_airtable_rows`.
5. Tests: manual-review uploads only Manual Review tier; Watchlist/No-Packet excluded; status stamped.

**Group 3 — P1 failure surfacing**
6. `run()` includes child stderr/stdout; capture stderr on captured path.
7. Test: mocked child rc=1 with stderr appears in `[REFUSE]`.

**Group 4 — P2 polish**
8. Set `MAX_LLM_USD_PER_RUN` before dry-run; status-line tidy.

Each group: small, independently-revertible, verified with `python -m unittest` (per Phase-A working-group convention). Do Group 1 first — it currently breaks real `score-confirmed` and `--airtable` runs.
