# Dashboard Legacy Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every canonical batch visible with accurate artifact counts, honest reconciliation and sendability states, explicit legacy classification, and a safely verified Airtable legacy-field cutover that preserves all lead records.

**Architecture:** The artifact-backed dashboard gains two focused domain modules: ordered stable-identity reconciliation and an explicit tracked legacy registry. The dashboard API separates literal counts from reconciliation-derived values, current sendability from historical packet tiers, and Airtable evidence from missing receipts; the React UI renders those distinctions directly. The active scoring path becomes schema-v2-only, while a separate read-only Airtable cutover utility proves redundancy, creates a keyed backup, and verifies the user-performed UI deletion.

**Tech Stack:** Python 3.10, stdlib `unittest`, FastAPI, React 19, TypeScript 5.7, Vitest, Airtable Web API via existing `requests`, JSON/CSV/JSONL artifacts.

## Global Constraints

- Preserve every Airtable lead record and every historical batch artifact.
- Delete only these Airtable fields: `Legacy Current Website Quality`, `Legacy Send Trigger`, `Legacy Last Contacted`, `Legacy Recommended Action`, `Legacy Form Strength`, `Legacy CTA Strength`, and `Legacy Skip Reason`.
- `Sendable` means the complete current `lead_machine/sendability.py` gate only.
- Historical packet tiers are labeled historical and never contribute to active sendable totals.
- Ambiguous or unreconciled cross-stage state is `Unknown`, never a guessed zero.
- Airtable state is `Verified`, `Partial`, or `Unknown`; absence of a receipt is `Unknown`.
- Airtable remains the rep-facing CRM; the dashboard remains a read-only owner/operator console.
- Use existing `requests`; add no dependencies.
- Use `.venv/bin/python -m unittest discover -s tests` for authoritative backend verification.
- Preserve unrelated dirty-worktree changes. Never stage `search_configs/water_restoration_cleveland_medium_003.json` unless the user separately requests it.
- Existing uncommitted receipt/manual-artifact work in `lead_machine/hermes_batch.py`, `lead_machine/airtable_receipts.py`, `lead_machine/dashboard/data.py`, `tests/test_dashboard.py`, and `dashboard-ui/src/*` is in scope and must be integrated rather than reset.
- Before every commit, run `git diff --cached --check` and confirm `git diff --cached --name-only` contains only that task's files.

---

## File Structure

### New files

- `lead_machine/dashboard/identity.py`: ordered, one-to-one stable-identity reconciliation with explicit unknown state.
- `lead_machine/dashboard/legacy.py`: typed loader and validator for the tracked legacy registry.
- `lead_machine/dashboard/legacy_batches.json`: explicit classification authority for historical incompatible batches.
- `tests/test_dashboard_identity.py`: unit tests for identity priority, fallback, ambiguity, and partial scoring.
- `audit_dashboard_batches.py`: read-only all-batch contract audit and machine-readable report.
- `tests/test_dashboard_audit.py`: audit command tests.
- `airtable_schema_cutover.py`: pre-delete backup/redundancy proof and post-delete verification.
- `tests/test_airtable_schema_cutover.py`: pure cutover logic and mocked HTTP tests.

### Modified files

- `lead_machine/dashboard/data.py`: literal counts, lifecycle, reconciliation, sendability compatibility, Airtable evidence, active-only totals.
- `lead_machine/dashboard/api.py`: lifecycle query parameter.
- `lead_machine/hermes_batch.py`: retain canonical manual scored path and receipt directory already present in the worktree.
- `lead_machine/airtable_receipts.py`: retain and integrate verified cohort receipt validation already present in the worktree.
- `lead_machine/score_schema.py`: shared current scored-row contract validation and CSV aliases.
- `lead_machine/manual_scores.py`: require schema v2 and remove deterministic legacy backfill.
- `import_llm_score_bundle.py`: keep its current strict v2 validation and add a regression assertion for versionless scores.
- `refresh_packet_tiers.py`: recompute tiers only for schema-v2 rows; never migrate old rows.
- `dashboard-ui/src/types.ts`: lifecycle, nullable unscored, reconciliation, historical packet, current-sendability, and Airtable-evidence types.
- `dashboard-ui/src/App.tsx`: lifecycle filters/badges and honest labels.
- `dashboard-ui/src/App.test.tsx`: active/legacy/unknown rendering tests.
- `dashboard-ui/src/styles.css`: lifecycle and historical-state presentation.
- `tests/test_dashboard.py`: repository and API contract coverage.
- `tests/test_lead_machine_contracts.py`: replace legacy-score acceptance/backfill tests with strict rejection tests.
- `tests/test_readiness_migration.py`: prove refresh is read-only/refusing for old schemas.
- `tests/test_scoring_v3_operations.py`: remove the obsolete legacy annotation test/import.
- `tests/test_upload_airtable_schema.py`: ensure deleted legacy field names are absent from active schema contracts.
- `docs/dashboard.md`: document lifecycle/count/Airtable semantics.
- `docs/airtable_schema_required.md`: regenerate current schema documentation.

### Deleted files

- `lead_machine/commercial_backfill.py`: obsolete active migration behavior.
- `annotate_legacy_scoring.py`: obsolete legacy annotation command.

---

### Task 1: Stable Identity Reconciliation

**Files:**
- Create: `lead_machine/dashboard/identity.py`
- Create: `tests/test_dashboard_identity.py`

**Interfaces:**
- Consumes: enriched rows in nested `{"google": {...}}` or flat form; scored CSV rows in title-case or snake-case form.
- Produces: `reconcile_rows(enriched_rows, scored_rows) -> Reconciliation`; `Reconciliation.as_dict() -> dict[str, int | str | None]`; `Reconciliation.scored_by_enriched_index -> dict[int, int]`.

- [ ] **Step 1: Write failing priority and partial-scoring tests**

```python
from lead_machine.dashboard.identity import reconcile_rows


def test_place_key_wins_and_partial_scoring_is_reconciled() -> None:
    enriched = [
        {"google": {"place_key": "p1", "business_name": "Alpha", "city": "Columbus"}},
        {"google": {"place_key": "p2", "business_name": "Beta", "city": "Columbus"}},
    ]
    result = reconcile_rows(enriched, [{"Place Key": "p2", "Business Name": "Wrong Name"}])
    assert result.status == "reconciled"
    assert result.matched == 1
    assert result.unmatched_enriched == 1
    assert result.unmatched_scored == 0
    assert result.scored_by_enriched_index == {1: 0}


def test_unique_phone_then_domain_then_name_city_fallbacks() -> None:
    enriched = [
        {"google": {"phone": "(614) 555-0100", "business_name": "Phone Co", "city": "Dublin"}},
        {"google": {"website_url": "https://domain.example/path", "business_name": "Domain Co", "city": "Hilliard"}},
        {"google": {"business_name": "Name Co", "city": "Westerville"}},
    ]
    scored = [
        {"Phone": "+1 614-555-0100"},
        {"Website URL": "https://www.domain.example/other"},
        {"Business Name": " Name Co ", "City": "WESTERVILLE"},
    ]
    result = reconcile_rows(enriched, scored)
    assert result.as_dict()["status"] == "reconciled"
    assert result.matched == 3
```

