# Skills roadmap for local-lead-machine

The loop doctrine's build order is **reliable manual run → make it a skill → wrap in a loop →
schedule**. A "skill" here = a Claude Code project skill at `.claude/skills/<name>/SKILL.md` that
encodes a repeatable operating procedure (the *method*) so an agent runs it the same way every time.
These are candidates, ordered by leverage. None are built yet — this is the menu.

## Tier 1 — build alongside Phase A (highest leverage now)

### `run-lead-batch` — the operating skill for Loop A
The "make it a skill" step of the MVL. Encodes the end-to-end run for a given config:
`search → enrich → score (api-llm) → packets → airtable`, with the budget caps enforced and the
per-run summary printed (leads in · A/B · sendable · $ spent vs caps). Wraps `run_pipeline.py` (G4)
with the pre-flight checks (`.env` keys present, caps set), the verification step (tests green), and
the stop conditions. *This is the procedure a future scheduler (Phase C) will call.*

### `add-vertical` — vertical-as-data scaffolder
Directly serves the G2 design goal (adding a vertical is a *data* change). Given a vertical name +
target/deny terms, it: adds a `VerticalProfile` to `lead_machine/verticals.py`, generates
`search_configs/<vertical>_<metro>_<size>.json`, and runs `tests.test_icp_lanes` +
`tests.test_lead_machine_contracts` to confirm no regression. Makes the **epoxy fast-follow** a
one-command job.

### `add-metro` — geo expansion
Serves the multi-metro requirement (Columbus / Cincinnati / Pittsburgh). Given a metro, authors
`search_configs/geo_slices/<metro>.json` and the per-metro config(s) for the active vertical, so list
volume can scale without hand-editing query lists (critical for low-density verticals like epoxy).

## Tier 2 — the verifier discipline (build as automation matures)

### `verify-batch` — independent maker≠checker reviewer
A read-only skill that audits a scored batch against the objective gate (the `sendability.py`
blockers): confirms each "sendable" lead has a real named leak + evidence-backed opener, flags
borderline grades, and reports the grade distribution shift after a scoring change. This is the
"spot-check 10 leads" verification from G3, made repeatable — and it generalizes into the **Loop B
asset checker** (does the mockup/video name a real, verifiable leak?).

## Tier 3 — Phase B/C (premature until a first close; see Gate 0)

### `build-asset-bundle` — Loop B asset factory (the moat)
Per A-lead, generate the mockup + customer-POV video script + scored audit behind the `verify-batch`
checker. **Do not automate sending off the back of this before a first close** (Gate 0). Until then
it's a *draft-only* accelerator for hand-built bundles.

### `outreach-sequence` — Loop C drafting in suggest mode
Drafts the warmth-tiered first-touch + follow-ups grounded in the packet evidence (opener +
evidence_refs + best_angle + safe_outreach_claims), under the fail-closed compliance gate. Queues for
human approval — never auto-sends by default.

---

**Note on scope creep:** a skill should encode a *method you already run reliably by hand*. Don't
skill-ify a step that isn't yet manually solid — that just automates an unverified procedure. Tier 1's
`run-lead-batch` is only worth building once Phase A's `run_pipeline.py --mode api-llm` path runs clean end-to-end.
