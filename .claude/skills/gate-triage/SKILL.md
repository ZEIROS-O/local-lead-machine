---
name: gate-triage
description: >-
  Diagnose a red check in the local-lead-machine CI — the daily "Airtable gates"
  workflow (required choices, schema report, corpus completeness, verdict
  reproducibility, gate liveness, accept-rate KPI) or the Tests workflow. Use when a
  gate goes red, when asked "why is CI failing" / "what does this gate mean", when the
  gates dashboard shows several failures at once, or before trusting a green run. Each
  gate has a different runbook and a different meaning; this maps the failure to the
  fix instead of re-deriving it.
---

# Gate triage

Two workflows, and they fail for unrelated reasons.

| Workflow | Runner | Trigger | What red means |
|---|---|---|---|
| `.github/workflows/tests.yml` | `ubuntu-latest` | push, PR, daily 11:17 UTC | The code is wrong, or a CI dep is missing |
| `.github/workflows/gates.yml` | **self-hosted `lpm`** | daily 11:37 UTC, `workflow_dispatch` | The **live base** or the **scored corpus** has drifted from the code |

The gates workflow reads the production Airtable base and the scored corpus on the machine
that owns it. It cannot run on a hosted runner — that is why it is self-hosted, and why
"queued forever" is a real failure mode.

## Triage order

1. **Which workflow?** `gh run list --limit 10`, then `gh run view <id> --log-failed`.
2. **Is it infrastructure or a real finding?** Preflight failures, a missing dep, and an
   offline runner are infrastructure. Everything else is the gate telling you something true.
3. **Reproduce locally before fixing.** Every gate is a plain script you can run by hand.

## Prerequisites the gates workflow needs

Set up once; when any of these go missing the failure looks like a gate failure but is not:

- A self-hosted Linux runner labelled **`lpm`** on the machine holding the scored corpus.
- Actions **variable** `LPM_SCORED_OUTPUT_DIR` — absolute path to that machine's scored-output
  directory.
- Actions **secrets** `AIRTABLE_PAT` and `AIRTABLE_BASE_ID`.

## The checks, in workflow order

### Preflight scored corpus — exit 2
`LPM_SCORED_OUTPUT_DIR` unset, or the directory does not exist.

**Not a code problem.** Either the repository variable is missing or the runner is on the wrong
machine. Nothing downstream can be trusted when this fails — every later gate reads that
directory.

### Check required Airtable choices
```bash
.venv/bin/python airtable_required_choices.py check
```
Exit 2 = the live base does **not** offer a select choice the code writes. The output names the
missing choice and prints the UI steps.

**Meaning:** a write path is about to 422 in production. The API cannot add a choice to an
existing select — this is hand work in the UI. Use the `airtable-schema-change` skill.

### Check Airtable schema
```bash
.venv/bin/python upload_airtable.py --schema-report
```
The base no longer matches `upload_airtable._FIELD_SCHEMA` — a field was renamed, deleted, or
retyped in the UI, or code added a field the base lacks.

### Check scored corpus completeness
```bash
.venv/bin/python evals/completeness.py --output-dir "$LPM_SCORED_OUTPUT_DIR"
```
Read-only reconciliation between the scored CSVs and Airtable `Prospects`. Red means the corpus
and the base disagree about which leads exist.

**Expect a long tail of benign findings.** The 2026-08-01 pass found 265 blank scored keys, 79
conflicts and 551 orphans, and the orphans were obsolete CSVs and intentional exclusions. Read
the categories before treating the count as damage.

### Check verdict reproducibility
```bash
.venv/bin/python evals/reproducibility.py --output-dir "$LPM_SCORED_OUTPUT_DIR"
```
Re-derives every verdict from stored evidence by calling the same `evaluate_sendability` +
`evaluate_packet_tier` the pipeline calls, and diffs against the base.

**Red means a row sitting in front of a rep was decided by a version of the gate that no longer
exists.** This is the most serious of the six — it is a live correctness statement, not a
hygiene check.

Also the tool for proving a refactor is a pure projection: `--snapshot-out` before,
`--baseline <snapshot>` after, require an empty diff.

### Check gate liveness
```bash
.venv/bin/python evals/gate_liveness.py --output-dir "$LPM_SCORED_OUTPUT_DIR"
```
No Airtable secrets — it replays the corpus through the gates and never reads the base. Three
distinct categories, and they mean different things:

| Category | Meaning | Severity |
|---|---|---|
| `unreferenced` | The blocker constant exists and nothing outside its own declaration reads it. Tests do not count as a reader. | Real. This is the exact shape of the deleted `MONEY_SIGNAL_BLOCKER`. |
| `never_fires` | Reachable from `evaluate_sendability`, zero corpus rows tripped it. | Real — nothing proves it works. |
| `outside_replay` | Minted by a different entry point (`upload_airtable`, `manual_lead_insert`, `readiness_flags`). | **Reported, never failed.** A one-entry-point replay cannot exercise it. |

The roster is discovered from the module, so a new blocker joins the eval the moment it is
declared. That also means declaring a blocker and not wiring it up turns this gate red.

### Report the accept-rate KPI
```bash
.venv/bin/python evals/accept_rate.py --summary-file "$GITHUB_STEP_SUMMARY"
```
`accept rate = accepted / (accepted + rejected)` over the sendable tier, read off human decisions
in the base. AGENTS.md: **below ~50% the loop is losing.**

**Three outcomes, not two.** This step writes its own summary line because
`insufficient-sample` is a legitimate third result — it never prints PASS on no data, since a
KPI reading green with nothing behind it is the same defect as a gate that cannot fail. As of
2026-08-01 the base had 12/0/12 → insufficient-sample. That is not a failure to fix; it is a
loop that has not yet produced enough decisions.

Leads still in the queue are undecided and stay **out** of the denominator — counting them would
report the machine's own proposals back as approvals.

## Known-cause shortlist

| Symptom | Cause | Fix |
|---|---|---|
| Gates workflow queued, never starts | Self-hosted `lpm` runner offline | Bring the runner up; nothing else will help |
| Preflight exit 2 | `LPM_SCORED_OUTPUT_DIR` unset/wrong | Repository variable, not code |
| `ModuleNotFoundError` in Tests | Dep missing from the CI install line | Add it to `tests.yml` — `lxml` was the 2026-08-02 instance |
| Required-choices exit 2 | Select value added in code only | Hand-create the choice in the base |
| accept-rate reads a field that does not exist | KPI pointed at a deleted column | Point it at `Verdict`; there is a parity test |
| Everything red at once | Preflight or runner failed first | Fix the top of the list, re-run; do not triage six findings that share one cause |

## Do not

- **Do not** call a gate green because it stopped failing without a change — check what it
  actually asserted.
- **Do not** treat `outside_replay` or `insufficient-sample` as failures. They are honest
  reports of what a check cannot know.
- **Do not** fix a gate by loosening it. Every one of these exists because the looser version
  shipped something wrong once.