- [ ] **Step 2: Run tests and confirm the module is missing**

Run: `.venv/bin/python -m unittest tests.test_dashboard_identity -v`

Expected: `ModuleNotFoundError: No module named 'lead_machine.dashboard.identity'`.

- [ ] **Step 3: Implement ordered unique matching**

```python
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal

from lead_machine.schema import normalize_domain, normalize_phone


def _first(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    google = row.get("google")
    return google if isinstance(google, dict) else row


def _text(value: str) -> str:
    return " ".join(value.casefold().split())


def _place_key(row: dict[str, Any]) -> str:
    return _first(_flat(row), "Place Key", "place_key")


def _place_id(row: dict[str, Any]) -> str:
    return _first(_flat(row), "Google Place ID", "Place ID", "google_place_id", "place_id")


def _phone(row: dict[str, Any]) -> str:
    return normalize_phone(_first(_flat(row), "Phone", "phone"))


def _domain(row: dict[str, Any]) -> str:
    return normalize_domain(_first(_flat(row), "Website URL", "website_url", "website"))


def _name_city(row: dict[str, Any]) -> str:
    flat = _flat(row)
    name = _text(_first(flat, "Business Name", "business_name", "name"))
    city = _text(_first(flat, "City", "city"))
    return f"{name}|{city}" if name and city else ""


IDENTITY_STEPS: tuple[tuple[str, Callable[[dict[str, Any]], str]], ...] = (
    ("place_key", _place_key),
    ("place_id", _place_id),
    ("phone", _phone),
    ("domain", _domain),
    ("name_city", _name_city),
)


@dataclass(frozen=True)
class Reconciliation:
    status: Literal["reconciled", "unknown"]
    matched: int
    unmatched_enriched: int
    unmatched_scored: int
    ambiguous: int
    scored_by_enriched_index: dict[int, int]

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "status": self.status,
            "matched": self.matched,
            "unmatched_enriched": self.unmatched_enriched if self.status == "reconciled" else None,
            "unmatched_scored": self.unmatched_scored,
            "ambiguous": self.ambiguous,
        }


def reconcile_rows(enriched_rows: Iterable[dict[str, Any]], scored_rows: Iterable[dict[str, Any]]) -> Reconciliation:
    enriched = list(enriched_rows)
    scored = list(scored_rows)
    unmatched_enriched = set(range(len(enriched)))
    unmatched_scored = set(range(len(scored)))
    pairs: dict[int, int] = {}
    ambiguous_rows: set[tuple[str, int]] = set()
    for _label, extractor in IDENTITY_STEPS:
        left: dict[str, list[int]] = defaultdict(list)
        right: dict[str, list[int]] = defaultdict(list)
        for index in unmatched_enriched:
            if token := extractor(enriched[index]):
                left[token].append(index)
        for index in unmatched_scored:
            if token := extractor(scored[index]):
                right[token].append(index)
        for token in sorted(set(left) & set(right)):
            if len(left[token]) == 1 and len(right[token]) == 1:
                enriched_index, scored_index = left[token][0], right[token][0]
                pairs[enriched_index] = scored_index
                unmatched_enriched.remove(enriched_index)
                unmatched_scored.remove(scored_index)
            else:
                ambiguous_rows.update(("enriched", index) for index in left[token])
                ambiguous_rows.update(("scored", index) for index in right[token])
    unresolved_ambiguity = sum(
        1
        for side, index in ambiguous_rows
        if index in (unmatched_enriched if side == "enriched" else unmatched_scored)
    )
    status = "reconciled" if not unmatched_scored and not unresolved_ambiguity else "unknown"
    return Reconciliation(status, len(pairs), len(unmatched_enriched), len(unmatched_scored), unresolved_ambiguity, pairs)
```

- [ ] **Step 4: Add ambiguity tests and run the module suite**

```python
def test_duplicate_fallback_is_unknown_not_zero() -> None:
    enriched = [
        {"google": {"business_name": "Same Co", "city": "Columbus"}},
        {"google": {"business_name": "Same Co", "city": "Columbus"}},
    ]
    result = reconcile_rows(enriched, [{"Business Name": "Same Co", "City": "Columbus"}])
    assert result.status == "unknown"
    assert result.as_dict()["unmatched_enriched"] is None
    assert result.ambiguous == 3
```

Run: `.venv/bin/python -m unittest tests.test_dashboard_identity -v`

Expected: all identity tests pass.

- [ ] **Step 5: Commit the reconciliation unit**

```bash
git add lead_machine/dashboard/identity.py tests/test_dashboard_identity.py
git diff --cached --check
git commit -m "feat: reconcile dashboard stages by stable identity"
```

---

### Task 2: Explicit Legacy Registry and Honest Backend Counts

**Files:**
- Create: `lead_machine/dashboard/legacy.py`
- Create: `lead_machine/dashboard/legacy_batches.json`
- Modify: `lead_machine/dashboard/data.py:144-190,303-422,424-922`
- Modify: `lead_machine/score_schema.py:1-110`
- Modify: `lead_machine/hermes_batch.py:23-99`
- Create/modify: `lead_machine/airtable_receipts.py`
- Modify: `tests/test_dashboard.py:99-394`

**Interfaces:**
- Consumes: `reconcile_rows()` from Task 1 and current score-state constants from `lead_machine.score_schema`.
- Produces: `load_legacy_batches(root) -> dict[str, LegacyBatch]`; `validate_current_scored_row(row) -> list[str]`; batch response fields `lifecycle`, `legacy`, `reconciliation`, `current_sendability`, `historical_packets`, and `airtable.evidence`.

- [ ] **Step 1: Write failing legacy and literal-count dashboard tests**

```python
def test_legacy_batch_uses_literal_counts_and_historical_packets(self) -> None:
    self.paths.enriched.write_text(
        "\n".join(json.dumps(row) for row in enriched_rows(2)) + "\n",
        encoding="utf-8",
    )
    legacy_rows = scored_rows(2)
    for row in legacy_rows:
        for key in ("Score Schema Version", "Commercial Fit State", "Contact Eligibility", "Asset State", "Promise State"):
            row.pop(key, None)
    write_csv(self.root / f"output/scored_{BATCH}.csv", legacy_rows)
    (self.root / "lead_machine/dashboard/legacy_batches.json").write_text(
        json.dumps({"schema_version": 1, "batches": [{"batch": BATCH, "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "Preserved history"}]}),
        encoding="utf-8",
    )
    batch = self.repo.batch(BATCH, force=True)
    assert batch is not None
    self.assertEqual(batch["lifecycle"], "legacy")
    self.assertEqual(batch["counts"]["scored"], 2)
    self.assertEqual(batch["current_sendability"]["status"], "incompatible")
    self.assertIsNone(batch["current_sendability"]["count"])
    self.assertEqual(batch["historical_packets"]["sendable"], 1)


def test_unreconciled_active_batch_reports_unknown_unscored(self) -> None:
    write_enriched(self.paths.enriched, 2)
    rows = scored_rows(2)
    rows[1]["Place Key"] = "not-in-enrichment"
    write_csv(self.paths.scored_csv, rows)
    batch = self.repo.batch(BATCH, force=True)
    assert batch is not None
    self.assertEqual(batch["counts"]["scored"], 2)
    self.assertIsNone(batch["counts"]["unscored"])
    self.assertEqual(batch["reconciliation"]["status"], "unknown")
```

