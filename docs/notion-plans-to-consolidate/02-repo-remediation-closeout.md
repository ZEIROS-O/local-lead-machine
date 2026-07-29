# Repo Remediation Closeout Implementation Plan

> Local snapshot copied from Notion on 2026-07-28T11:43:49.196Z.  
> Source: [Repo Remediation Closeout Implementation Plan](https://app.notion.com/p/3abc57f0654f80e897aee22bee48b4aa)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Before editing, use `superpowers:using-git-worktrees` to create an isolated worktree from `fix/repo-remediation`. Steps use checkbox (`- [ ]`) syntax for tracking.
**Goal:** Close the small set of verified gaps left after the July 25–27 repo remediation without replaying completed work, changing production behavior, or mixing in the active Airtable schema-cutover branch.
**Architecture:** The production remediation is already implemented on `fix/repo-remediation`; this is a closure plan, not a rebuild. Make one test-only change that isolates DNC entry-point tests from a developer `.env`, reconcile the Phase A documentation with implemented reality, remove two exact ignored credential backups from the original checkout, and leave a verified local handoff. Do not push, merge, call paid APIs, or write to Airtable.
**Tech Stack:** Python 3 via `.venv/bin/python`, stdlib `unittest`, `python-dotenv`, Git, Markdown, and the existing `Makefile`.
## Global Constraints
- Repository: `/home/lucascoh/local-lead-machine`.
- Read `AGENTS.md` before acting. Its maker/checker, fail-closed compliance, quality-gate, spend-cap, and Gate 0 rules remain mandatory.
- Treat commits `741884f` through `a31b2fb` as completed remediation. Do not reimplement those batches.
- Preserve follow-up commits already on `fix/repo-remediation`; this plan does not revert or redesign them.
- Do not work in the active `feat/airtable-crm-rebuild` checkout. It contains later Airtable work and an untracked `search_configs/garage_doors_cleveland_medium_002.json`; do not add, edit, move, or delete that file.
- Base the isolated closeout worktree on the current `fix/repo-remediation` tip. At plan-writing time that tip was `9257cc3`.
- At plan-writing time `origin/main` was `32c7baf`. Refresh read-only branch facts at execution time; never assume these hashes are still current.
- Do not push, open or merge a PR, merge into `main`, or rewrite branch history without explicit authorization.
- Do not run `upload_airtable.py --live`, `backfill_packet_tier.py --live`, paid Anthropic scoring, Google Places search, or live enrichment.
- Do not print, open, copy, hash, or inspect the contents of `.env` or either `.env.backup.*` file.
- Use `.venv/bin/python -m unittest discover -s tests`; bare `python -m unittest` is not valid verification in this repo.
- Production compliance remains fail-closed. The test fix must not change `lead_machine/compliance.py`, `make_search_batch.py`, `enrich_leads.py`, or `score_leads.py`.
- Keep tracked changes to `tests/test_dnc_enrichment.py`, `docs/phase-a-backlog.md`, `AGENTS.md`, and `CLAUDE.md`.
- The two credential backups are ignored and untracked; their deletion produces no Git diff.
- Make one local commit for the test fix and one local commit for documentation. Stop before any external Git operation.
---
## Current State — Do Not Rebuild
The old version of this plan described a prerequisite plus seven remediation batches. Those changes already exist:
<table header-row="true">
<tr>
<td>Completed scope</td>
<td>Commit</td>
<td>Current implementation</td>
</tr>
<tr>
<td>PR #9 prerequisite and narrow Packet Tier repair</td>
<td>`32c7baf`, `741884f`</td>
<td>PR #9 is the base; the repair tool updated only verified Place Keys. Do not rerun it live.</td>
</tr>
<tr>
<td>DNC enforcement on the production path</td>
<td>`efb6150`</td>
<td>Search, enrichment, scoring, sendability, packets, and Airtable payloads fail closed around suppression.</td>
</tr>
<tr>
<td>Honest buyer-intent signals</td>
<td>`14687fb`</td>
<td>`money_signal` is `yes`/`no`/`unknown`; `demand_signal` is separate; ad stubs report `unimplemented`.</td>
</tr>
<tr>
<td>Scoring corrections and ceiling shadow</td>
<td>`9514981`</td>
<td>No-site demotion removed; strongest leak selected; `ICP Ceiling` disagreement report added.</td>
</tr>
<tr>
<td>Data-driven vertical and market resolution</td>
<td>`cb08d13`</td>
<td>Six verticals and eleven metros resolve through profiles rather than Cleveland/trade literals.</td>
</tr>
<tr>
<td>Concurrent, durable enrichment</td>
<td>`470b387`, `c2e343b`</td>
<td>Bounded workers, streaming JSONL, SQLite locking, retained error rows, and responsive interruption.</td>
</tr>
<tr>
<td>External-service robustness</td>
<td>`c19e93d`</td>
<td>Airtable receipts/retry, transient Google retry, and model-aware LLM pricing.</td>
</tr>
<tr>
<td>Documentation and root hygiene</td>
<td>`a31b2fb`</td>
<td>Pain-score docs, AGENTS facts, archived historical run directories, and legacy Places entry-point labeling.</td>
</tr>
<tr>
<td>Post-remediation fixes already on the branch</td>
<td>`86a5077` through `9257cc3`</td>
<td>Manual-entry schema parity, post-send CRM write-back, cadence handling, and schema vocabulary corrections.</td>
</tr>
</table>
Verified at plan-writing time:
- An isolated export of `9257cc3` ran `681` tests successfully.
- The isolated export passed `make PY=/home/lucascoh/local-lead-machine/.venv/bin/python compile`.
- The active feature checkout passed `make compile`.
- The active configured checkout ran `695` tests with exactly three failures in `EntryPointTests`; all three occur because the tests clear `os.environ` and then each production `main()` reloads the real repository `.env`.
- Both ignored plaintext backups still exist:
	- `.env.backup.20260604_114651`
	- `.env.backup.20260604_115027`
- `docs/phase-a-backlog.md` still says “planned, not started,” leaves G2–G4 unchecked, names Sonnet 4.6 pricing, and describes the pre-remediation boolean money-signal design.
---
### Task 1: Isolate the closeout work from the active feature checkout
**Files:**
- Read: `AGENTS.md`
- Read: `.git`
- Preserve: `search_configs/garage_doors_cleveland_medium_002.json`
**Interfaces:**
- Consumes: branch `fix/repo-remediation` and its current tip.
- Produces: an isolated worktree on a new local branch named `fix/repo-remediation-closeout`.
- [ ] **Step 1: Capture read-only branch and worktree truth**
Run from `/home/lucascoh/local-lead-machine`:
```bash
git status --short --branch
git branch -avv
git log --graph --decorate --oneline --all -35
git merge-base --is-ancestor origin/main fix/repo-remediation
```
Expected at the recorded baseline:
```plain text
feat/airtable-crm-rebuild is the active checkout
fix/repo-remediation points to 9257cc3
origin/main points to 32c7baf
32c7baf is an ancestor of fix/repo-remediation
```
If the hashes moved, continue only when the branch topology still shows that `fix/repo-remediation` contains the remediation and the active feature checkout remains separate. Record the new hashes in the final handoff.
If `origin/main` already contains `fix/repo-remediation`, stop and report that the integration premise has changed; do not manufacture a second remediation branch.
- [ ] **Step 2: Confirm the active untracked config is outside scope**
Run:
```bash
git status --short -- search_configs/garage_doors_cleveland_medium_002.json
```
Expected:
```plain text
?? search_configs/garage_doors_cleveland_medium_002.json
```
Do not stage or copy it.
- [ ] **Step 3: Create the isolated worktree**
Invoke `superpowers:using-git-worktrees` and create a worktree on:
```plain text
branch: fix/repo-remediation-closeout
base: fix/repo-remediation
```
Do not switch branches in `/home/lucascoh/local-lead-machine`.
- [ ] **Step 4: Verify the isolated baseline**
Run inside the new worktree:
```bash
git status --short --branch
git rev-parse HEAD
.venv/bin/python -m unittest discover -s tests
make compile
```
If `.venv` is not present in the worktree, use the original checkout interpreter without copying `.env`:
```bash
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest discover -s tests
make PY=/home/lucascoh/local-lead-machine/.venv/bin/python compile
```
Expected: clean Git status, the `fix/repo-remediation` tip as `HEAD`, all tests passing in the isolated worktree, and compile exit code `0`.
---
### Task 2: Make DNC entry-point tests independent of the repository `.env`
**Files:**
- Modify: `tests/test_dnc_enrichment.py`
- Test: `tests/test_dnc_enrichment.py`
**Interfaces:**
- Consumes: module-local `load_dotenv` imports in `make_search_batch`, `enrich_leads`, and `score_leads`.
- Produces: `EntryPointTests` whose environment is fully controlled by `patch.dict(os.environ, {}, clear=True)`.
- Production behavior: unchanged.
- [ ] **Step 1: Create a safe ignored ****`.env`**** to reproduce the leak**
First confirm the isolated worktree has no `.env`:
```bash
test ! -e .env
```
Use `apply_patch` to create an ignored `.env` containing exactly:
```plain text
ALLOW_MISSING_SUPPRESSION_INDEX=1
```
Do not copy the real repository `.env`.
- [ ] **Step 2: Run the three affected tests and verify the failure**
Run:
```bash
.venv/bin/python -m unittest \
  tests.test_dnc_enrichment.EntryPointTests.test_make_search_batch_main_fails_closed_before_paying_for_search \
  tests.test_dnc_enrichment.EntryPointTests.test_enrich_leads_reports_a_missing_source_as_an_error_not_a_traceback \
  tests.test_dnc_enrichment.EntryPointTests.test_score_leads_fails_closed_without_a_suppression_source
```
If the worktree uses the original interpreter, replace `.venv/bin/python` with `/home/lucascoh/local-lead-machine/.venv/bin/python`.
Expected before the fix: three assertion failures because each `main()` returns `0` after loading `ALLOW_MISSING_SUPPRESSION_INDEX=1` from `.env`.
- [ ] **Step 3: Patch dotenv loading at the unit-test boundary**
Replace `EntryPointTests.setUp` with:
```python
def setUp(self) -> None:
    self.env = patch.dict(os.environ, {}, clear=True)
    self.env.start()
    self.addCleanup(self.env.stop)

    for target in (
        "make_search_batch.load_dotenv",
        "enrich_leads.load_dotenv",
        "score_leads.load_dotenv",
    ):
        dotenv = patch(target, return_value=False)
        dotenv.start()
        self.addCleanup(dotenv.stop)
```
Do not alter any production module. These tests are explicitly asserting behavior with no configured suppression source, so dotenv loading belongs outside their test boundary.
- [ ] **Step 4: Run the affected tests and verify they pass**
Run the same three-test command from Step 2.
Expected:
```plain text
Ran 3 tests
OK
```
- [ ] **Step 5: Prove the rest of the DNC contract still passes**
Run:
```bash
.venv/bin/python -m unittest tests.test_dnc_enrichment
```
Expected: all `test_dnc_enrichment` tests pass, including `--help`, live-index recheck, pre-token filtering, No Packet, and scoring-v3 fallback coverage.
- [ ] **Step 6: Delete the synthetic ****`.env`**** with ****`apply_patch`**
Delete only the `.env` created in Step 1. Verify:
```bash
test ! -e .env
git status --short
```
Expected: only `tests/test_dnc_enrichment.py` is modified.
- [ ] **Step 7: Run full verification**
Run:
```bash
.venv/bin/python -m unittest discover -s tests
make compile
git diff --check
```
Expected: all tests pass, compile exits `0`, and `git diff --check` prints nothing.
- [ ] **Step 8: Commit the test isolation**
Run:
```bash
git add tests/test_dnc_enrichment.py
git commit -m "test(compliance): isolate DNC entrypoints from dotenv"
```
---
### Task 3: Reconcile the Phase A plan-of-record documentation
**Files:**
- Modify: `docs/phase-a-backlog.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
**Interfaces:**
- Consumes: the implemented state described in `AGENTS.md`, current production entry points, and commits listed in this plan.
- Produces: documentation that distinguishes implemented software from remaining operating validation.
- [ ] **Step 1: Pin the stale statements before editing**
Run:
```bash
rg -n 'planned, not started|Sonnet 4\.6|source="count_only"|money_signal =|^- \[ \]|Work groups G1→G4 in order|~80% built' \
  docs/phase-a-backlog.md AGENTS.md CLAUDE.md
```
Expected: matches in all three documents, including unchecked G2–G4 work that already exists in code.
- [ ] **Step 2: Rewrite ****`docs/phase-a-backlog.md`**** as the current Phase A implementation record**
Replace the stale design backlog with a concise document containing these exact sections and facts:
```markdown
# Phase A — Discovery and scoring implementation record

> **Status (2026-07-28):** G1–G4 software implementation is present. Operating validation remains active: review velocity needs a second observation at least 14 days later, the offer still has zero closes, and Gate 0 forbids autonomous sending before approximately 3–5 closes.
>
> Completed groups are historical implementation records, not work to replay.

## Current pipeline

`make_search_batch.py` / `places_search.py` → `enrich_leads.py` → scoring (`score_leads.py`, API or subscription adapter) → `lead_machine/sendability.py` → `make_packets.py` → Airtable dry-run or explicitly approved live sync.

No script in this pipeline autonomously sends outreach.

## G1 — Automated scoring: implemented

- Anthropic Messages API through `requests`, with manual/subscription fallbacks.
- Model-aware token and dollar caps; unpriced models fail closed.
- Content-addressed subscription-scoring receipts prevent stale-score reuse.

## G2 — Data-driven verticals and markets: implemented

- Six live verticals: hardscaping, car detailing, epoxy, plumbing, water restoration, and garage doors.
- Eleven metros resolve through `market_profiles/*.json`.
- Vertical behavior resolves through `lead_machine/verticals.py`; scoring must not name Cleveland or one trade.

## G3 — Buyer intent and objective gating: implemented

- `money_signal` is tri-state: `yes`, `no`, or `unknown`.
- Only `yes` is truthy in a gate.
- `demand_signal` is separate from spend.
- Google and Meta ad checks are explicit `unimplemented` states; they make no pretend-negative claim.
- Review velocity needs two observations at least 14 days apart; until then it is `unknown`.
- `fit_ceiling` is shadow-only on the LLM path and is reported through `Grade Disagreement`.

## G4 — Capped command and summary: implemented

- `run_pipeline.py` defaults to `--mode api-llm`; manual mode retains its wait/resume behavior.
- The pipeline builds packets and runs Airtable as a dry-run by default.
- `--airtable-live` remains an explicit external write and is never implied by ordinary execution.
- Run summaries report lead counts, A/B counts, sendable counts, packets, Airtable rows, calls, tokens, dollars, caps, and cap-stop reason.

## Remaining operating validation

- Re-run target batches after at least 14 days to populate real review velocity.
- Hand-build assets and send manually while the offer remains unvalidated.
- Record outcomes through `mark_sent.py` and `mark_outcome.py`.
- Do not build autonomous sending before approximately 3–5 closes.
- Evaluate the shadow `Grade Disagreement` report before enforcing or deleting `fit_ceiling`.

## Verification contract

- `.venv/bin/python -m unittest discover -s tests`
- `make compile`
- Suppression is required unless `ALLOW_MISSING_SUPPRESSION_INDEX=1` is explicitly set for local development.
- Airtable defaults to dry-run; live writes require explicit approval.
```
Do not restore the old boolean money-signal design, single-vertical migration narrative, or Sonnet 4.6 constant pricing.
- [ ] **Step 3: Update ****`AGENTS.md`**** roadmap language**
Change the Phase A roadmap bullet to:
```markdown
- **Phase A — Loop A (implemented; operating validation active):** discovery, enrichment, capped scoring, objective sendability, packets, and Airtable dry-run are built. Review velocity, offer validation, and manual-close evidence still require real elapsed time and operator action.
```
Change the “Active work” introduction to:
```markdown
**[`docs/phase-a-backlog.md`](docs/phase-a-backlog.md)** is the current Phase A implementation record and validation contract. G1–G4 are implemented and must not be replayed from their historical task lists. Read the record and the key facts below before changing scoring, enrichment, ICP, search configs, or the pipeline.
```
Leave every non-negotiable invariant and current key fact intact.
- [ ] **Step 4: Update ****`CLAUDE.md`**
Replace its stale G1→G4 instruction with:
```markdown
Current Phase A implementation record and validation contract:
**[docs/phase-a-backlog.md](docs/phase-a-backlog.md)**. G1–G4 are implemented; do not replay their
historical task lists. Read the record before changing scoring, enrichment, ICP, search configs, or
the pipeline. Keep every change small, independently revertible, and verified with
`.venv/bin/python -m unittest discover -s tests`.
```
- [ ] **Step 5: Verify the documentation has no stale implementation claims**
Run:
```bash
rg -n 'planned, not started|Sonnet 4\.6|source="count_only"|money_signal =|^- \[ \]|Work groups G1→G4 in order|~80% built' \
  docs/phase-a-backlog.md AGENTS.md CLAUDE.md
```
Expected: no matches.
Then run:
```bash
rg -n 'tri-state|only `yes`|eleven metros|airtable-live|zero closes|3–5 closes|must not be replayed' \
  docs/phase-a-backlog.md AGENTS.md CLAUDE.md
git diff --check
```
Expected: the current invariants are present and `git diff --check` prints nothing.
- [ ] **Step 6: Commit the documentation reconciliation**
Run:
```bash
git add docs/phase-a-backlog.md AGENTS.md CLAUDE.md
git commit -m "docs: reconcile Phase A with implemented state"
```
---
### Task 4: Remove the two exact ignored credential backups
**Files:**
- Delete from the original checkout only: `.env.backup.20260604_114651`
- Delete from the original checkout only: `.env.backup.20260604_115027`
**Interfaces:**
- Consumes: two explicitly named ignored, untracked plaintext backup files.
- Produces: neither file exists in `/home/lucascoh/local-lead-machine`.
- Git output: none; ignored untracked deletions are not committable.
- [ ] **Step 1: Return to the original checkout without switching its branch**
Run:
```bash
cd /home/lucascoh/local-lead-machine
git status --short --branch
```
Expected: the active feature branch and its unrelated untracked config remain present.
- [ ] **Step 2: Verify exact targets without reading them**
Run:
```bash
test -f .env.backup.20260604_114651
test -f .env.backup.20260604_115027
git check-ignore .env.backup.20260604_114651
git check-ignore .env.backup.20260604_115027
```
Run these separately and expect both to fail, proving neither file is tracked:
```bash
git ls-files --error-unmatch .env.backup.20260604_114651
git ls-files --error-unmatch .env.backup.20260604_115027
```
If either file is tracked, stop and report instead of deleting it.
- [ ] **Step 3: Delete only the two authorized targets**
Run exactly:
```bash
rm -- /home/lucascoh/local-lead-machine/.env.backup.20260604_114651 /home/lucascoh/local-lead-machine/.env.backup.20260604_115027
```
Do not use a wildcard, recursive delete, environment variable, or command substitution. Do not delete `.env`.
- [ ] **Step 4: Verify deletion and preserve unrelated state**
Run:
```bash
test ! -e .env.backup.20260604_114651
test ! -e .env.backup.20260604_115027
git status --short --branch
git status --short -- search_configs/garage_doors_cleveland_medium_002.json
```
Expected: both backups are absent and the unrelated garage-doors config has exactly the same status it had in Task 1.
There is no commit for this task because the deleted files were ignored and untracked.
---
### Task 5: Final verification and local integration handoff
**Files:**
- Verify: all tracked files changed by Tasks 2–3
- Read: Git history from `origin/main` through the closeout branch
**Interfaces:**
- Consumes: two local closeout commits and the existing remediation history.
- Produces: a verified local branch plus a precise integration report.
- External state: unchanged.
- [ ] **Step 1: Run the complete verification gate in the isolated worktree**
Run:
```bash
.venv/bin/python -m unittest discover -s tests
make compile
git diff --check fix/repo-remediation..HEAD
git status --short --branch
```
If the worktree uses the original interpreter:
```bash
/home/lucascoh/local-lead-machine/.venv/bin/python -m unittest discover -s tests
make PY=/home/lucascoh/local-lead-machine/.venv/bin/python compile
```
Expected: all tests pass, compile exits `0`, no whitespace errors, and no uncommitted tracked changes.
- [ ] **Step 2: Verify the closeout contains only the intended tracked scope**
Run:
```bash
git diff --name-status fix/repo-remediation..HEAD
git log --oneline --decorate fix/repo-remediation..HEAD
```
Expected changed files:
```plain text
M AGENTS.md
M CLAUDE.md
M docs/phase-a-backlog.md
M tests/test_dnc_enrichment.py
```
Expected closeout commits:
```plain text
test(compliance): isolate DNC entrypoints from dotenv
docs: reconcile Phase A with implemented state
```
- [ ] **Step 3: Inventory the branch range that would be integrated**
Run:
```bash
git log --reverse --format='%h %s' origin/main..HEAD
```
In the handoff, separate:
1. Original remediation and its direct follow-ups: `741884f` through `c2e343b`.
2. Later post-send CRM work already on `fix/repo-remediation`: `a08c8c6` through `9257cc3`.
3. The two new closeout commits.
Do not silently describe the entire range as only the old remediation plan.
- [ ] **Step 4: Report and stop**
Report:
- closeout branch name and final commit hash;
- exact test count and `OK` result from the fresh run;
- compile exit code;
- confirmation that both `.env.backup.*` files were removed without reading them;
- confirmation that the active feature checkout and its untracked garage-doors config were preserved;
- current `origin/main` and `fix/repo-remediation` hashes;
- the three-part commit-range classification from Step 3;
- that no push, PR, merge, Airtable write, paid LLM call, Places call, or live enrichment occurred.
Stop before any push, PR creation, merge, cherry-pick into `main`, or live-system verification.
---
## Completion Criteria
This plan is complete only when all of the following are true:
- The three DNC entry-point tests pass even when the isolated worktree contains a safe `.env` with `ALLOW_MISSING_SUPPRESSION_INDEX=1`.
- The full stdlib unittest suite passes from the closeout worktree.
- `make compile` passes.
- Production compliance and pipeline code are unchanged.
- Phase A documentation says G1–G4 software is implemented while operating validation and Gate 0 remain active.
- The two exact plaintext `.env.backup.*` files no longer exist in the original checkout.
- The active Airtable feature branch and its untracked garage-doors config are untouched.
- The closeout branch contains exactly two new tracked commits.
- No external write or paid API call occurred.
- Integration is left for explicit user authorization after the commit-range handoff.

