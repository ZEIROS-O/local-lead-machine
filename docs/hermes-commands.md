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
| `/sync-dry <batch> [--include-manual]` | Airtable dry-run only; records POST/PATCH/SKIP counts and input hash. | Airtable read only when credentials are present |
| `/sync-live <batch> [--include-manual]` | Live Airtable sync only after a fresh clean matching dry-run hash. | Airtable write |
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
- `/sync-live` refuses unless `/sync-dry` recorded a clean hash for the same
  include-manual mode and the packet/scored inputs have not changed.
- No command except `/sync-live` builds `upload_airtable.py --live`.

## Run State

Hermes writes local metadata to `run_state/<batch>.json`:

- batch identity and stage counts
- score dry-run timestamp and projected cost
- paid scoring timestamp
- packet build timestamp
- Airtable dry-run/live timestamps
- dry-run/live POST/PATCH/SKIP counts
- dry-run warnings and include-manual mode
- input hash for live sync verification
- next recommended command

`run_state/` is ignored by git.

## Hermes Skill Path

During this worktree phase, the router must shell into the Hermes worktree:

```bash
cd ~/local-lead-machine-hermes && source ~/local-lead-machine/.venv/bin/activate && \
  python -m lead_machine.hermes_commands <verb> <args...> --execute
```

When Hermes is merged into `~/local-lead-machine`, update the path back to the
main repo.

## Tests

Offline validation:

```bash
python -m unittest tests.test_hermes_commands tests.test_hermes_safe_operator
python -m unittest discover -s tests -p 'test*.py'
```