Update the shared `scored_rows()` test helper so its default fixture is genuinely current-schema-v2. Add these fields to every generated row:

```python
"Score Schema Version": "2",
"Commercial Materiality": "Material consequence",
"Recurring Consequence": "Recurring consequence",
"Root Cause Confidence": "High confidence",
"LPM Offer Component": "Get the Lead",
"Packageable Solution": "Estimate funnel",
"Value Mechanism": "More completed inquiries",
"Beyond Free Fix": "Multi-step conversion path",
"Self Fix Risk": "Low",
"Intentionality": "Not intentional",
"Asset State": "none",
"Promise State": "asset-free",
"Asset ID": "",
"Asset Verifier Verdict": "",
```

- [ ] **Step 2: Run the focused tests and confirm current semantics fail**

Run: `.venv/bin/python -m unittest tests.test_dashboard.DashboardRepositoryTest.test_legacy_batch_uses_literal_counts_and_historical_packets tests.test_dashboard.DashboardRepositoryTest.test_unreconciled_active_batch_reports_unknown_unscored -v`

Expected: failures for missing lifecycle/current-sendability fields and current `counts.scored` intersection behavior.

- [ ] **Step 3: Add and validate the registry loader**

```python
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class LegacyBatch:
    batch: str
    legacy_since: str
    reason: str
    note: str


def load_legacy_batches(root: Path) -> dict[str, LegacyBatch]:
    path = root / "lead_machine/dashboard/legacy_batches.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1, "batches": []}
    if value.get("schema_version") != 1 or not isinstance(value.get("batches"), list):
        raise ValueError("legacy batch registry must use schema_version 1 and a batches list")
    result: dict[str, LegacyBatch] = {}
    for raw in value["batches"]:
        item = LegacyBatch(**raw)
        if not all((item.batch, item.legacy_since, item.reason, item.note)) or item.batch in result:
            raise ValueError(f"invalid or duplicate legacy batch registry entry: {item.batch!r}")
        result[item.batch] = item
    return result
```

Populate `lead_machine/dashboard/legacy_batches.json` with exactly these initial entries, each using `legacy_since: "2026-07-20"` and `reason: "pre_commercial_readiness_schema"`:

```json
{
  "schema_version": 1,
  "batches": [
    {"batch": "detailing_cleveland_medium_001", "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "Historical scores are preserved and are not current-sendability compatible."},
    {"batch": "detailing_columbus_medium_001", "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "Historical scores are preserved and are not current-sendability compatible."},
    {"batch": "epoxy_cleveland_medium_001", "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "Historical scores are preserved and are not current-sendability compatible."},
    {"batch": "water_restoration_cleveland_medium_002", "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "Historical scores are preserved and are not current-sendability compatible."},
    {"batch": "water_restoration_columbus_medium_002", "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "Historical scores are preserved and are not current-sendability compatible."}
  ]
}
```

- [ ] **Step 4: Centralize current scored-row validation**

Move the title/snake alias table now duplicated in `refresh_packet_tiers.py` into `lead_machine/score_schema.py`, and add this public boundary:

```python
COMMERCIAL_FIELD_TITLES = {
    "commercial_materiality": "Commercial Materiality",
    "recurring_consequence": "Recurring Consequence",
    "root_cause_confidence": "Root Cause Confidence",
    "offer_component": "LPM Offer Component",
    "packageable_solution": "Packageable Solution",
    "value_mechanism": "Value Mechanism",
    "beyond_free_fix": "Beyond Free Fix",
    "self_fix_risk": "Self Fix Risk",
    "intentionality": "Intentionality",
}

CSV_SCORE_ALIASES = {
    "score_schema_version": ("Score Schema Version", "score_schema_version"),
    "commercial_fit_state": ("Commercial Fit State", "commercial_fit_state"),
    **{key: (title, key) for key, title in COMMERCIAL_FIELD_TITLES.items()},
    "contact_eligibility": ("Contact Eligibility", "contact_eligibility"),
    "asset_state": ("Asset State", "asset_state"),
    "promise_state": ("Promise State", "promise_state"),
    "asset_id": ("Asset ID", "asset_id"),
    "asset_verifier_verdict": ("Asset Verifier Verdict", "asset_verifier_verdict"),
}


def validate_current_scored_row(row: Dict[str, Any]) -> list[str]:
    errors: list[str] = []
    version = _alias_value(row, CSV_SCORE_ALIASES["score_schema_version"])
    if version != str(SCORE_SCHEMA_VERSION):
        errors.append(f"score_schema_version must be {SCORE_SCHEMA_VERSION}")
    for key, aliases in CSV_SCORE_ALIASES.items():
        values = {_semantic(value) for alias in aliases if (value := row.get(alias)) not in (None, "")}
        if len(values) > 1:
            errors.append(f"conflicting aliases {key}")
    state = _alias_value(row, CSV_SCORE_ALIASES["commercial_fit_state"]).casefold()
    contact = _alias_value(row, CSV_SCORE_ALIASES["contact_eligibility"]).casefold()
    asset = _alias_value(row, CSV_SCORE_ALIASES["asset_state"]).casefold()
    promise = _alias_value(row, CSV_SCORE_ALIASES["promise_state"]).casefold()
    if state not in COMMERCIAL_FIT_STATES:
        errors.append("invalid commercial_fit_state")
    if contact not in CONTACT_ELIGIBILITY_STATES:
        errors.append("invalid contact_eligibility")
    if asset not in ASSET_STATES:
        errors.append("invalid asset_state")
    if promise not in PROMISE_STATES:
        errors.append("invalid promise_state")
    if state == "commercially-sendable":
        errors.extend(f"missing value {key}" for key in COMMERCIAL_DIMENSION_KEYS if not _alias_value(row, CSV_SCORE_ALIASES[key]))
    if contact == "contact-now" and state != "commercially-sendable":
        errors.append("contact-now requires commercially-sendable")
    if contact == "contact-after-asset" and asset != "planned":
        errors.append("contact-after-asset requires planned asset_state")
    if promise == "future-build" and asset != "planned":
        errors.append("future-build requires planned asset_state")
    asset_id = _alias_value(row, CSV_SCORE_ALIASES["asset_id"])
    verdict = _alias_value(row, CSV_SCORE_ALIASES["asset_verifier_verdict"])
    if asset == "verified-existing" and (not asset_id or verdict != "PASS"):
        errors.append("verified-existing requires asset_id and PASS verifier")
    if promise == "existing-verified-asset" and asset != "verified-existing":
        errors.append("existing-verified-asset requires verified-existing asset_state")
    return errors
```

- [ ] **Step 5: Refactor the repository response without changing artifacts**

In `DashboardRepository._read_batch()`:

```python
legacy_entry = self._legacy_batches.get(batch)
lifecycle = "legacy" if legacy_entry else "active"
reconciliation = reconcile_rows(enriched_rows, scored_rows)
scoring_complete = bool(enriched_count) and reconciliation.status == "reconciled" and reconciliation.unmatched_enriched == 0
current_errors = [error for row in scored_rows for error in validate_current_scored_row(row)]
current_compatible = bool(scored_rows) and not current_errors
current_sendable = sum(1 for row in effective_tiered_rows if is_contact_ready(row)) if lifecycle == "active" and current_compatible else None
historical_tiers = _tier_counts(effective_tiered_rows) if lifecycle == "legacy" else None
```

