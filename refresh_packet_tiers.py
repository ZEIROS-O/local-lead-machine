#!/usr/bin/env python3
"""Refresh packet-tier fields in an existing scored CSV without LLM calls."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

from lead_machine.commercial_backfill import COMMERCIAL_FIELD_ALIASES, backfill_legacy_commercial_fit
from lead_machine.score_schema import (
    ASSET_STATES,
    COMMERCIAL_DIMENSION_KEYS,
    COMMERCIAL_FIT_STATES,
    CONTACT_ELIGIBILITY_STATES,
    PROMISE_STATES,
    SCORE_SCHEMA_VERSION,
)
from lead_machine.sendability import apply_sendability, is_contact_ready


PACKET_FIELDS = [
    "Packet Tier",
    "Manual Review Reason",
    "Packet Eligible",
    "Packet Eligibility Reason",
]

SCHEMA_FIELDS = [
    "Score Schema Version",
    "score_schema_version",
    "Commercial Fit Source",
    "commercial_fit_source",
]
COMMERCIAL_FIELDS = [
    "Commercial Fit State",
    "commercial_fit_state",
    *[field for pair in ((title, key) for key, title in COMMERCIAL_FIELD_ALIASES.items()) for field in pair],
    "Contact Eligibility",
    "contact_eligibility",
    "Asset State",
    "asset_state",
    "Promise State",
    "promise_state",
    "Asset ID",
    "asset_id",
    "Asset Verifier Verdict",
    "asset_verifier_verdict",
]
SENDABILITY_FIELDS = [
    "Sendable",
    "Minimum Evidence Passed",
    "Send Blockers",
    "Sendability Blockers",
]
REFRESH_FIELDS = [*SCHEMA_FIELDS, *COMMERCIAL_FIELDS, *SENDABILITY_FIELDS, *PACKET_FIELDS]

CONTRACT_ALIASES = {
    "commercial_fit_state": ("Commercial Fit State", "commercial_fit_state"),
    **{
        key: (title, key)
        for key, title in COMMERCIAL_FIELD_ALIASES.items()
    },
    "contact_eligibility": ("Contact Eligibility", "contact_eligibility"),
    "asset_state": ("Asset State", "asset_state"),
    "promise_state": ("Promise State", "promise_state"),
    "asset_id": ("Asset ID", "asset_id"),
    "asset_verifier_verdict": ("Asset Verifier Verdict", "asset_verifier_verdict"),
}
SCHEMA_METADATA_ALIASES = {
    "score_schema_version": ("Score Schema Version", "score_schema_version"),
    "commercial_fit_source": ("Commercial Fit Source", "commercial_fit_source"),
}


class SchemaMigrationError(ValueError):
    pass


def raise_csv_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def output_fieldnames(existing: Iterable[str]) -> list[str]:
    fieldnames = list(existing)
    for field in REFRESH_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    return fieldnames


def _first(row: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = row.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _semantic_alias_value(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _alias_conflict(row: dict[str, Any], aliases: tuple[str, ...]) -> bool:
    values = {
        normalized
        for alias in aliases
        if alias in row and (normalized := _semantic_alias_value(row.get(alias)))
    }
    return len(values) > 1


def row_identity(row: dict[str, Any]) -> str:
    place_key = _first(row, ("Place Key", "place_key", "Place ID", "place_id"))
    if place_key:
        return place_key
    business = _first(row, ("Business Name", "business_name")).casefold()
    city = _first(row, ("City", "city")).casefold()
    return f"fallback:{business}|{city}" if business or city else ""


def _contract_errors(row: dict[str, Any]) -> list[str]:
    missing_fields = [key for key, aliases in CONTRACT_ALIASES.items() if not any(alias in row for alias in aliases)]
    errors = [f"missing field {field}" for field in missing_fields]
    errors.extend(
        f"conflicting aliases {key}"
        for key, aliases in CONTRACT_ALIASES.items()
        if _alias_conflict(row, aliases)
    )
    state = _first(row, CONTRACT_ALIASES["commercial_fit_state"]).casefold()
    contact = _first(row, CONTRACT_ALIASES["contact_eligibility"]).casefold()
    asset = _first(row, CONTRACT_ALIASES["asset_state"]).casefold()
    promise = _first(row, CONTRACT_ALIASES["promise_state"]).casefold()
    if state not in COMMERCIAL_FIT_STATES:
        errors.append(f"invalid commercial_fit_state {state or '<missing>'}")
    if contact not in CONTACT_ELIGIBILITY_STATES:
        errors.append(f"invalid contact_eligibility {contact or '<missing>'}")
    if asset not in ASSET_STATES:
        errors.append(f"invalid asset_state {asset or '<missing>'}")
    if promise not in PROMISE_STATES:
        errors.append(f"invalid promise_state {promise or '<missing>'}")
    if contact == "contact-now" and state != "commercially-sendable":
        errors.append("contact-now requires commercially-sendable")
    if contact == "contact-after-asset" and asset != "planned":
        errors.append("contact-after-asset requires planned asset_state")
    if promise == "future-build" and asset != "planned":
        errors.append("future-build requires planned asset_state")
    asset_id = _first(row, CONTRACT_ALIASES["asset_id"])
    verifier = _first(row, CONTRACT_ALIASES["asset_verifier_verdict"])
    if asset == "verified-existing" and (not asset_id or verifier != "PASS"):
        errors.append("verified-existing requires asset_id and PASS verifier")
    if promise == "existing-verified-asset" and asset != "verified-existing":
        errors.append("existing-verified-asset requires verified-existing asset_state")
    if state == "commercially-sendable":
        for key in COMMERCIAL_DIMENSION_KEYS:
            if not _first(row, CONTRACT_ALIASES[key]):
                errors.append(f"missing value {key}")
    return errors


def classify_schema(row: dict[str, Any]) -> tuple[str, list[str]]:
    raw_version = _first(row, SCHEMA_METADATA_ALIASES["score_schema_version"])
    raw_source = _first(row, SCHEMA_METADATA_ALIASES["commercial_fit_source"])
    populated = {
        key
        for key, aliases in CONTRACT_ALIASES.items()
        if _first(row, aliases)
    }
    if not raw_version and not raw_source and not populated:
        return "legacy", []
    errors = [
        f"conflicting aliases {key}"
        for key, aliases in SCHEMA_METADATA_ALIASES.items()
        if _alias_conflict(row, aliases)
    ]
    if raw_version and raw_version != str(SCORE_SCHEMA_VERSION):
        errors.append(f"schema version must be {SCORE_SCHEMA_VERSION}, got {raw_version}")
    errors.extend(_contract_errors(row))
    if errors:
        return ("invalid_v2" if raw_version else "partial_v2"), errors
    return ("v2" if raw_version else "complete_unversioned_v2"), []


def _normalize_contract_aliases(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    updated = dict(row)
    for key, aliases in SCHEMA_METADATA_ALIASES.items():
        if _alias_conflict(updated, aliases):
            raise SchemaMigrationError(f"conflicting aliases {key}")
    updated["Score Schema Version"] = SCORE_SCHEMA_VERSION
    updated["score_schema_version"] = SCORE_SCHEMA_VERSION
    updated["Commercial Fit Source"] = source
    updated["commercial_fit_source"] = source
    for key, aliases in CONTRACT_ALIASES.items():
        if _alias_conflict(updated, aliases):
            raise SchemaMigrationError(f"conflicting aliases {key}")
        value = _first(updated, aliases)
        for alias in aliases:
            updated[alias] = value
    return updated


def refresh_row(row: dict[str, Any]) -> dict[str, Any]:
    schema_class, errors = classify_schema(row)
    identity = row_identity(row) or "<missing-identity>"
    if errors:
        raise SchemaMigrationError(f"{identity}: {schema_class}: " + "; ".join(errors))
    if schema_class == "legacy":
        migrated = backfill_legacy_commercial_fit(row)
    elif schema_class == "complete_unversioned_v2":
        source = _first(row, SCHEMA_METADATA_ALIASES["commercial_fit_source"])
        migrated = _normalize_contract_aliases(row, source=source or "unversioned-v2-contract")
    else:
        source = _first(row, ("Commercial Fit Source", "commercial_fit_source")) or "llm-schema-v2"
        migrated = _normalize_contract_aliases(row, source=source)
    refreshed = apply_sendability(migrated)
    updated = dict(row)
    for field in REFRESH_FIELDS:
        updated[field] = refreshed.get(field, "")
    return updated


def impact_report(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    classes: Counter[str] = Counter()
    errors: list[str] = []
    identities = [row_identity(row) for row in rows]
    identity_counts = Counter(identities)
    missing = [index + 1 for index, identity in enumerate(identities) if not identity]
    duplicates = sorted(identity for identity, count in identity_counts.items() if identity and count > 1)
    refreshed_rows: list[dict[str, Any]] = []
    for row in rows:
        schema_class, row_errors = classify_schema(row)
        classes[schema_class] += 1
        if row_errors:
            errors.append(f"{row_identity(row) or '<missing-identity>'}: " + "; ".join(row_errors))
            continue
        refreshed_rows.append(refresh_row(row))
    def previously_sendable(row: dict[str, Any]) -> bool:
        sendable = str(row.get("Sendable") or row.get("sendable") or "").strip().casefold() == "yes"
        packet = str(row.get("Packet Eligible") or row.get("packet_eligible") or "").strip().casefold() == "yes"
        schema_class, _ = classify_schema(row)
        return is_contact_ready(row) if schema_class in {"v2", "complete_unversioned_v2"} else sendable and packet

    before = sorted(row_identity(row) for row in rows if previously_sendable(row) and row_identity(row))
    after = sorted(row_identity(row) for row in refreshed_rows if is_contact_ready(row) and row_identity(row))
    removed = sorted((Counter(before) - Counter(after)).elements())
    report = {
        "rows": len(rows),
        "schema_classes": dict(sorted(classes.items())),
        "before_sendable_identities": before,
        "after_sendable_identities": after,
        "removed_sendable_identities": removed,
        "missing_identity_rows": missing,
        "duplicate_identities": duplicates,
        "errors": errors,
        "safe_to_write": not any((errors, missing, duplicates, removed, len(refreshed_rows) != len(rows))),
    }
    return report, refreshed_rows


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    raise_csv_limit()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return [], []
        return list(reader.fieldnames), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            writer = csv.DictWriter(tmp, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def refresh_csv(input_path: Path | str, output_path: Path | str | None = None) -> int:
    source = Path(input_path)
    destination = Path(output_path) if output_path is not None else source

    fieldnames, rows = read_csv(source)
    final_fieldnames = output_fieldnames(fieldnames)
    report, refreshed_rows = impact_report(rows)
    if not report["safe_to_write"]:
        raise SchemaMigrationError(json.dumps(report, ensure_ascii=False, sort_keys=True))

    write_csv(destination, final_fieldnames, refreshed_rows)

    return len(refreshed_rows)


def check_csv(input_path: Path | str) -> dict[str, Any]:
    _, rows = read_csv(Path(input_path))
    report, _ = impact_report(rows)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh Packet Tier / Manual Review packet fields in a scored CSV without LLM calls."
    )
    parser.add_argument("input", type=Path, help="Existing scored CSV to refresh")
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination scored CSV. Defaults to overwriting the input file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report schema classes and sendable identity impact without writing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        print(json.dumps(check_csv(args.input), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    try:
        count = refresh_csv(args.input, args.output)
    except SchemaMigrationError as exc:
        print(f"[refresh_packet_tiers] REFUSED: {exc}", file=sys.stderr)
        return 2
    destination = args.output or args.input
    print(f"[refresh_packet_tiers] refreshed {count} rows -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
