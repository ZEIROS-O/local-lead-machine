# Small-Scale Repository Hygiene Design

**Date:** 2026-07-22

## Goal

Perform a narrow, reversible hygiene pass without changing lead discovery,
enrichment, scoring, sendability, packet generation, Airtable behavior, or
runtime automation.

## Tracked Changes

1. Correct `CLAUDE.md` so its verification command matches the repository's
   required test discovery command: `python -m unittest discover -s tests`.
2. Refresh `docs/README.md` to index the current roadmap and operator-facing
   documentation already present in `docs/`. Do not rewrite those documents or
   change their status.

No Python, shell, frontend, configuration, scoring-profile, search-profile, or
pipeline behavior will change.

## Local Generated-Artifact Cleanup

Remove only ignored artifacts that are safe to regenerate:

- `.pytest_cache/`
- Python `__pycache__/` directories and compiled bytecode beneath the repo
- `dashboard-ui/dist/`
- `dashboard-ui/*.tsbuildinfo`

Preserve all of the following:

- `.env` files and backups
- `.remember/` and logs
- `dashboard-ui/node_modules/`
- `output/`, `screenshots/`, packets, reports, review queues, and lead data
- `run_state/`, manual lead inputs, subscription locks, and other runtime state
- Git branches, worktrees, tracked source files, and tracked configuration

## Execution Order

1. Apply the two tracked documentation updates.
2. Run repository verification while dependencies and caches remain available.
3. Remove the approved regenerable ignored artifacts.
4. Inspect the final diff, worktree state, and remaining ignored-artifact scope.

## Verification

- Run `python -m unittest discover -s tests` using the repository environment.
- Validate local Markdown links.
- Run `git diff --check`.
- Confirm the tracked diff contains only `CLAUDE.md` and `docs/README.md`.
- Confirm protected operational artifacts remain present and untouched.
- Confirm the approved cache/build paths are absent after cleanup.

## Delivery

Leave the implementation changes uncommitted and unpushed for user review. The
design checkpoint is committed separately before implementation.