Load the registry in `DashboardRepository.__init__()`, include the registry file's stat in `_signature()`, and add `reconciliation: Reconciliation` to `BatchRead`. Remove the old enriched/scored identity-intersection count block. Translate a reconciliation `unknown` result into one fail-visible `identity_reconciliation_unknown` issue instead of emitting a guessed unscored count.

Set the response values exactly as follows:

```python
"lifecycle": lifecycle,
"legacy": None if legacy_entry is None else {
    "legacy_since": legacy_entry.legacy_since,
    "reason": legacy_entry.reason,
    "note": legacy_entry.note,
},
"counts": {
    "raw": len(raw_rows),
    "deduped": len(deduped_rows),
    "enriched": enriched_count,
    "scored": raw_scored_count,
    "unscored": reconciliation.unmatched_enriched if reconciliation.status == "reconciled" else None,
    "packets": len(packet_rows),
    "sendable": current_sendable,
    "manual_review": tiers["manual_review"] if lifecycle == "active" and current_compatible else None,
    "watchlist": tiers["watchlist"] if lifecycle == "active" and current_compatible else None,
    "no_packet": tiers["no_packet"] if lifecycle == "active" and current_compatible else None,
},
"reconciliation": reconciliation.as_dict(),
"current_sendability": {
    "status": "evaluated" if lifecycle == "active" and current_compatible else "incompatible" if scored_rows else "not_scored",
    "count": current_sendable,
},
"historical_packets": None if historical_tiers is None else historical_tiers,
```

For legacy batches, set `next_action` to `Legacy batch — preserved history`, skip current readiness mismatch issues, and do not mark packet/live receipt drift as active attention. For active scored batches with `current_errors`, add one blocked `incompatible_current_score_schema` issue and do not evaluate readiness.

Integrate the existing uncommitted custom receipt reader, but expose a top-level evidence state:

```python
"evidence": {
    "status": "verified" if live_current else "partial" if latest_custom else "unknown",
    "timestamp": str(state.get("last_live_sync_timestamp") or latest_custom.get("recorded_at") or ""),
    "records": live_post_count + live_patch_count if live_current else custom_count if latest_custom else None,
},
```

- [ ] **Step 6: Update lead joins to use reconciliation pairs**

Replace `_row_key` dictionary joins in `DashboardRepository.leads()` with `read.reconciliation.scored_by_enriched_index`. Store the `Reconciliation` object in `BatchRead`, and for legacy rows set `sendable` to `None`, `readiness_mismatch` to `False`, and add `sendability_state: "historical"`. Active current rows retain objective `is_contact_ready()` evaluation.

- [ ] **Step 7: Run focused backend tests**

Run: `.venv/bin/python -m unittest tests.test_dashboard tests.test_dashboard_identity -v`

Expected: all tests pass; the old equal-count identity mismatch assertion is updated to expect literal `scored == 2`, `unscored is None`, and reconciliation `unknown`.

- [ ] **Step 8: Commit registry and backend contract**

```bash
git add lead_machine/dashboard/legacy.py lead_machine/dashboard/legacy_batches.json lead_machine/dashboard/data.py lead_machine/score_schema.py lead_machine/hermes_batch.py lead_machine/airtable_receipts.py tests/test_dashboard.py
git diff --cached --check
git commit -m "feat: classify legacy batches and report literal counts"
```

---

### Task 3: Active-Only Totals, Lifecycle API, and All-Batch Audit

**Files:**
- Modify: `lead_machine/dashboard/data.py:924-1151`
- Modify: `lead_machine/dashboard/api.py:71-95`
- Create: `audit_dashboard_batches.py`
- Create: `tests/test_dashboard_audit.py`
- Modify: `tests/test_dashboard.py:395-414`

**Interfaces:**
- Consumes: Task 2 batch response contract.
- Produces: `GET /api/batches?lifecycle=all|active|legacy`; overview `inventory_totals`; `audit_repository(root) -> dict[str, Any]`.

- [ ] **Step 1: Write failing lifecycle and active-total tests**

```python
def test_overview_excludes_legacy_from_operational_totals(self) -> None:
    active = self.repo.batch(BATCH, force=True)
    assert active is not None
    legacy_name = "detailing_columbus_medium_001"
    (self.root / "market_profiles/columbus.json").write_text("{}\n", encoding="utf-8")
    (self.root / f"search_configs/{legacy_name}.json").write_text(
        json.dumps({"batch": legacy_name, "vertical": "detailing", "location": "Columbus OH", "queries": ["ceramic coating"]}),
        encoding="utf-8",
    )
    registry_path = self.root / "lead_machine/dashboard/legacy_batches.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({"schema_version": 1, "batches": [{"batch": legacy_name, "legacy_since": "2026-07-20", "reason": "pre_commercial_readiness_schema", "note": "History"}]}), encoding="utf-8")
    overview = DashboardRepository(self.root).overview(force=True)
    self.assertEqual(overview["inventory_totals"], {"all": 2, "active": 1, "legacy": 1})
    self.assertEqual(overview["totals"]["batches"], 1)
    self.assertEqual(DashboardRepository(self.root).batches(lifecycle="legacy", force=True)["total"], 1)
```

- [ ] **Step 2: Run the test and confirm lifecycle filtering is absent**

Run: `.venv/bin/python -m unittest tests.test_dashboard.DashboardRepositoryTest.test_overview_excludes_legacy_from_operational_totals -v`

Expected: failure because `inventory_totals` and the `lifecycle` parameter do not exist.

- [ ] **Step 3: Add lifecycle filtering and active-only overview aggregation**

Add `lifecycle: Literal["all", "active", "legacy"] = "all"` to `DashboardRepository.batches()`, `_filtered()`, and the FastAPI query. In `overview()`, derive both inventories:

```python
all_items = self._filtered(reads, vertical=vertical, market=market, lifecycle="all")
items = [item for item in all_items if item["lifecycle"] == "active"]
inventory_totals = {
    "all": len(all_items),
    "active": len(items),
    "legacy": sum(item["lifecycle"] == "legacy" for item in all_items),
}
totals = {
    "batches": len(items),
    "needs_attention": sum(item["health"] != "healthy" for item in items),
    "unscored": sum(item["counts"]["unscored"] or 0 for item in items),
    "unknown_unscored_batches": sum(item["counts"]["unscored"] is None for item in items),
    "sendable": sum(item["counts"]["sendable"] or 0 for item in items),
    "airtable_ready": sum(item["progress"] == "airtable_ready" for item in items),
    "finished": sum(item["progress"] == "finished" for item in items),
}
```

Build `attention` only from active `items`; include `lifecycles: ["active", "legacy"]` in filter metadata. Sorting must use `-(item["counts"]["unscored"] or 0)`.

- [ ] **Step 4: Write the failing all-batch audit test**

