# Airtable Status Consolidation Design

**Date:** 2026-08-03

## Objective

Update the repository to match the live Airtable `Prospects.Status` consolidation that Lou has
already completed:

- `Do Not Contact` converges into `Not Fit`.
- `Sent` converges into `Follow-Up`.
- `Mockup Needed` converges into `New`.

This is a repository-only change. It performs no live Airtable writes and no record migration.

## Canonical Status Contract

The complete code-declared `Prospects.Status` vocabulary becomes:

1. `New`
2. `Manual Review`
3. `Not Fit`
4. `Follow-Up`
5. `Won`
6. `Lost`
7. `Booked`

The accept-rate partition becomes:

- Accepted: `Follow-Up`, `Booked`, `Won`, `Lost`
- Rejected: `Not Fit`
- Undecided: `New`, `Manual Review`

Every canonical status must belong to exactly one partition.

## Workflow Changes

### Queue admission

The ready-to-contact queue accepts only `Status = New`. The generated Airtable filter formula,
the row-level defensive evaluator, and the case-normalized compliance guard must remain equivalent.

### Upload defaults

A newly uploaded sendable lead defaults to `New`, not `Mockup Needed`. `Manual Review` remains the
status for leads routed to human review. Existing Airtable status values remain human-owned and
must not be overwritten by update payloads.

### Contact logging

`mark_sent.py` remains the name of the command because it records the operator's completed contact
action. Its Airtable transition changes from `New -> Sent` to `New -> Follow-Up`. It still writes
`Last Contacted`, `Outreach Channel`, and `Next Follow-Up` and preserves its fail-closed readiness,
consent, suppression, and exact-idempotency checks.

Receipts from the command report `status: Follow-Up`. Internal event labels that describe the
historical action (for example, a `sent` ledger event or `mark-sent` command) remain action names;
they are not members of the Airtable status vocabulary.

### Outcome logging

`mark_outcome.py` accepts only `Follow-Up` as an open post-contact source status. Reply and meeting
events continue to keep the record in `Follow-Up`; closing events still move it to `Won` or `Lost`.

### Opt-out safety

Removing `Do Not Contact` from the visible Airtable status vocabulary does not remove or weaken
suppression. Opt-outs remain represented by the configured suppression ledger and checked
fail-closed before contact. When a visible terminal status is needed, the consolidated status is
`Not Fit`; that status alone is not a substitute for a suppression-ledger entry.

## Required-Choice and Documentation Changes

The required-choice checker and schema documentation must stop requiring the three deleted choices.
Operator documentation, queue formulas, help/error text, examples, and current-state descriptions
must use the consolidated vocabulary. Historical audit or cutover evidence remains unchanged when
it describes what was true at that time; current instructions embedded in those documents may be
updated only when they would otherwise direct an operator to recreate a deleted choice.

## Testing

Implementation follows test-driven development:

1. Change literal contract tests first and verify they fail against the old implementation.
2. Update the smallest production surfaces needed to satisfy the new contract.
3. Run focused suites for queue state, send logging, outcomes, Hermes integration, required choices,
   uploads, and accept-rate evaluation.
4. Run the authoritative full suite with `.venv/bin/python -m unittest discover -s tests`.
5. Run repository searches for the removed status choices and classify any remaining occurrences as
   deliberate historical evidence or stale current guidance.

No test or implementation step may make a live Airtable request.

## Scope Boundaries

- Do not mutate the live Airtable base.
- Do not add `typecast` to any Airtable write.
- Do not rename the `mark_sent` command or historical sent-event schema.
- Do not change follow-up cadence, scoring, ICP, enrichment, or suppression semantics.
- Do not edit or discard unrelated vertical/search work already present in the worktree.
- Do not commit or push unless Lou explicitly asks.
