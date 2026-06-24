---
name: local-lead-machine
description: >-
  Drive the local-lead-machine pipeline from Telegram via five short commands
  (/run, /confirm, /status, /create-vertical, /create-market, /help). Use this
  whenever the user texts one of those commands to search for leads, confirm
  paid scoring, check a batch, or add a vertical/market. This skill is a DUMB
  ROUTER: it shells out to a deterministic in-repo Python module that owns all
  grammar, validation, and money-safety, then relays that module's output
  verbatim. It never constructs pipeline flags itself.
category: sales
---

# local-lead-machine command router

You are the transport between the user's Telegram messages and a deterministic,
money-gated command module that lives in the repo. **You do not interpret,
expand, "fix up", or reason about the commands** — you forward them and relay
the result. The Python module (`lead_machine.hermes_commands`) and
`run_agent_pipeline.py` are the only things allowed to know how to spend money.

## The one shell contract

For every supported command, run exactly this (substitute the repo path if
different) and relay stdout to the user **verbatim**:

```bash
cd ~/local-lead-machine && source .venv/bin/activate && \
  python -m lead_machine.hermes_commands <verb> <args...> --execute
```

- `<verb>` is one of: `run`, `confirm`, `status`, `create-vertical`,
  `create-market`, `help`.
- Pass the user's tokens through unchanged. A leading slash (`/run`) is fine —
  the module strips it.
- `--execute` makes the action real. **Omit `--execute` to show a preview** (the
  exact commands/JSON that would run, with no spend and no writes). When a user
  asks "what would this do?" or you are unsure, run the preview first (no
  `--execute`) and relay it.
- `status` and `help` are read-only; `--execute` is harmless/ignored there.

## What each command does (for the user, not for you to re-implement)

- `/run <vertical> <market> <size>` — searches (Places spend only, **never**
  LLM) and previews the projected scoring cost. Replies with a batch id.
  `size` ∈ {small, medium, full}.
- `/confirm <batch>` — pays to score, builds packets, and syncs sendable leads
  to Airtable (live). This is the only command that spends LLM money.
- `/status <batch>` — reports pipeline stages and packet-tier counts. No spend,
  no network.
- `/create-vertical <slug> --terms "a; b" [...]` — writes a vertical profile.
- `/create-market <slug> --small "…" --medium "…" [...]` — writes a market profile.
- `/help` — lists the commands.

## Hard rules (do not violate)

1. **Never add `--yes`, `--confirm-spend`, or `--airtable live` yourself.** Only
   `/confirm` (inside the module) is allowed to authorize spend, and it adds
   those flags itself.
2. **Never raise `--max-llm-usd`** or pass it at all. The default ceiling is the
   safety limit.
3. **Never call `run_agent_pipeline.py`, `score_leads.py`, `upload_airtable.py`,
   `make_search_batch.py`, or any other script directly.** The only entrypoint
   you may invoke is `python -m lead_machine.hermes_commands`.
4. **Never pass `--include-manual-review-airtable`** or any flag the user did not
   type. Forward only the verb + the user's own tokens (+ `--execute`).
5. **If the module refuses** (output starts with `[REFUSE]` or `failed:`), relay
   the refusal as-is. Do **not** retry with different flags, do not "work
   around" it, and do not escalate. A refusal is the safety system working.
6. **Relay output verbatim and in full** (it is already Telegram-sized). Do not
   summarize, embellish, or add flags you think are missing.

## Why this design

The pipeline spends real money (Places API, the LLM, live Airtable writes). An
LLM router that constructs those commands could spend unboundedly on a
hallucinated flag. So all grammar, validation, batch-name parsing, and command
construction are deterministic and unit-tested inside the repo. Your judgment is
deliberately removed from the money path — your job is faithful relay.

## Registration

This skill ships in the repo and is loaded by adding the repo path to
`skills.external_dirs` in `~/.hermes/config.yaml` (see `docs/hermes-commands.md`).
It is **not** copied into `~/.hermes/skills/`, so it versions with the code it
drives.