```python
from audit_dashboard_batches import audit_repository
from tests.test_dashboard import make_root


def make_fixture_repo(root: Path) -> None:
    make_root(root)


def test_audit_accepts_explicit_unknown_but_rejects_literal_count_drift(tmp_path: Path) -> None:
    make_fixture_repo(tmp_path)
    clean = audit_repository(tmp_path)
    assert clean["discrepancies"] == []
    assert clean["batch_count"] == clean["lifecycle_counts"]["active"] + clean["lifecycle_counts"]["legacy"]
```

- [ ] **Step 5: Implement the audit command**

```python
def audit_repository(root: Path) -> dict[str, Any]:
    repo = DashboardRepository(root)
    response = repo.batches(lifecycle="all", force=True)
    discrepancies: list[dict[str, str]] = []
    unknowns: list[dict[str, str]] = []
    lifecycle_counts = {"active": 0, "legacy": 0}
    for batch in response["items"]:
        lifecycle_counts[batch["lifecycle"]] += 1
        for key in ("raw", "deduped", "enriched", "scored", "packets"):
            artifact = batch["artifacts"].get(key)
            if artifact and artifact["rows"] is not None and batch["counts"][key] != artifact["rows"]:
                discrepancies.append({"batch": batch["batch"], "field": key, "message": "summary count differs from artifact rows"})
        if batch["reconciliation"]["status"] == "unknown":
            unknowns.append({"batch": batch["batch"], "field": "reconciliation", "message": "explicitly unresolved"})
        if batch["lifecycle"] == "active" and batch["counts"]["scored"] and batch["current_sendability"]["status"] != "evaluated":
            discrepancies.append({"batch": batch["batch"], "field": "current_sendability", "message": "active scored batch is not current-schema compatible"})
    return {
        "batch_count": response["total"],
        "lifecycle_counts": lifecycle_counts,
        "unknowns": unknowns,
        "discrepancies": discrepancies,
        "ok": not discrepancies,
    }
```

The CLI accepts `--root` and `--output`, writes indented JSON when requested, prints the same JSON to stdout, and exits `0` only when `ok` is true.

- [ ] **Step 6: Run backend/API/audit tests**

Run: `.venv/bin/python -m unittest tests.test_dashboard tests.test_dashboard_audit -v`

Expected: all tests pass, including `GET /api/batches?lifecycle=legacy` returning HTTP 200 and an invalid lifecycle returning HTTP 422.

- [ ] **Step 7: Commit lifecycle aggregation and audit**

```bash
git add lead_machine/dashboard/data.py lead_machine/dashboard/api.py audit_dashboard_batches.py tests/test_dashboard.py tests/test_dashboard_audit.py
git diff --cached --check
git commit -m "feat: add lifecycle filters and batch accuracy audit"
```

---

### Task 4: Dashboard Lifecycle and Accuracy UX

**Files:**
- Modify: `dashboard-ui/src/types.ts:1-117`
- Modify: `dashboard-ui/src/App.tsx:1-304`
- Modify: `dashboard-ui/src/App.test.tsx:1-160`
- Modify: `dashboard-ui/src/styles.css`

**Interfaces:**
- Consumes: Task 2/3 API response.
- Produces: All/Active/Legacy lifecycle filter, explicit lifecycle badges, nullable-value rendering, current versus historical packet labels, and Airtable evidence labels.

- [ ] **Step 1: Update the test fixtures to the new API contract**

Add to the active fixture:

```typescript
lifecycle: "active",
legacy: null,
reconciliation: { status: "reconciled", matched: 11, unmatched_enriched: 7, unmatched_scored: 0, ambiguous: 0 },
current_sendability: { status: "evaluated", count: 2 },
historical_packets: null,
```

Add `inventory_totals: { all: 2, active: 1, legacy: 1 }` and `unknown_unscored_batches: 0` to the overview fixture. Add `airtable.evidence: { status: "unknown", timestamp: "", records: null }`.

- [ ] **Step 2: Write failing lifecycle rendering tests**

```typescript
it("shows legacy history without calling it current sendable", async () => {
  const legacy = {
    ...batch,
    batch: "detailing_columbus_medium_001",
    lifecycle: "legacy",
    legacy: { legacy_since: "2026-07-20", reason: "pre_commercial_readiness_schema", note: "Preserved history" },
    counts: { ...batch.counts, scored: 140, unscored: null, sendable: null, manual_review: null },
    reconciliation: { status: "unknown", matched: 0, unmatched_enriched: null, unmatched_scored: 140, ambiguous: 0 },
    current_sendability: { status: "incompatible", count: null },
    historical_packets: { sendable: 16, manual_review: 28, watchlist: 0, no_packet: 96 },
  };
  mockDashboard([legacy]);
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(await screen.findByText("Legacy")).toBeInTheDocument();
  expect(screen.getByText("16 historical packet-ready")).toBeInTheDocument();
  expect(screen.getByText("Unknown", { selector: ".unscored-value" })).toBeInTheDocument();
  expect(screen.queryByText("16 sendable")).not.toBeInTheDocument();
});

it("sends the selected lifecycle to the batch API", async () => {
  mockDashboard([batch]);
  render(<MemoryRouter><App /></MemoryRouter>);
  fireEvent.change(await screen.findByLabelText("Lifecycle"), { target: { value: "legacy" } });
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("lifecycle=legacy"), expect.anything());
});
```

Define the test helper in `App.test.tsx` so both tests use the same response boundary:

```typescript
function mockDashboard(items: Array<{ lifecycle: string } & Record<string, unknown>>) {
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(response(
    url.startsWith("/api/overview")
      ? { ...overview, inventory_totals: { all: items.length, active: items.filter((item) => item.lifecycle === "active").length, legacy: items.filter((item) => item.lifecycle === "legacy").length } }
      : { generated_at: overview.generated_at, total: items.length, items },
  ))));
}
```

- [ ] **Step 3: Run frontend tests and confirm missing UI semantics**

Run: `npm test` in `dashboard-ui/`.

Expected: failures for missing lifecycle filter/badge and historical/unknown copy.

- [ ] **Step 4: Update TypeScript types exactly**

```typescript
export type Lifecycle = "active" | "legacy";
export type EvidenceStatus = "verified" | "partial" | "unknown";

// Within BatchSummary
lifecycle: Lifecycle;
legacy: null | { legacy_since: string; reason: string; note: string };
counts: {
  raw: number; deduped: number; enriched: number; scored: number;
  unscored: number | null; packets: number; sendable: number | null;
  manual_review: number | null; watchlist: number | null; no_packet: number | null;
};
reconciliation: { status: "reconciled" | "unknown"; matched: number; unmatched_enriched: number | null; unmatched_scored: number; ambiguous: number };
current_sendability: { status: "evaluated" | "incompatible" | "not_scored"; count: number | null };
historical_packets: null | { sendable: number; manual_review: number; watchlist: number; no_packet: number };

// Within LeadRow
sendable: boolean | null;
sendability_state: "current" | "historical" | "not_scored";
```

- [ ] **Step 5: Render lifecycle and nullable values explicitly**

Use one helper for all nullable counts:

```typescript
function countText(value: number | null) {
  return value === null ? "Unknown" : formatNumber(value);
}
```

Add `lifecycle: "all"` to `Filters`, a `Lifecycle` select with All/Active/Legacy options, and pass non-all values as the `lifecycle` query. In `BatchTable`, render:

