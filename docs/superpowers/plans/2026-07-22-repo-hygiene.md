# Small-Scale Repository Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct two repository documentation surfaces and remove only the approved regenerable local caches without changing pipeline behavior or operational artifacts.

**Architecture:** This is a documentation-and-workspace-maintenance pass. Tracked edits are confined to the root agent pointer and documentation index; verification runs before an explicit allowlisted cleanup of ignored cache/build paths.

**Tech Stack:** Markdown, Git, Bash, Python stdlib `unittest`

## Global Constraints

- Do not change Python, shell, frontend, configuration, scoring-profile, search-profile, or pipeline behavior.
- Preserve `.env` files and backups, `.remember/`, logs, `dashboard-ui/node_modules/`, lead data, outputs, screenshots, packets, reports, review queues, `run_state/`, manual lead inputs, and subscription locks.
- Remove only `.pytest_cache/`, repository Python `__pycache__/` directories and compiled bytecode, `dashboard-ui/dist/`, and `dashboard-ui/*.tsbuildinfo`.
- Leave implementation changes uncommitted and unpushed for user review.

---

### Task 1: Correct and Refresh Repository Documentation

**Files:**
- Modify: `CLAUDE.md:7`
- Modify: `docs/README.md:6-14`
- Reference: `docs/superpowers/specs/2026-07-22-repo-hygiene-design.md`

**Interfaces:**
- Consumes: Existing repository test-discovery convention and existing documentation files.
- Produces: An accurate verification command and links to current roadmap/operator documentation.

- [ ] **Step 1: Confirm the implementation worktree starts clean**

Run:

```bash
git status --short --branch
```

Expected: `main` is ahead of `origin/main` only by the committed design
checkpoint; the implementation plan itself is the sole untracked planning
artifact, with no implementation changes present.

- [ ] **Step 2: Correct the root verification command**

Apply this exact patch:

```diff
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@
-independently-revertible batch verified with `python -m unittest`.
+independently-revertible batch verified with `python -m unittest discover -s tests`.
```

- [ ] **Step 3: Add the current roadmap and operator docs to the docs index**

Append these exact rows to the existing table in `docs/README.md`:

```markdown
| [`phase-b-c-roadmap.md`](phase-b-c-roadmap.md) | Post-Gate-0 build order for asset generation, outreach sequencing, and feedback-loop work; its status note defers current Phase A truth to the plan of record. |
| [`market-machine-end-state.md`](market-machine-end-state.md) | Long-horizon market-intelligence and lead-machine product vision; it does not change Gate 0 or the plan of record. |
| [`hermes-commands.md`](hermes-commands.md) | Approval-gated Hermes/Telegram operator commands, safety boundaries, and dry-run/live execution rules. |
| [`airtable_schema_required.md`](airtable_schema_required.md) | Required Airtable field contract for CRM synchronization. |
```

- [ ] **Step 4: Validate the documentation links**

Run:

```bash
python -c 'import pathlib,re; bad=[]
for p in pathlib.Path(".").rglob("*.md"):
    s=str(p)
    if any(x in s for x in (".venv/", "node_modules/")):
        continue
    text_without_fences=re.sub(r"```.*?```", "", p.read_text(errors="replace"), flags=re.S)
    for m in re.finditer(r"\[[^]]+\]\(([^)]+)\)", text_without_fences):
        q=m.group(1).split("#", 1)[0]
        if q and "://" not in q and not q.startswith("mailto:") and not (p.parent / q).resolve().exists():
            bad.append((s, q))
assert not bad, bad'
```

Expected: exit code `0` with no output.

- [ ] **Step 5: Check the tracked patch for whitespace and scope**

Run:

```bash
git diff --check
git diff --name-only
```

Expected: `git diff --check` exits `0`; the only paths printed are `CLAUDE.md` and `docs/README.md`.

### Task 2: Verify Repository Behavior Before Cleanup

**Files:**
- Test: `tests/`
- Preserve: `.env`, `.env.backup.*`, `.remember/`, `logs/`, `dashboard-ui/node_modules/`, `output/`, `screenshots/`, `packets/`, `reports/`, `review_queue_*/`, `run_state/`, `manual_leads/`

**Interfaces:**
- Consumes: The repository virtual environment and existing stdlib unit-test suite.
- Produces: Fresh evidence that the documentation-only tracked patch does not coincide with a failing repository state.

- [ ] **Step 1: Confirm protected operational paths before cleanup**

Run:

```bash
for path in .env .remember logs dashboard-ui/node_modules output screenshots packets reports run_state manual_leads; do
    if [ -e "$path" ]; then
        stat -c '%n %F' "$path"
    fi
done
```

Expected: every currently present protected path is listed; this command does not modify any path.

- [ ] **Step 2: Run the authoritative Python test suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: the run ends with `OK` and no failures or errors.

- [ ] **Step 3: Recheck the implementation diff after tests**

Run:

```bash
git diff --check
git diff --name-only
```

Expected: the only tracked implementation paths remain `CLAUDE.md` and `docs/README.md`.

### Task 3: Remove the Approved Regenerable Artifacts

**Files:**
- Delete locally: `.pytest_cache/`
- Delete locally: repository `__pycache__/` directories and compiled bytecode
- Delete locally: `dashboard-ui/dist/`
- Delete locally: `dashboard-ui/*.tsbuildinfo`

**Interfaces:**
- Consumes: The explicit cleanup allowlist from the approved design.
- Produces: A smaller local workspace with operational artifacts preserved.

- [ ] **Step 1: Inventory only the approved cleanup targets**

Run:

```bash
find . -path './.git' -prune -o -path './.venv' -prune -o -path './dashboard-ui/node_modules' -prune -o -type d -name '__pycache__' -print
find dashboard-ui -maxdepth 1 -type f -name '*.tsbuildinfo' -print
for path in .pytest_cache dashboard-ui/dist; do
    if [ -e "$path" ]; then
        du -sh "$path"
    fi
done
```

Expected: output contains only approved Python caches, TypeScript build metadata, `.pytest_cache`, and the dashboard build directory.

- [ ] **Step 2: Remove the allowlisted artifacts**

Run from `/home/lucascoh/local-lead-machine`:

```bash
rm -rf -- ./__pycache__ ./lead_machine/__pycache__ ./lead_machine/dashboard/__pycache__ ./tests/__pycache__ ./scripts/__pycache__
rm -rf -- .pytest_cache dashboard-ui/dist
rm -f -- dashboard-ui/*.tsbuildinfo
```

- [ ] **Step 3: Verify every approved target is absent**

Run:

```bash
test ! -e .pytest_cache
test ! -e dashboard-ui/dist
test -z "$(find . -path './.git' -prune -o -path './.venv' -prune -o -path './dashboard-ui/node_modules' -prune -o -type d -name '__pycache__' -print -quit)"
test -z "$(find dashboard-ui -maxdepth 1 -type f -name '*.tsbuildinfo' -print -quit)"
```

Expected: all four commands exit `0` with no output.

- [ ] **Step 4: Confirm protected paths remain and review the final scope**

Run:

```bash
for path in .env .remember logs dashboard-ui/node_modules output screenshots packets reports run_state manual_leads; do
    if [ -e "$path" ]; then
        stat -c '%n %F' "$path"
    fi
done
git status --short --branch
git diff --check
git diff --stat
git diff --name-only
```

Expected: protected paths still exist; the branch remains ahead only by the
design commit; the implementation plan remains an untracked planning artifact;
implementation changes are uncommitted; and the tracked diff contains only
`CLAUDE.md` and `docs/README.md`.
