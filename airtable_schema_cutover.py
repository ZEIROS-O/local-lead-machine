#!/usr/bin/env python3
"""Back up and verify removal of fields from an Airtable table.

This tool is deliberately read-only against Airtable. It prepares a local
snapshot/checklist before manual schema deletion in the Airtable UI, then
verifies that fields and records survived the cutover.

The default cutover targets the redundant Legacy* fields below; any other
field set can be backed up the same way via ``--fields`` or
``--fields-config`` (see ``parse_args``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv


LEGACY_FIELD_PAIRS = {
    "Legacy Send Trigger": "Send Trigger",
    "Legacy Last Contacted": "Last Contacted",
    "Legacy Recommended Action": "Recommended Action",
    "Legacy Form Strength": "Form Strength",
    "Legacy Skip Reason": "Skip Reason",
}

# Legacy columns whose canonical half was later demoted off the Airtable surface
# (see docs/audits/2026-07-24-airtable-field-review.md, section H). There is no
# column left to migrate them into, so the pair checks below cannot apply: every
# value in them is legacy-only by definition, which would refuse the cutover
# forever. They are backed up and listed for deletion like any other legacy
# field, and their populated-row counts are reported so the operator sees how
# much data the deletion drops.
LEGACY_ORPHAN_FIELDS = (
    "Legacy Current Website Quality",
    "Legacy CTA Strength",
)


@dataclass(frozen=True)
class FieldCutover:
    """The set of fields one cutover deletes.

    ``pairs`` maps a legacy column to the canonical column its values must
    already live in; deletion is refused until the two reconcile. ``plain``
    fields have no canonical half: they are backed up and deleted outright,
    and their populated-row counts are reported so the operator sees how
    much data the deletion drops.
    """

    pairs: dict[str, str] = dataclass_field(default_factory=dict)
    plain: tuple[str, ...] = ()

    @property
    def delete_fields(self) -> list[str]:
        return [*self.pairs, *self.plain]

    @property
    def canonical_fields(self) -> list[str]:
        return list(self.pairs.values())

    @property
    def backup_fields(self) -> list[str]:
        return ["Place Key", *self.delete_fields, *self.canonical_fields]


DEFAULT_CUTOVER = FieldCutover(dict(LEGACY_FIELD_PAIRS), LEGACY_ORPHAN_FIELDS)


def all_legacy_fields() -> list[str]:
    return DEFAULT_CUTOVER.delete_fields


def schema_field_names(schema: dict[str, Any]) -> set[str]:
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise ValueError("table schema must contain a fields list")
    return {
        str(field.get("name") or "")
        for field in fields
        if isinstance(field, dict) and field.get("name")
    }


def comparable(field: str, value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:10] if field == "Legacy Last Contacted" and len(text) >= 10 else text


def prepare_cutover(
    schema: dict[str, Any],
    records: list[dict[str, Any]],
    cutover: FieldCutover = DEFAULT_CUTOVER,
) -> dict[str, Any]:
    names = schema_field_names(schema)
    missing_legacy = sorted(set(cutover.delete_fields) - names)
    missing_canonical = sorted(set(cutover.canonical_fields) - names)
    legacy_only = {field: [] for field in cutover.pairs}
    conflicts = {field: [] for field in cutover.pairs}
    orphan_populated = {field: 0 for field in cutover.plain}
    for record in records:
        fields = record.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        record_id = str(record.get("id") or "<missing-record-id>")
        for legacy, current in cutover.pairs.items():
            old = comparable(legacy, fields.get(legacy))
            new = comparable(legacy, fields.get(current))
            if old and not new:
                legacy_only[legacy].append(record_id)
            elif old and new and old != new:
                conflicts[legacy].append(record_id)
        for orphan in cutover.plain:
            if comparable(orphan, fields.get(orphan)):
                orphan_populated[orphan] += 1
    populated_legacy_only = {key: value for key, value in legacy_only.items() if value}
    populated_conflicts = {key: value for key, value in conflicts.items() if value}
    return {
        "record_count": len(records),
        "missing_legacy_fields": missing_legacy,
        "missing_canonical_fields": missing_canonical,
        "legacy_only": populated_legacy_only,
        # Orphans have no canonical column left, so their values cannot be
        # reconciled — only backed up. Reported, never blocking.
        "orphan_populated_counts": {k: v for k, v in orphan_populated.items() if v},
        "conflicts": populated_conflicts,
        "safe_to_delete": not any(
            (missing_legacy, missing_canonical, populated_legacy_only, populated_conflicts)
        ),
    }


def verify_cutover(
    snapshot: dict[str, Any],
    schema: dict[str, Any],
    record_count: int,
    cutover: FieldCutover = DEFAULT_CUTOVER,
) -> list[str]:
    names = schema_field_names(schema)
    errors: list[str] = []
    expected_count = int(snapshot["record_count"])
    if record_count != expected_count:
        errors.append(
            f"record count changed: expected {expected_count}, found {record_count}"
        )
    remaining = sorted(set(cutover.delete_fields) & names)
    if remaining:
        label = "legacy fields" if cutover is DEFAULT_CUTOVER else "cutover fields"
        errors.append(f"{label} still present: " + ", ".join(remaining))
    missing_current = sorted(set(cutover.canonical_fields) - names)
    if missing_current:
        errors.append("canonical fields missing: " + ", ".join(missing_current))
    return errors


def get_json(
    url: str, token: str, *, params: list[tuple[str, Any]] | None = None
) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params or [],
        timeout=30,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("Airtable response must be an object")
    return value


def fetch_table_schema(base_id: str, table: str, token: str) -> dict[str, Any]:
    value = get_json(
        f"https://api.airtable.com/v0/meta/bases/{quote(base_id)}/tables", token
    )
    matches = [
        item
        for item in value.get("tables", [])
        if isinstance(item, dict)
        and (item.get("id") == table or item.get("name") == table)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one Airtable table named/id {table!r}, found {len(matches)}"
        )
    return matches[0]


def fetch_records(
    base_id: str,
    table: str,
    token: str,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = ""
    while True:
        params: list[tuple[str, Any]] = [
            ("pageSize", 100),
            *[("fields[]", field) for field in (fields or [])],
        ]
        if offset:
            params.append(("offset", offset))
        page = get_json(
            f"https://api.airtable.com/v0/{quote(base_id)}/{quote(table, safe='')}",
            token,
            params=params,
        )
        page_records = page.get("records", [])
        if not isinstance(page_records, list):
            raise ValueError("Airtable records response must contain a records list")
        records.extend(item for item in page_records if isinstance(item, dict))
        offset = str(page.get("offset") or "")
        if not offset:
            return records


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def backup_records(
    records: list[dict[str, Any]], cutover: FieldCutover = DEFAULT_CUTOVER
) -> dict[str, Any]:
    names = cutover.backup_fields
    return {
        "records": [
            {
                "record_id": record["id"],
                **{
                    name: (record.get("fields") or {}).get(name, "")
                    for name in names
                },
            }
            for record in records
        ]
    }


def parse_field_list(value: str) -> tuple[str, ...]:
    """Parse a comma-separated ``--fields`` value into exact field names."""
    fields = tuple(
        dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
    )
    if not fields:
        raise ValueError("--fields must name at least one field")
    return fields


def load_fields_config(path: Path) -> tuple[str, ...]:
    """Load a JSON fields config: an object with a non-empty ``fields`` list."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read fields config {path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"fields config {path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("fields"), list):
        raise ValueError(
            f'fields config {path} must be a JSON object with a "fields" list'
        )
    fields = value["fields"]
    if not fields or not all(isinstance(item, str) and item.strip() for item in fields):
        raise ValueError(
            f'fields config {path} "fields" must name at least one non-empty string'
        )
    return tuple(dict.fromkeys(item.strip() for item in fields))


