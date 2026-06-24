# Hermes / Telegram command layer

Drive the lead pipeline from your phone with five short commands, without ever
letting a non-deterministic LLM construct a money-spending command.

- **Telegram ⇄ Hermes** is the transport (the external Nous Research Hermes Agent
  at `~/.hermes`, already wired to your DM).
- **`hermes/skills/local-lead-machine/SKILL.md`** is a dumb router: it shells out
  to the deterministic module below and relays its output verbatim.
- **`lead_machine/hermes_commands.py`** is the deterministic core. It owns all
  grammar, validation, batch-name parsing, and command construction, and forwards
  to `run_agent_pipeline.py` — the single money-safety boundary. It never
  re-implements cost estimation, batch forking, or Airtable logic.

## The commands

All are invoked as `python -m lead_machine.hermes_commands <verb> … [--execute]`.
Without `--execute`, every mutating verb **previews** (prints the exact
command(s) / JSON it would run) and changes nothing. `status` and `help` are
read-only.

| Command | What it does | Spend |
| --- | --- | --- |
| `run <vertical> <market> <size>` | Search + enrich, then dry-run the scoring cost. Reports the resolved batch id. | Places only, **never LLM** |
| `confirm <batch>` | Paid scoring → packets → **live Airtable (sendable-only)**. | LLM + Airtable |
| `status <batch>` | Pipeline stages + packet-tier counts. | none |
| `create-vertical <slug> --terms "a; b" […]` | Write `vertical_profiles/<slug>.json`. | none |
| `create-market <slug> --small "…" --medium "…" […]` | Write `market_profiles/<slug>.json`. | none |
| `help` | List the commands. | none |

`size` ∈ {`small`, `medium`, `full`}. Slugs must match `^[a-z][a-z0-9_]*$`.

### Two-step money safety for `/run` → `/confirm`

`/run` deliberately uses `search-only` then `score-dry-run`, which spend Places
budget but **guarantee zero LLM spend** (there is no flag to lower the pipeline's
$0.50 confirmation threshold, so the only safe preview is a dry run). It reports
the projected scoring cost and the batch id. `/confirm` is the *only* path that
adds `--yes` and `--airtable live`, and it runs the in-pipeline hard-ceiling
check first — a projected cost above `--max-llm-usd` (default $1.00) refuses even
with `--yes`.

### Examples

```bash
# Preview (no spend, no writes):
python -m lead_machine.hermes_commands run epoxy cleveland medium
python -m lead_machine.hermes_commands confirm epoxy_cleveland_medium_001
python -m lead_machine.hermes_commands create-vertical pool_builders \
  --display "Pool Builders" --airtable "Pool Builders" \
  --terms "pool builder; inground pool contractor"
python -m lead_machine.hermes_commands create-market pittsburgh \
  --display "Pittsburgh" --state PA \
  --small "Pittsburgh PA" --medium "Pittsburgh PA; Mt Lebanon PA"

# Read-only:
python -m lead_machine.hermes_commands status epoxy_cleveland_medium_001
python -m lead_machine.hermes_commands help

# Real action (spends / writes):
python -m lead_machine.hermes_commands run epoxy cleveland medium --execute
python -m lead_machine.hermes_commands confirm epoxy_cleveland_medium_001 --execute
```

### Batch names

The layer supports only the new `<vertical>_<market>_<size>_NNN` form. Parsing is
profile-aware: it strips the trailing `_NNN` and `_<size>`, then matches the
remainder against existing `vertical_profiles/` + `market_profiles/` stems, so
multi-word slugs (`pool_builders`) work. Legacy market-first config names
(`cleveland_hardscaping_vpain_*`) are not supported.

## Wiring it into Hermes (one-line user config)

The skill ships in this repo and is loaded by pointing Hermes at it — **not** by
copying it into the external `~/.hermes/` tree. Add the repo's `hermes/skills`
path to `skills.external_dirs` in `~/.hermes/config.yaml`:

```yaml
skills:
  external_dirs:
    - /home/you/local-lead-machine/hermes/skills
```

Then restart / reload Hermes so it picks up the `local-lead-machine` skill. The
skill documents the exact shell contract and the prohibitions (never add `--yes`,
never raise the cap, never call the scripts directly, relay refusals as-is).

## Safety model (what stops a bad spend)

- **Determinism:** the LLM never builds pipeline flags; the Python module does.
- **Preview-by-default:** nothing spends or writes without `--execute`.
- **Single boundary:** `run_agent_pipeline.py` does the cost estimate, hard
  ceiling, lockfile, and Airtable filtering; this layer only forwards to it.
- **Sendable-only Airtable:** `/confirm` never passes
  `--include-manual-review-airtable`.
- **Residual risk:** a misbehaving Hermes agent could ignore the skill and call
  scripts directly. The skill states the prohibitions explicitly, but the real
  guard is that the deterministic module is the only thing that knows how to
  build correct, safe commands.

## Tests

`tests/test_hermes_commands.py` covers the whole layer offline — no subprocess,
network, LLM, or Airtable. Run:

```bash
python -m unittest tests.test_hermes_commands
python -m unittest discover tests   # full regression
```
