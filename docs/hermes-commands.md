# Hermes / Telegram command layer

Hermes is a safe operator layer for the local lead machine. It routes short
commands to deterministic Python code in this repo and keeps paid scoring,
packet generation, and Airtable live sync as separate stages.

## Workflow

```text
/run -> /score-dry -> /score or /score-rest -> /packets -> /sync-dry -> /sync-live -> /status
```

`/confirm` is deprecated and refusal-only. It never scores, builds packets, or
syncs Airtable.

## Commands

All commands are invoked as:

```bash
python -m lead_machine.hermes_commands <verb> ... [--execute]
```

Without `--execute`, mutating commands preview the command they would run.

| Command | What it does | External spend/write |
| --- | --- | --- |
| `/run <vertical> <market> <size>` | Search/enrich only through `run_agent_pipeline.py --mode search-only`. | Google Places/search only |
| `/score-dry <batch> [--limit N] [--budget USD] [--batch-size N] [--max-tokens N]` | API-LLM cost estimate with `--dry-run-llm-cost`. | none |
| `/score <batch> --limit N --timeout MINUTES [...]` | Paid API-LLM scoring only, with resume and budget env caps. | Anthropic only |
| `/score-rest <batch> [...]` | Scores remaining rows in chunks and stops on no progress or budget exhaustion. | Anthropic only |
| `/packets <batch>` | Refreshes `output/scored_<batch>_api_tiered.csv` from canonical `_api.csv`, then builds `packets_<batch>`. | none |
| `/sync-dry <batch>` | Airtable dry-run for all scored rows; records POST/PATCH/SKIP counts and input hash. | Airtable read only when credentials are present |
| `/sync-live <batch>` | Live Airtable sync for all scored rows only after a fresh clean matching dry-run hash. | Airtable write |
| `/status <batch>` | Local status only. | none |

Defaults:

- score dry-run limit: `25`
- budget: `$2.00`
- LLM batch size: `3`
- LLM max tokens: `4096`
- score-rest chunk size: `9`

## Safety Model

- `/run` never scores and never touches Airtable.
- `/score-dry` includes `--dry-run-llm-cost` and no paid flags.
- `/score` requires explicit `--limit` and `--timeout`, and sets
  `MAX_LLM_USD_PER_RUN`, `MAX_LLM_CALLS_PER_RUN`, and
  `MAX_LLM_TOKENS_PER_RUN`.
- `/packets` always uses `output/scored_<batch>_api.csv` as the source and
  regenerates `output/scored_<batch>_api_tiered.csv`.
- `/sync-live` refuses unless `/sync-dry` recorded a clean total-upload hash
  and the packet/scored inputs have not changed.
- No command except `/sync-live` builds `upload_airtable.py --live`.

## Parallel batch runs

`run_agent_pipeline.py` takes a lockfile so two pipelines cannot run at once. The
default is a single global path, `output/.agent_pipeline.lock`, so back-to-back
batches serialise even when they have nothing to do with each other. That is a
conservative default, not a structural limit: **`--lock-file` already accepts a
distinct path per batch, and distinct batches can safely run in parallel.**

```bash
python run_agent_pipeline.py --batch garage_doors_cleveland_medium_001 \
  --vertical garage_doors --market cleveland --size medium --mode search-only \
  --lock-file output/.agent_pipeline.garage_doors_cleveland_medium_001.lock &

python run_agent_pipeline.py --batch epoxy_toledo_medium_001 \
  --vertical epoxy --market toledo --size medium --mode search-only \
  --lock-file output/.agent_pipeline.epoxy_toledo_medium_001.lock &
```

**Why it is safe.** Every file the wrapper and its stages write is derived from
the batch name, so two distinct batches never name the same path — verified by
enumerating them for two batches: `output/enriched_<batch>.jsonl`,
`output/scored_<batch>_api.csv`, `output/scored_<batch>_api_tiered.csv`,
`output/lead_cards_<batch>.jsonl`, `output/llm_usage_<batch>.json`,
`output/agent_run_summary_<batch>.json`, `packets_<batch>/`,
`review_queue_<batch>/scores/`, `search_configs/<batch>.json`,
`data/raw/<batch>.csv`, `data/deduped/<batch>.csv`,
`reports/search_<batch>.md`, `screenshots/<batch>/`. Zero overlap. The stages do
not derive paths independently either — `run_agent_pipeline.py` passes each one
explicitly, and `make_search_batch.py` keys its own from `config.batch`.

**Pass `--batch` explicitly when running in parallel.** Without it the wrapper
derives the name from `--vertical/--market/--size` and, if outputs already
exist, forks to the next free `<base>_NNN`. That scan is a check-then-act with no
lock behind it (`_next_free_index` reads the filesystem, `resolve_batch` writes
the config afterwards), so two concurrent runs sharing a base would both see the
same free index and land on the same batch — at which point every path above
collides. Different bases are fine; the same base is not.

**Two files really are shared, and both tolerate it by construction:**

- `run_state/review_velocity.sqlite3` — SQLite, opened per call with a 10s busy
  timeout, written with `INSERT OR REPLACE` keyed by `(place_key, observed_at,
  source)`. Concurrent writers queue; they do not corrupt or overwrite each
  other's observations.
- `run_state/airtable_writes.jsonl` — opened `"a"` and flushed per receipt, so
  appends from separate processes interleave by line rather than clobber. Only
  `--airtable live` writes it.

**What the real ceiling becomes.** Once the lock stops serialising runs, the
binding constraints are external: **Google Places QPS/quota** and the **LLM
provider's rate limits**. Note that the per-run caps (`MAX_SEARCH_CALLS_PER_RUN`,
`MAX_DETAILS_CALLS_PER_RUN`, `MAX_TOTAL_PLACES_PER_RUN`, `MAX_LLM_USD_PER_RUN`,
`MAX_LLM_CALLS_PER_RUN`, `MAX_LLM_TOKENS_PER_RUN`) are *per process* and are not
coordinated across parallel runs — N parallel batches can spend up to N times a
single run's ceiling. Budget accordingly.

## Run State

Hermes writes local metadata to `run_state/<batch>.json`:

- batch identity and stage counts
- score dry-run timestamp and projected cost
- paid scoring timestamp
- packet build timestamp
- Airtable dry-run/live timestamps
- dry-run/live POST/PATCH/SKIP counts
- dry-run warnings and the fixed `all_scored_rows` upload scope
- input hash for live sync verification
- next recommended command

`run_state/` is ignored by git.

## Hermes Skill Path

Hermes is merged into the canonical repo (2026-07-11); the router shells into it directly:

```bash
cd ~/local-lead-machine && source ~/local-lead-machine/.venv/bin/activate && \
  python -m lead_machine.hermes_commands <verb> <args...> --execute
```

## Tests

Offline validation:

```bash
python -m unittest tests.test_hermes_commands tests.test_hermes_safe_operator
python -m unittest discover -s tests -p 'test*.py'
```
