# Dashboard Legacy Cutover and Accuracy Contract

Date: 2026-07-20

Status: Approved design direction; implementation pending review of this document

## Objective

Make the batch dashboard an accurate operator view of every canonical batch while removing obsolete Airtable schema from the active system.

The cutover preserves every Airtable lead and every historical batch artifact. It deletes only seven redundant Airtable fields, explicitly marks incompatible historical batches as `Legacy`, and prevents historical scoring semantics from being presented as current sendability.

## Problem Statement

The dashboard currently mixes three different kinds of truth:

1. literal artifact counts, such as enriched and scored rows;
2. identity-based pipeline progress, such as which enriched leads were scored;
3. current commercial-readiness and sendability rules.

That mixture causes false results. For example, `detailing_columbus_medium_001` has 140 enriched rows and 140 rows in its canonical scored CSV, but the dashboard reports zero scored because the two artifacts use different identity shapes. The same historical scored file lacks the current commercial-readiness fields, so evaluating it through today's sendability gate produces zero sendable even though it previously had 16 packet-ready rows.

Zero is not an acceptable fallback for unknown or incompatible data. The dashboard must distinguish literal counts, current-state evaluations, historical labels, and unresolved ambiguity.

## Scope

This change includes:

- an explicit registry of legacy batches;
- backend batch classification and accurate stage accounting;
- active versus legacy dashboard presentation and filtering;
- strict current-schema enforcement in the active scoring pipeline;
- retirement of active legacy score/backfill compatibility;
- Airtable removal of seven redundant legacy fields, with backup and verification;
- migration/audit tooling and tests that prove counts reconcile or display `Unknown`;
- a full audit of every canonical batch exposed by the dashboard.

This change does not:

- delete or rewrite historical batch artifacts;
- delete, suppress, or otherwise invalidate any Airtable lead;
- rescore legacy batches automatically;
- reinterpret historical packet eligibility as current sendability;
- build autonomous outreach or sending;
- make Airtable the source of batch-stage truth.

## Sources of Authority

Each dashboard value has one named authority:

| Value | Authority |
| --- | --- |
| Batch existence and classification | Canonical batch inventory plus explicit legacy registry |
| Discovered count | Canonical discovery artifact |
| Enriched count | Canonical enriched artifact |
| Scored count | Canonical scored artifact, with identity reconciliation status shown separately |
| Unscored count | Stable-identity set difference when identity reconciliation is complete |
| Current sendable count | Current sendability gate applied only to current-schema scored leads |
| Historical packet-ready count | Stored historical packet classification, labeled `Historical` |
| Airtable state | Verified sync receipt or live read-only audit; otherwise `Unknown` |
| Latest activity | Latest canonical artifact or verified receipt timestamp |

Filesystem artifacts remain the owner/operator pipeline authority. Airtable remains the rep-facing CRM and outreach record.

## Accuracy Contract

The dashboard must obey all of the following rules.

### Literal stage counts

- Discovered, enriched, and scored totals are literal row counts from their canonical artifacts after the artifact's normal row validation.
- The dashboard must never substitute an identity-join result for the scored artifact's literal row count.
- Duplicate or malformed rows are surfaced in diagnostics rather than silently hidden.

### Cross-stage reconciliation

- `Unscored` is calculated only when enriched and scored records can be reconciled by stable identity.
- The preferred identity order is: Place Key, Google Place ID, normalized phone, normalized website domain, then unique normalized business-name plus city.
- Fallback identities are permitted only when the match is unique on both sides.
- Ambiguous, conflicting, or unreconciled identities produce `Unknown` plus a diagnostic count. They never produce a guessed zero.
- The UI distinguishes `140 scored rows` from `140/140 enriched leads reconciled`.

### Current sendability

- `Sendable` always means the complete current `lead_machine/sendability.py` gate passed.
- Only active batches with current-schema score fields are evaluated for current sendability.
- Missing current commercial-readiness fields fail closed for shipping, but the dashboard reports the batch as incompatible or incomplete instead of implying that every lead was affirmatively rejected.
- Historical packet tiers are never added to current sendable totals.

