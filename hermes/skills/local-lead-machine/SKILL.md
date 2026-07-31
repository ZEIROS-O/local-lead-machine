---
name: local-lead-machine
description: >-
  Drive the local-lead-machine pipeline from Telegram through the safe staged
  Hermes operator commands (/run, /score-dry, /score, /score-rest, /packets,
  /sync-dry, /sync-live, /status). This skill is a dumb router: it shells out
  to the deterministic in-repo Python module and relays output verbatim.
category: sales
---

# local-lead-machine command router

You are transport only. Do not interpret, expand, repair, or reconstruct lead
pipeline commands. Forward the user's tokens to the deterministic command module
and relay stdout verbatim.

## Shell Contract

Run exactly (Hermes is merged into the canonical repo; the worktree phase is over):

```bash
cd ~/local-lead-machine
source ~/local-lead-machine/.venv/bin/activate
export LOCAL_LEAD_MACHINE_ROOT=~/local-lead-machine
export LOCAL_LEAD_MACHINE_PYTHON=~/local-lead-machine/.venv/bin/python
python -m lead_machine.hermes_commands <verb> <args...> --execute
```

Example:

```bash
cd ~/local-lead-machine
source ~/local-lead-machine/.venv/bin/activate
LOCAL_LEAD_MACHINE_ROOT=~/local-lead-machine \
LOCAL_LEAD_MACHINE_PYTHON=~/local-lead-machine/.venv/bin/python \
python -m lead_machine.hermes_commands status detailing_cleveland_medium_001
```

Supported verbs:

- `run`
- `score-dry`
- `score`
- `score-rest`
- `packets`
- `sync-dry`
- `sync-live`
- `status`
- `confirm` (deprecated/refusal-only)
- `create-vertical`
- `create-market`
- `help`

Pass user tokens unchanged. A leading slash is fine; the module strips it.

## Command Semantics

- `/run <vertical> <market> <size>` searches/enriches only. It never scores and
  never touches Airtable.
- `/score-dry <batch>` estimates cost only with no Anthropic call.
- `/score <batch> --limit N --timeout MINUTES` runs paid scoring only.
- `/score-rest <batch>` chunks remaining paid scoring.
- `/packets <batch>` deterministically refreshes tiers and builds packets.
- `/sync-dry <batch>` dry-runs all scored rows and records the input hash.
- `/sync-live <batch>` is the only live Airtable command and mirrors all scored
  rows only after the latest dry-run is clean and hash-matched.
- `/status <batch>` reads local files only.
- `/confirm <batch>` refuses and tells the user to use staged commands.

## Hard Rules

1. Never call pipeline scripts directly. The only entrypoint is
   `python -m lead_machine.hermes_commands`.
2. Never add flags the user did not type, except the required trailing
   `--execute`.
3. Never add `--live` except by forwarding `/sync-live`.
4. Never retry a refusal with different flags.
5. Relay `[REFUSE]` output exactly.
6. Do not summarize command output; it is already bounded for Telegram.