```tsx
<span className={`lifecycle-badge lifecycle-${batch.lifecycle}`}>{batch.lifecycle === "legacy" ? "Legacy" : "Active"}</span>
<span className="unscored-value">{countText(batch.counts.unscored)}</span>
{batch.lifecycle === "legacy" && batch.historical_packets
  ? <span className="packet-mix historical"><b>{batch.historical_packets.sendable}</b> historical packet-ready <i /> <b>{batch.historical_packets.manual_review}</b> historical manual review</span>
  : <span className="packet-mix"><b>{countText(batch.counts.sendable)}</b> current sendable <i /> <b>{countText(batch.counts.manual_review)}</b> manual</span>}
<span className={`receipt-status receipt-${batch.airtable.evidence.status}`}>{humanize(batch.airtable.evidence.status)}</span>
```

The detail header uses `Unknown waiting` when unscored is null, displays the legacy note, and labels the lead table sendability column `Historical state` for a legacy batch. The metric strip labels batch totals `Active batches` and displays `inventory_totals.all` in the inventory header.

- [ ] **Step 6: Add restrained styles and run frontend verification**

Add `.lifecycle-badge`, `.lifecycle-legacy`, `.packet-mix.historical`, `.unscored-value`, and `.legacy-note` rules using the existing palette and type scale. Do not add gradients or motion.

Run: `npm test && npm run build` in `dashboard-ui/`.

Expected: Vitest passes and Vite produces `dashboard-ui/dist` successfully.

- [ ] **Step 7: Commit the UI contract**

```bash
git add dashboard-ui/src/types.ts dashboard-ui/src/App.tsx dashboard-ui/src/App.test.tsx dashboard-ui/src/styles.css
git diff --cached --check
git commit -m "feat: distinguish active and legacy dashboard batches"
```

---

### Task 5: Strict Current Scoring Cutover

**Files:**
- Modify: `lead_machine/manual_scores.py:1-23,79-231,334-577`
- Modify: `import_llm_score_bundle.py:15-110`
- Modify: `refresh_packet_tiers.py:15-265`
- Modify: `tests/test_lead_machine_contracts.py:4098-4198,5146-5180`
- Modify: `tests/test_readiness_migration.py`
- Modify: `tests/test_scoring_v3_operations.py:1-20,224-231`
- Delete: `lead_machine/commercial_backfill.py`
- Delete: `annotate_legacy_scoring.py`

**Interfaces:**
- Consumes: `validate_current_scored_row()` and alias constants from Task 2.
- Produces: schema-v2-only manual score validation and packet-tier refresh; old score inputs fail before any write.

- [ ] **Step 1: Replace legacy-acceptance tests with strict rejection tests**

```python
def test_manual_score_loader_rejects_versionless_score(self) -> None:
    raw_score = self._manual_score()
    raw_score.pop("score_schema_version", None)
    with tempfile.TemporaryDirectory() as tmp:
        score_path = Path(tmp) / "score.json"
        score_path.write_text(json.dumps(raw_score), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "score_schema_version must be 2"):
            load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})


def test_refresh_refuses_legacy_without_writing(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "legacy.csv"
        destination = Path(tmp) / "tiered.csv"
        write_rows(source, [{"Place Key": "old", "Sendable": "Yes", "Packet Eligible": "Yes"}])
        destination.write_text("sentinel\n", encoding="utf-8")
        before = destination.read_bytes()
        with self.assertRaisesRegex(SchemaMigrationError, "current schema v2 required"):
            refresh_csv(source, destination)
        self.assertEqual(destination.read_bytes(), before)
```

Delete tests that assert deterministic backfill values; retain current-v2 validation, alias-conflict, and sendability tests.

- [ ] **Step 2: Run focused tests and confirm legacy inputs still pass/backfill**

Run: `.venv/bin/python -m unittest tests.test_readiness_migration tests.test_lead_machine_contracts.LeadMachineContractsTest.test_manual_score_loader_rejects_versionless_score -v`

Expected: failure because versionless manual scores and legacy CSVs are currently accepted.

- [ ] **Step 3: Make manual score validation unconditionally schema-v2**

In `validate_manual_score()`, remove `require_schema_v2` and all `_legacy_score_compatibility` behavior:

```python
schema_value = data.get("score_schema_version")
if type(schema_value) is not int or schema_value != SCORE_SCHEMA_VERSION:
    raise ValueError(f"score_schema_version must be {SCORE_SCHEMA_VERSION}")
_require_keys(data, V2_SCORE_FIELDS)
```

Remove the `commercial_backfill` import. Always set `Commercial Fit Source`/`commercial_fit_source` to `llm-schema-v2`, remove the conditional backfill at the end of `merge_manual_score()`, and keep `apply_sendability()` as the only gate.

In `import_llm_score_bundle.py`, remove the deleted `require_schema_v2=True` keyword from its `validate_manual_score()` call; the validator is now strict for every caller.

- [ ] **Step 4: Make packet refresh current-schema-only**

Remove the backfill import and branch from `refresh_packet_tiers.py`. `refresh_row()` must enforce:

```python
schema_class, errors = classify_schema(row)
identity = row_identity(row) or "<missing-identity>"
if schema_class != "v2" or errors:
    detail = "; ".join(errors) if errors else f"found {schema_class}"
    raise SchemaMigrationError(f"{identity}: current schema v2 required: {detail}")
migrated = _normalize_contract_aliases(row, source=_first(row, ("Commercial Fit Source", "commercial_fit_source")) or "llm-schema-v2")
```

`impact_report()` keeps reporting `legacy`, `partial_v2`, `complete_unversioned_v2`, and `invalid_v2`, but sets `safe_to_write` false unless every row is `v2`, identities are complete/unique, and no previously sendable current identity is removed.

- [ ] **Step 5: Remove obsolete code and annotations**

Delete `lead_machine/commercial_backfill.py` and `annotate_legacy_scoring.py`. Remove `LegacyAnnotationTest` and its import from `tests/test_scoring_v3_operations.py`. Keep the current `Legacy Scoring: No` output in scoring v3 until a separate schema-field cleanup proves no consumer depends on that non-Airtable diagnostic field.

- [ ] **Step 6: Run scoring and pipeline contract suites**

Run:

```bash
.venv/bin/python -m unittest tests.test_readiness_migration tests.test_scoring_v3 tests.test_scoring_v3_operations tests.test_lead_machine_contracts -v
```

Expected: all tests pass; versionless imports and legacy tier refreshes refuse without modifying destinations.

- [ ] **Step 7: Commit strict scoring cutover**

```bash
git add lead_machine/manual_scores.py lead_machine/score_schema.py import_llm_score_bundle.py refresh_packet_tiers.py tests/test_lead_machine_contracts.py tests/test_readiness_migration.py tests/test_scoring_v3_operations.py
git add -u lead_machine/commercial_backfill.py annotate_legacy_scoring.py
git diff --cached --check
git commit -m "refactor: require current scoring schema"
```

---

### Task 6: Airtable Legacy-Field Backup and Verification Tool

**Files:**
- Create: `airtable_schema_cutover.py`
- Create: `tests/test_airtable_schema_cutover.py`
- Modify: `tests/test_upload_airtable_schema.py`