### Airtable state

- Airtable status is one of `Verified`, `Partial`, or `Unknown`.
- `Verified` requires a successful receipt or live audit that covers the expected stable identities.
- `Partial` requires evidence of a strict subset of expected identities or a partial sync result.
- No receipt and no live audit means `Unknown`, not `Not Run` and not zero.
- Receipts record batch, expected identities, created/updated/unchanged/error counts, timestamp, and schema/version information sufficient to reproduce the status.

### Visibility and totals

- Every canonical batch appears in the `All` view.
- The dashboard supports `All`, `Active`, and `Legacy` filters.
- Every row has an explicit `Active` or `Legacy` badge.
- Active operational totals, attention queues, and suggested next actions exclude legacy batches.
- Legacy batches remain inspectable and show their literal historical stage counts.

## Legacy Batch Registry

The classification authority will be a tracked file:

`lead_machine/dashboard/legacy_batches.json`

Each entry contains:

- `batch`: canonical batch identifier;
- `legacy_since`: ISO date;
- `reason`: machine-readable reason code;
- `note`: concise operator explanation.

Example shape:

```json
{
  "schema_version": 1,
  "batches": [
    {
      "batch": "detailing_columbus_medium_001",
      "legacy_since": "2026-07-20",
      "reason": "pre_commercial_readiness_schema",
      "note": "Historical scores are preserved and are not current-sendability compatible."
    }
  ]
}
```

The registry is explicit authority; runtime schema guessing does not silently reclassify a batch. An audit command may propose candidates, but changes to the tracked registry are reviewed changes.

Configured or enriched-but-unscored batches remain active if they can be processed by the current scorer. A batch is not legacy merely because it is old.

## Legacy Batch Presentation

A legacy row displays:

- literal discovered, enriched, and scored artifact counts;
- identity reconciliation status, including `Unknown` when necessary;
- historical packet counts when present, labeled `Historical packet-ready` and `Historical manual review`;
- no current sendable total unless current-schema scores genuinely exist and the batch is intentionally migrated back to active;
- Airtable verification state using the same receipt/audit rules as active batches;
- a next action that explains it is preserved history, not active scoring work.

Legacy detail views do not apply today's sendability gate to old score rows. They explain which schema incompatibility caused the classification.

## Active Pipeline Cutover

The active pipeline becomes strict about the current scoring contract:

- imports that omit required current score and commercial-readiness fields are rejected with actionable validation errors;
- legacy score annotation and commercial-readiness backfill paths are removed from normal pipeline commands;
- packet generation consumes only current-schema score rows and the current objective gate;
- compatibility code whose only purpose is rewriting old scoring semantics is retired from active code, tests, and documentation;
- safe sparse-record identity matching remains supported, but is named fallback identity matching rather than legacy matching.

Historical files remain readable by the dashboard's legacy reader. They are not rewritten to look current.

## Airtable Schema Cutover

### Fields to delete

The following seven Airtable fields are redundant and will be deleted:

1. `Legacy Current Website Quality`
2. `Legacy Send Trigger`
3. `Legacy Last Contacted`
4. `Legacy Recommended Action`
5. `Legacy Form Strength`
6. `Legacy CTA Strength`
7. `Legacy Skip Reason`

The corresponding canonical fields remain. The live pre-cutover audit found no value that exists only in a legacy field. The one apparent `Last Contacted` difference is formatting of the same date, not unique data.

### Safe deletion procedure

Because Airtable's public Web API exposes schema reads but not field deletion, the actual field deletion is performed in the Airtable UI. Repository automation prepares and verifies the cutover:

1. Record the pre-cutover table record count and schema.
2. Export an ignored backup of the seven fields keyed by Airtable record ID and stable lead identity.
3. Run a redundancy assertion proving no legacy-only values remain.
4. Produce the exact seven-field deletion checklist.
5. Delete only those fields in the Airtable UI.
6. Run a post-cutover live schema and record-count audit.
7. Fail verification unless all seven fields are absent, canonical replacements remain, and the lead record count is unchanged.

No lead records are deleted. Existing leads remain valid for contact.