def resolve_cutover(args: argparse.Namespace) -> FieldCutover:
    """The cutover a CLI invocation asked for; the default when no override."""
    if getattr(args, "fields", None) is not None:
        return FieldCutover(plain=parse_field_list(args.fields))
    if getattr(args, "fields_config", None):
        return FieldCutover(plain=load_fields_config(args.fields_config))
    return DEFAULT_CUTOVER


def cutover_from_snapshot(snapshot: dict[str, Any]) -> FieldCutover:
    """The cutover recorded in a snapshot; the default for older snapshots."""
    stored = snapshot.get("cutover")
    if stored is None:
        return DEFAULT_CUTOVER
    if not isinstance(stored, dict):
        raise ValueError("snapshot cutover section must be an object")
    pairs = stored.get("pairs")
    plain = stored.get("plain")
    if (
        not isinstance(pairs, dict)
        or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
            for k, v in pairs.items()
        )
        or not isinstance(plain, list)
        or not all(isinstance(item, str) and item.strip() for item in plain)
        or not (pairs or plain)
    ):
        raise ValueError("snapshot cutover section is malformed")
    return FieldCutover(dict(pairs), tuple(plain))


def _credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    load_dotenv()
    base_id = args.base_id or os.getenv("AIRTABLE_BASE_ID", "")
    table = (
        args.table
        or os.getenv("AIRTABLE_PROSPECTS_TABLE", "")
        or os.getenv("AIRTABLE_TABLE", "")
    )
    token = (
        args.token
        or os.getenv("AIRTABLE_PAT", "")
        or os.getenv("AIRTABLE_API_KEY", "")
    )
    missing = [
        name
        for name, value in (
            ("AIRTABLE_BASE_ID", base_id),
            ("AIRTABLE_PROSPECTS_TABLE", table),
            ("AIRTABLE_PAT", token),
        )
        if not value
    ]
    if missing:
        raise ValueError("missing Airtable configuration: " + ", ".join(missing))
    return base_id, table, token


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_prepare(args: argparse.Namespace) -> int:
    base_id, table, token = _credentials(args)
    cutover = resolve_cutover(args)
    schema = fetch_table_schema(base_id, table, token)
    records = fetch_records(
        base_id, str(schema.get("id") or table), token, cutover.backup_fields
    )
    report = prepare_cutover(schema, records, cutover)
    output_dir = args.output_dir or Path("run_state/airtable_schema_cutover") / _timestamp()
    backup_path = output_dir / "backup.json"
    backup = backup_records(records, cutover)
    write_json_atomic(backup_path, backup)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_id": base_id,
        "table": {"id": schema.get("id", ""), "name": schema.get("name", "")},
        "cutover": {"pairs": dict(cutover.pairs), "plain": list(cutover.plain)},
        "record_count": len(records),
        "fields": [
            {"id": field.get("id", ""), "name": field.get("name", "")}
            for field in schema.get("fields", [])
            if isinstance(field, dict)
        ],
        "redundancy": report,
        "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
    }
    snapshot_path = output_dir / "pre_cutover.json"
    write_json_atomic(snapshot_path, snapshot)
    if report["safe_to_delete"]:
        checklist = [
            "# Airtable field deletion checklist",
            "",
            "Delete these fields manually in the Airtable UI:",
            "",
            *[f"- [ ] `{name}`" for name in cutover.delete_fields],
            "",
        ]
        orphan_counts = report.get("orphan_populated_counts") or {}
        if orphan_counts:
            checklist += [
                "## Read before deleting",
                "",
                "These fields have no canonical column to migrate into, so deleting",
                f"them drops their values from Airtable. `{backup_path.name}` is the only copy.",
                "",
                *[
                    f"- `{name}` holds data on {count} of {len(records)} records"
                    for name, count in sorted(orphan_counts.items())
                ],
                "",
            ]
        checklist += [
            "Then verify without modifying Airtable:",
            "",
            f"`.venv/bin/python airtable_schema_cutover.py verify {snapshot_path}`",
            "",
        ]
        write_text_atomic(output_dir / "delete_checklist.md", "\n".join(checklist))
    print(
        json.dumps(
            {
                "safe_to_delete": report["safe_to_delete"],
                "output_dir": str(output_dir),
                "report": report,
            },
            indent=2,
        )
    )
    return 0 if report["safe_to_delete"] else 2