**Interfaces:**
- Consumes: `AIRTABLE_BASE_ID`, `AIRTABLE_PROSPECTS_TABLE`, and `AIRTABLE_PAT` or equivalent CLI flags.
- Produces: `prepare_cutover(schema, records) -> dict`; `verify_cutover(snapshot, schema, record_count) -> list[str]`; ignored backup/report/checklist under an explicit `run_state/airtable_schema_cutover/<timestamp>/` directory.

- [ ] **Step 1: Write failing pure redundancy and preservation tests**

```python
from airtable_schema_cutover import LEGACY_FIELD_PAIRS, prepare_cutover, verify_cutover


def schema_with_all_pairs() -> dict:
    names = ["Place Key", *LEGACY_FIELD_PAIRS, *LEGACY_FIELD_PAIRS.values()]
    return {"fields": [{"id": f"fld{index}", "name": name} for index, name in enumerate(dict.fromkeys(names))]}


def schema_without_legacy_fields() -> dict:
    names = ["Place Key", *LEGACY_FIELD_PAIRS.values()]
    return {"fields": [{"id": f"fld{index}", "name": name} for index, name in enumerate(dict.fromkeys(names))]}


def test_prepare_refuses_legacy_only_or_conflicting_values() -> None:
    schema = schema_with_all_pairs()
    records = [{"id": "rec1", "fields": {"Place Key": "p1", "Legacy Send Trigger": "old", "Send Trigger": ""}}]
    report = prepare_cutover(schema, records)
    assert report["safe_to_delete"] is False
    assert report["legacy_only"]["Legacy Send Trigger"] == ["rec1"]


def test_last_contacted_iso_timestamp_equals_same_date() -> None:
    schema = schema_with_all_pairs()
    records = [{"id": "rec1", "fields": {"Legacy Last Contacted": "2026-06-24T17:27:00.000Z", "Last Contacted": "2026-06-24"}}]
    assert prepare_cutover(schema, records)["safe_to_delete"] is True


def test_verify_requires_same_record_count_and_exact_field_absence() -> None:
    snapshot = {"record_count": 132}
    errors = verify_cutover(snapshot, schema_without_legacy_fields(), 131)
    assert "record count changed: expected 132, found 131" in errors
```

- [ ] **Step 2: Run tests and confirm the tool is missing**

Run: `.venv/bin/python -m unittest tests.test_airtable_schema_cutover -v`

Expected: import failure for `airtable_schema_cutover`.

- [ ] **Step 3: Implement exact field pairs and semantic comparison**

```python
LEGACY_FIELD_PAIRS = {
    "Legacy Current Website Quality": "Current Website Quality",
    "Legacy Send Trigger": "Send Trigger",
    "Legacy Last Contacted": "Last Contacted",
    "Legacy Recommended Action": "Recommended Action",
    "Legacy Form Strength": "Form Strength",
    "Legacy CTA Strength": "CTA Strength",
    "Legacy Skip Reason": "Skip Reason",
}


def schema_field_names(schema: dict[str, Any]) -> set[str]:
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise ValueError("table schema must contain a fields list")
    return {str(field.get("name") or "") for field in fields if isinstance(field, dict) and field.get("name")}


def comparable(field: str, value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:10] if field == "Legacy Last Contacted" and len(text) >= 10 else text


def prepare_cutover(schema: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    names = schema_field_names(schema)
    missing_legacy = sorted(set(LEGACY_FIELD_PAIRS) - names)
    missing_canonical = sorted(set(LEGACY_FIELD_PAIRS.values()) - names)
    legacy_only = {field: [] for field in LEGACY_FIELD_PAIRS}
    conflicts = {field: [] for field in LEGACY_FIELD_PAIRS}
    for record in records:
        fields = record.get("fields") or {}
        for legacy, current in LEGACY_FIELD_PAIRS.items():
            old = comparable(legacy, fields.get(legacy))
            new = comparable(legacy, fields.get(current))
            if old and not new:
                legacy_only[legacy].append(record["id"])
            elif old and new and old != new:
                conflicts[legacy].append(record["id"])
    legacy_only = {key: value for key, value in legacy_only.items() if value}
    conflicts = {key: value for key, value in conflicts.items() if value}
    return {
        "record_count": len(records),
        "missing_legacy_fields": missing_legacy,
        "missing_canonical_fields": missing_canonical,
        "legacy_only": legacy_only,
        "conflicts": conflicts,
        "safe_to_delete": not any((missing_legacy, missing_canonical, legacy_only, conflicts)),
    }
```

- [ ] **Step 4: Implement paginated read-only Airtable access and atomic output**

Use `requests.get` only. Read schema from `/v0/meta/bases/{base_id}/tables`, find the exact table by ID or name, and read all records from `/v0/{base_id}/{quoted_table}` with pagination. `prepare` writes:

```python
def get_json(url: str, token: str, *, params: list[tuple[str, Any]] | None = None) -> dict[str, Any]:
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params or [], timeout=30)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("Airtable response must be an object")
    return value


def fetch_table_schema(base_id: str, table: str, token: str) -> dict[str, Any]:
    value = get_json(f"https://api.airtable.com/v0/meta/bases/{quote(base_id)}/tables", token)
    matches = [item for item in value.get("tables", []) if item.get("id") == table or item.get("name") == table]
    if len(matches) != 1:
        raise ValueError(f"expected one Airtable table named/id {table!r}, found {len(matches)}")
    return matches[0]


def fetch_records(base_id: str, table: str, token: str, fields: list[str] | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = ""
    while True:
        params = [("pageSize", 100), *[("fields[]", field) for field in (fields or [])]]
        if offset:
            params.append(("offset", offset))
        page = get_json(f"https://api.airtable.com/v0/{quote(base_id)}/{quote(table, safe='')}", token, params=params)
        records.extend(item for item in page.get("records", []) if isinstance(item, dict))
        offset = str(page.get("offset") or "")
        if not offset:
            return records
```

- `backup.json`: record ID, Place Key, and all fourteen legacy/current paired values;
- `pre_cutover.json`: base ID, table ID/name, record count, field names/IDs, redundancy report, backup SHA-256, and timestamp;
- `delete_checklist.md`: the exact seven names and the command to run `verify`.

Write each file to a sibling temporary file and replace the destination only after serialization succeeds. Return exit `2` without a checklist if `safe_to_delete` is false. Never issue POST, PATCH, PUT, or DELETE.

Use this exact atomic writer and backup shape:

```python
def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def backup_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    names = ["Place Key", *LEGACY_FIELD_PAIRS, *LEGACY_FIELD_PAIRS.values()]
    return {
        "records": [
            {"record_id": record["id"], **{name: (record.get("fields") or {}).get(name, "") for name in names}}
            for record in records
        ]
    }
```

- [ ] **Step 5: Implement post-delete verification**

```python
def verify_cutover(snapshot: dict[str, Any], schema: dict[str, Any], record_count: int) -> list[str]:
    names = schema_field_names(schema)
    errors = []
    if record_count != snapshot["record_count"]:
        errors.append(f"record count changed: expected {snapshot['record_count']}, found {record_count}")
    remaining = sorted(set(LEGACY_FIELD_PAIRS) & names)
    if remaining:
        errors.append("legacy fields still present: " + ", ".join(remaining))
    missing_current = sorted(set(LEGACY_FIELD_PAIRS.values()) - names)
    if missing_current:
        errors.append("canonical fields missing: " + ", ".join(missing_current))
    return errors
```