### Repository cleanup

All upload/download allowlists, mappings, documentation, and tests stop referencing the seven deleted field names. A regression test prevents a deleted legacy field from re-entering the active Airtable schema contract.

## Backend Response Model

The batch API should expose enough structure that the UI never infers semantics from a number alone. The response includes, conceptually:

- `lifecycle`: `active` or `legacy`;
- `stage_counts`: literal artifact counts;
- `reconciliation`: status, matched count, unmatched counts, and ambiguity count;
- `current_sendability`: status and count, where status can be `evaluated`, `incompatible`, or `unknown`;
- `historical_packets`: explicitly historical counts when available;
- `airtable`: `verified`, `partial`, or `unknown`, with evidence timestamp;
- `diagnostics`: concise operator-visible explanations;
- `next_action`: lifecycle-aware action or informational state.

Existing API fields may be retained temporarily for a coordinated frontend migration, but they must be derived from this contract and must not preserve incorrect semantics.

## Dashboard UX

The inventory header shows counts for all canonical batches and the selected lifecycle filter. The default operator view may remain `Active`, but `All` is a first-class filter and exposes every batch.

The table separates:

- literal `Enriched` and `Scored` counts;
- reconciliation-derived `Unscored`, which can display `Unknown`;
- `Current sendable` for eligible active batches;
- `Historical packet-ready` for legacy batches;
- Airtable evidence state.

Status color alone is not relied upon. Labels contain the words `Legacy`, `Historical`, `Unknown`, `Partial`, or `Verified` as applicable.

## Migration and Operational Sequence

Implementation proceeds in this order:

1. Add failing backend contract tests and fixture coverage for active, legacy, ambiguous, and no-receipt batches.
2. Add the explicit legacy registry and strict batch reader model.
3. Update the dashboard API and UI with lifecycle filters and labeled historical values.
4. Add the full batch audit report and resolve every discrepancy or mark it `Unknown` with evidence.
5. Remove active legacy scoring/backfill compatibility and update its tests/docs.
6. Remove the seven legacy Airtable field references from repository contracts.
7. Generate the Airtable backup, redundancy proof, and deletion checklist.
8. User deletes the exact seven Airtable fields in the UI.
9. Run the live post-cutover schema, record-count, and identity audit.

Steps 1 through 7 do not mutate Airtable. Step 8 is the only destructive external schema action.

## Acceptance Criteria

The change is complete only when all of the following are demonstrated:

- every canonical batch is present in the `All` dashboard view;
- every batch has an explicit lifecycle classification;
- active and legacy filters return exactly their classified subsets;
- active operational totals and attention queues exclude legacy batches;
- literal stage counts match canonical artifact row counts for every batch;
- active cross-stage counts reconcile by stable identity;
- ambiguous identity cases display `Unknown`, never a fabricated zero;
- legacy packet counts are visibly labeled historical and never counted as current sendable;
- current sendable counts exactly match the current objective gate;
- batches without Airtable evidence display `Unknown`;
- known verified historical syncs can be represented through durable receipts or verified audit evidence;
- no active source, schema allowlist, or documentation references the seven deleted Airtable fields;
- the pre/post Airtable lead record count is unchanged;
- the seven legacy fields are absent and their canonical replacements remain;
- backend unit tests, dashboard tests, frontend tests, frontend build, and the full unittest suite pass;
- a generated all-batch audit report contains no unexplained discrepancy.

## Rollback

Code changes are independently revertible. The registry and dashboard changes can be reverted without changing historical artifacts or Airtable leads.

Before Airtable field deletion, the ignored keyed export provides a recovery source. Recreating deleted Airtable columns, if ever required, is a manual schema operation followed by a controlled import from that export. This is a last-resort rollback; the redundancy proof is the primary guard.

## Decision Summary

Use an explicit legacy registry and strict current-schema pipeline. Preserve old batches as historical truth, preserve all leads, remove only redundant Airtable fields, and make the dashboard report uncertainty honestly. This fixes the dashboard by separating current operational readiness from historical scoring instead of forcing incompatible data into one misleading set of numbers.