def run_verify(args: argparse.Namespace) -> int:
    base_id, table, token = _credentials(args)
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if snapshot.get("base_id") != base_id:
        raise ValueError("snapshot base_id does not match configured Airtable base")
    expected_table = snapshot.get("table") or {}
    configured_matches_snapshot = table in {
        str(expected_table.get("id") or ""),
        str(expected_table.get("name") or ""),
    }
    if not configured_matches_snapshot:
        raise ValueError("snapshot table does not match configured Airtable table")
    # A CLI override wins; otherwise verify the set the snapshot was prepared
    # with (older snapshots predate the section and fall back to the default).
    if (
        getattr(args, "fields", None) is not None
        or getattr(args, "fields_config", None) is not None
    ):
        cutover = resolve_cutover(args)
    else:
        cutover = cutover_from_snapshot(snapshot)
    schema = fetch_table_schema(base_id, table, token)
    records = fetch_records(base_id, str(schema.get("id") or table), token, fields=[])
    errors = verify_cutover(snapshot, schema, len(records), cutover)
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 2


def _add_field_override_args(subparser: argparse.ArgumentParser) -> None:
    group = subparser.add_mutually_exclusive_group()
    group.add_argument(
        "--fields",
        default=None,
        help="comma-separated field names to delete outright (backed up first); "
        "overrides the default Legacy* set",
    )
    group.add_argument(
        "--fields-config",
        type=Path,
        help='path to a JSON object with a "fields" list, same effect as --fields',
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare and verify manual Airtable field deletion (GET-only)."
    )
    parser.add_argument("--base-id", default="")
    parser.add_argument("--table", default="")
    parser.add_argument("--token", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--output-dir", type=Path)
    _add_field_override_args(prepare)
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot", type=Path)
    _add_field_override_args(verify)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_prepare(args) if args.command == "prepare" else run_verify(args)
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"[airtable-schema-cutover] REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