The `verify` command loads `pre_cutover.json`, fetches current schema and record count, prints a JSON verdict, exits `0` only when errors is empty, and performs no write.

- [ ] **Step 6: Add active-schema regression assertions**

In `tests/test_upload_airtable_schema.py`, assert all seven names are absent from `_FIELD_SCHEMA`, `CSV_AIRTABLE_FIELDS`, and `AIRTABLE_ALLOWED_FIELDS`, while every canonical replacement remains present.

- [ ] **Step 7: Run Airtable tests**

Run: `.venv/bin/python -m unittest tests.test_airtable_schema_cutover tests.test_upload_airtable_schema tests.test_lead_machine_contracts -v`

Expected: all tests pass and mocked request assertions prove only GET is used.

- [ ] **Step 8: Commit the read-only cutover tooling**

```bash
git add airtable_schema_cutover.py tests/test_airtable_schema_cutover.py tests/test_upload_airtable_schema.py
git diff --cached --check
git commit -m "feat: verify Airtable legacy field cutover"
```

---

### Task 7: Documentation and Full Local Verification

**Files:**
- Modify: `docs/dashboard.md`
- Modify: `docs/airtable_schema_required.md`

**Interfaces:**
- Consumes: completed local implementation.
- Produces: operator documentation and a locally verified all-batch audit report.

- [ ] **Step 1: Update dashboard documentation with exact semantics**

Document:

- all canonical batches are visible via All/Active/Legacy;
- operational totals and attention are active-only;
- stage counts are literal artifact rows;
- unscored is nullable and requires successful stable-identity reconciliation;
- current sendable uses the four-field gate;
- legacy packet counts are historical;
- Airtable evidence is Verified/Partial/Unknown;
- the dashboard does not execute commands or mutate Airtable.

- [ ] **Step 2: Regenerate the Airtable schema document**

Run:

```bash
.venv/bin/python upload_airtable.py --schema-report --schema-report-output docs/airtable_schema_required.md
```

Expected: the file contains canonical fields and none of the seven `Legacy ...` field names.

- [ ] **Step 3: Run the complete backend suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: all discovered tests pass; the command discovers more than zero tests.

- [ ] **Step 4: Run frontend and static verification**

Run:

```bash
cd dashboard-ui && npm test && npm run build
cd .. && .venv/bin/python -m compileall -q lead_machine *.py
git diff --check
```

Expected: Vitest passes, build passes, compilation emits no errors, and diff check is clean.

- [ ] **Step 5: Run the real all-batch audit**

Run:

```bash
.venv/bin/python audit_dashboard_batches.py --root . --output reports/dashboard_batch_audit_2026-07-20.json
```

Expected: exit `0`, `batch_count: 28`, lifecycle counts sum to 28, the five named scored batches are legacy, `plumbing_cleveland_medium_001` is active/current, and `discrepancies` is empty. Explicit reconciliation unknowns are allowed only when named in `unknowns`.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/dashboard.md docs/airtable_schema_required.md
git diff --cached --check
git commit -m "docs: explain dashboard accuracy and legacy lifecycle"
```

---

### Task 8: Live Airtable Cutover and End-to-End Proof

**Files:**
- Generated and ignored: `run_state/airtable_schema_cutover/2026-07-20/backup.json`
- Generated and ignored: `run_state/airtable_schema_cutover/2026-07-20/pre_cutover.json`
- Generated and ignored: `run_state/airtable_schema_cutover/2026-07-20/delete_checklist.md`
- Generated and ignored: `reports/dashboard_batch_audit_2026-07-20.json`

**Interfaces:**
- Consumes: Task 6 tool and Task 7 verified implementation.
- Produces: live pre/post proof that seven fields were removed and 132 lead records were preserved, plus dashboard API evidence.

- [ ] **Step 1: Prepare the live cutover snapshot**

Run with approved network access:

```bash
.venv/bin/python airtable_schema_cutover.py prepare --output-dir run_state/airtable_schema_cutover/2026-07-20
```

Expected: `safe_to_delete: true`, `record_count: 132`, empty `legacy_only`, empty `conflicts`, and an exact seven-field checklist. Stop if any expected value differs.

- [ ] **Step 2: Inspect the generated backup before deletion**

Run:

```bash
.venv/bin/python -c 'import json; from pathlib import Path; p=Path("run_state/airtable_schema_cutover/2026-07-20"); b=json.loads((p/"backup.json").read_text()); r=json.loads((p/"pre_cutover.json").read_text()); print({"backup_records": len(b["records"]), "record_count": r["record_count"], "safe_to_delete": r["redundancy"]["safe_to_delete"], "sha256": r["backup_sha256"]})'
```

Expected: backup records and snapshot record count both equal 132, safe-to-delete is true, and SHA-256 is non-empty.

- [ ] **Step 3: Pause for the user-performed Airtable UI deletion**

The user deletes only the seven names from `delete_checklist.md`. No code or API call performs this destructive schema action. Do not continue if any record or canonical field was deleted.

- [ ] **Step 4: Verify the live post-cutover schema and lead count**

Run with approved network access:

```bash
.venv/bin/python airtable_schema_cutover.py verify --snapshot run_state/airtable_schema_cutover/2026-07-20/pre_cutover.json
```

Expected: `ok: true`, `record_count: 132`, no remaining legacy fields, all seven canonical fields present, and `errors: []`.

- [ ] **Step 5: Verify real dashboard API responses**

Run:

```bash
.venv/bin/python -c 'from lead_machine.dashboard.data import DashboardRepository; r=DashboardRepository("."); o=r.overview(force=True); b=r.batches(lifecycle="all", force=True); assert b["total"] == o["inventory_totals"]["all"] == 28; assert o["inventory_totals"]["legacy"] == 5; assert all(x["lifecycle"] in {"active", "legacy"} for x in b["items"]); assert all(x["counts"]["scored"] == x["artifacts"]["scored"]["rows"] for x in b["items"]); print({"inventory": o["inventory_totals"], "active_totals": o["totals"]})'
```

Expected: assertions pass and output shows 28 total batches, 5 legacy batches, and active-only operational totals.

- [ ] **Step 6: Run final verification and inspect worktree scope**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
cd dashboard-ui && npm test && npm run build
cd .. && .venv/bin/python audit_dashboard_batches.py --root . --output reports/dashboard_batch_audit_2026-07-20.json
git status --short
```

Expected: all tests/build/audit pass. `search_configs/water_restoration_cleveland_medium_003.json` remains uncommitted and untouched unless separately requested.

---

## Completion Evidence

Report these exact artifacts and facts at handoff:

- design commit and implementation commit hashes;
- backend test count and pass result;
- frontend Vitest and build results;
- `reports/dashboard_batch_audit_2026-07-20.json` verdict;
- active/legacy/all batch counts;
- literal enriched/scored counts for `detailing_columbus_medium_001` as the regression example;
- current sendable versus historical packet-ready presentation for that batch;
- Airtable pre/post record count and seven-field absence verification;
- confirmation that no Airtable lead or historical batch artifact was deleted or rewritten.
