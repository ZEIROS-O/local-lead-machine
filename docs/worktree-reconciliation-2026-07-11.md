# Worktree Reconciliation — 2026-07-11

Four worktrees of this repo existed on this machine. This report records what each contained,
why (marked **confirmed** / **inferred** / **unknown**), and its final disposition. Patch
inventories of every dirty checkout were saved to
`~/worktree-reconciliation-backups-2026-07-11/` **before any refs changed**.

Evidence basis: `git worktree list`, merge-bases, `git rev-list --count`, per-worktree
`git status`, full test runs (`.venv/bin/python -m unittest discover -s tests` — **256 pass**
pre-reconciliation on the Phase A tip, **381 pass** on final merged main). Post-merge runtime
checks all passed without paid APIs or Airtable writes: hermes `status` and `score-dry`
(projected $0.00, no Anthropic call), read-only `queue-status`, `morning-queue` producing a
valid 30-item queue file in `run_state/`, `mark-sent --dry-run`, and a manual-inserter dry-run
for the water-restoration lane (registry-resolved vertical, Weak→Poor mapping, refusal on
unknown verticals).

## Dispositions

| Worktree | Branch | State found | Disposition |
|---|---|---|---|
| `local-lead-machine` (this one) | `phase-a-g1-api-llm` | 35 commits ahead of main, 0 behind; dirty (see below) | Land remaining work; **fast-forward main**; becomes the only checkout |
| `local-lead-machine-airtable` | `airtable-csv-sync` | Clean; tip 59aa7a0 (2026-06-02) | Already merged to main via PR #4 (`a9af536`); **remove, nothing to recover** |
| `local-lead-machine-hermes` | `hermes-command-layer` | 8 unique commits (2026-06-24 → 07-10); one untracked file | **Merge --no-ff into main**; discard untracked config |
| `local-lead-machine-manual-leads` | `manual-lead-inserter` | Branch is a pure ancestor of Phase A (0 ahead / 10 behind, tip 851fe29, 2026-06-25); uncommitted feature | **Port feature files only**; never merge the stale base |

## Per-worktree detail

### phase-a-g1-api-llm (authoritative line)

- 35 commits, 2026-06-20 (`6f7ea58`) → 2026-07-09 (`ad3ccc9`): G1–G4 automation, vertical/ICP
  work, buyer intent, pagination, scoring batches 1–8.
- Dirty state found: one-line `lead_machine/lead_card.py` addition (Review Demand Band —
  **confirmed** as the missing piece of the Batch 6–8 line, committed `e6511df`); untracked
  send-queue scripts, `morning_queue.json`, `run_state/`, `manual_leads/`, and three docs.
- **Confirmed (operator):** the send-queue runtime files stayed uncommitted during the staged
  rollout of the morning-queue loop. They are hard dependencies of the Hermes queue verbs
  (`hermes_queue.py` invokes `query_ready_to_send.py` / `mark_sent.py` by filename and reads
  `morning_queue.json`) and are now committed with offline tests (`3048579`, plus `09c2490`
  from a parallel session — see anomalies).
- `run_state/` and `manual_leads/` hold lead contents; now gitignored (`9495978`), never
  committed.

### airtable-csv-sync

- Created 2026-06-02; single-purpose CSV→Airtable sync. Merged to main 2026-06-02 (PR #4).
- Checkout verified clean on 2026-07-11. **Confirmed:** no unmerged or uncommitted work.
- **Inferred:** worktree simply never got cleaned up after the PR merged.

### hermes-command-layer

- Branched from `5c02010` (2026-06-24); 8 unique commits ending `fe06bb6` (2026-07-10):
  deterministic command layer, Telegram routing, autopilot checkpoints, and the four morning
  send-queue verbs (`queue-status`, `queue-topup`, `morning-queue`, `mark-sent`).
- **Confirmed (operator):** isolated deliberately for staged safety — the operator/Telegram
  surface was kept off the fast-moving Phase A line until both were trusted.
- 11 commits behind Phase A at reconciliation; shared-pipeline conflicts resolved **in favor of
  current Phase A behavior** (vertical-derived review bounds, centralized scoring rules, current
  Airtable payload/drop diagnostics, current prompt wording, and their tests). All Hermes-only
  command/state/executor/queue/doc/test code preserved.
- Untracked `search_configs/epoxy_cleveland_medium_002.json`: **confirmed stale** — uses the
  obsolete five-review floor (`min_reviews: 5`; current epoxy configs use 10) and predates
  pagination; config numbering on the main line already advanced to 004/005. Discarded (copy in
  backups).

### manual-lead-inserter

- Worktree base 851fe29 (2026-06-25), never advanced; all value was uncommitted:
  `manual_lead_insert.py` (wrapper), `lead_machine/manual_lead_insert.py`,
  `tests/test_manual_lead_insert.py`, plus edits to three tracked files.
- **Confirmed (operator):** the feature stayed uncommitted under test/approval constraints.
- Ported: wrapper + module + tests only, adapted to the shared vertical registry
  (`lead_machine/verticals.py`) so newer lanes (garage doors, water restoration) work without
  hardcoded lists. Preserved: dry-run default, explicit `--live`, opt-in LLM extraction,
  deterministic Place Keys, multi-business refusal, fail-before-network validation.
- **Not ported (confirmed superseded):** the `make_llm_batch.py` hunk (wording now centralized
  in `lead_machine/scoring_rules.py:6-7`) and the `upload_airtable.py` domain-dedup hunk (now in
  `upload_airtable.py` via `lead_machine.schema.normalize_domain` with a `GENERIC_DOMAINS`
  guard — strictly better). The `lead_machine/airtable_sync.py` hunk (Current Website Quality as
  a global typed field) was **rejected by decision**: its normalizer returns `None` for unknown
  labels and would silently drop pipeline free-text values; instead the field stays text-schema
  and Weak→Poor normalization lives inside manual-input handling.

## Anomalies and known debt

- **Parallel-session commit:** `09c2490` ("Add send-queue scripts and pipeline docs",
  2026-07-11 14:44) landed on `phase-a-g1-api-llm` from another session while this
  reconciliation was in progress. Contents matched the working-tree files already inventoried
  and tested; reconciliation commits sit on top of it. **Confirmed** identical to backed-up
  copies.
- **Queue-script duplication (2026-07-09 audit):** `query_ready_to_send.py` and `mark_sent.py`
  each build their own Airtable client and are driven via subprocess from `hermes_queue.py`.
  Committed as-is deliberately; known debt, not target architecture.
- **Machine-absolute path:** `morning_queue.json` points `outcomes_jsonl` at
  `~/operator-agent/STATE/outcomes.jsonl`. Accepted for a single-operator repo; an env-var
  override (`LPM_OUTCOMES_JSONL`) takes precedence where the config is consumed.
- **Pinned test command:** bare `python -m unittest` from repo root discovers **zero tests and
  reports OK**. The verification command is `.venv/bin/python -m unittest discover -s tests`.
- **No pushes:** after fast-forward, merge, and port, local `main` is 51 commits ahead of
  `origin/main` (`3c4ab84`, verified ancestor — origin had not moved). Remote branches
  untouched; pushing requires separate approval.

## Unknowns

- Why `airtable-csv-sync`'s worktree survived five weeks past its merge — no record; inferred
  oversight, no impact.
- Whether any external automation still referenced the Hermes worktree path beyond the
  operator-agent skill files that were retargeted — a repo-wide and skill-dir grep found none
  outside backups, but out-of-band references (e.g. shell history, private notes) can't be
  ruled out.
