#!/usr/bin/env python3
"""Build a deterministic, local-only input corpus for Airtable upserts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import AbstractSet, Any, Iterable

from lead_machine.schema import normalize_phone
from lead_machine.airtable_sync import VERTICAL_CHOICES, normalize_airtable_value
from lead_machine.compliance import (
    ComplianceError,
    load_configured_suppression_index,
    record_matches_suppression,
)
from lead_machine.csv_validation import read_validated_dict_rows
from lead_machine.verticals import resolve_profile
from refresh_verdicts import VERDICT_REFRESH_FIELDS, refresh_row, validate_refreshed_row


def raise_csv_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def upsert_key(row: dict[str, Any]) -> str:
    place_key = str(row.get("Place Key") or "").strip()
    if place_key:
        return f"place:{place_key}"
    phone = normalize_phone(str(row.get("Phone") or ""))
    return f"phone:{phone}" if phone else ""


def _resolve_upload_vertical(
    row: dict[str, Any], overrides: dict[str, str], key: str
) -> tuple[str, bool]:
    """Return a supported Airtable vertical without guessing unknown services."""
    stored = str(row.get("Vertical") or "").strip()
    stored_profile = resolve_profile(stored, fallback="").key
    if stored.casefold() == "service":
        try:
            raw = json.loads(str(row.get("Raw Evidence") or ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"service row {key} has malformed Raw Evidence") from exc
        google = raw.get("google") if isinstance(raw, dict) else None
        if not isinstance(google, dict):
            raise ValueError(f"service row {key} has malformed Raw Evidence.google")
        category = str(google.get("category") or "")
        source_query = str(google.get("source_query") or "")
        resolved = resolve_profile(category, source_query=source_query, fallback="").key
        vertical = str(normalize_airtable_value("Vertical", resolved) or "")
    else:
        vertical = str(normalize_airtable_value("Vertical", stored_profile) or "")

    override = ""
    if key in overrides:
        override = str(normalize_airtable_value("Vertical", overrides[key]) or "")
        if not override or override not in VERTICAL_CHOICES:
            raise ValueError(f"override for {key} is not a supported Vertical")
    if not vertical:
        if stored.casefold() == "service":
            if override:
                return override, True
            raise ValueError(f"cannot resolve service Vertical for {key}")
        raise ValueError(f"unsupported Vertical {stored!r} for {key}")
    if vertical not in VERTICAL_CHOICES:
        raise ValueError(f"unsupported Vertical {vertical!r} for {key}")
    return vertical, False


def resolve_upload_vertical(
    row: dict[str, Any], overrides: dict[str, str], key: str
) -> str:
    """Return a supported Airtable vertical without guessing unknown services."""
    return _resolve_upload_vertical(row, overrides, key)[0]


def _sorted_paths(paths: Iterable[Path | str]) -> list[Path]:
    resolved = [Path(path) for path in paths]
    return sorted(resolved, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def _validate_distinct_paths(paths: Iterable[tuple[str, Path | None]]) -> None:
    seen: dict[Path, str] = {}
    for label, path in paths:
        if path is None:
            continue
        resolved = path.resolve()
        prior = seen.get(resolved)
        if prior is not None:
            raise ValueError(
                f"path collision: {label} and {prior} both resolve to {resolved}"
            )
        seen[resolved] = label


def prepare_corpus(
    paths: Iterable[Path | str],
    overrides: dict[str, str],
    excluded_upsert_keys: Iterable[str] = (),
    *,
    suppression_index: AbstractSet[str] = frozenset(),
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    raise_csv_limit()
    suppressed_rows = 0
    fieldnames: list[str] = []
    selected: dict[str, tuple[Path, int, dict[str, str]]] = {}
    skipped_no_upsert = 0
    overwritten_rows = 0
    source_rows = 0
    sorted_paths = _sorted_paths(paths)
    for source_path in sorted_paths:
        with source_path.open(newline="", encoding="utf-8") as handle:
            source_fieldnames, source_records = read_validated_dict_rows(
                handle, source=str(source_path)
            )
            for field in source_fieldnames:
                if field not in fieldnames:
                    fieldnames.append(field)
            for line_number, row in enumerate(source_records, start=2):
                source_rows += 1
                key = upsert_key(row)
                if not key:
                    skipped_no_upsert += 1
                    continue
                if key in selected:
                    overwritten_rows += 1
                selected[key] = (source_path, line_number, row)
    exclusions = set(excluded_upsert_keys)
    excluded_selected_keys = exclusions & set(selected)
    unused_exclusions = sorted(exclusions - set(selected))
    if unused_exclusions:
        raise ValueError(f"unused excluded upsert keys: {len(unused_exclusions)}")
    rows: list[dict[str, str]] = []
    consumed_overrides: set[str] = set()
    source_biconditional_violations = 0
    source_verdict_tier_violations = 0
    for key, (_, _, source_row) in selected.items():
        if key in excluded_selected_keys:
            continue
        prepared = dict(source_row)
        prepared["Vertical"], consumed_override = _resolve_upload_vertical(prepared, overrides, key)
        if consumed_override:
            consumed_overrides.add(key)
        # Count staleness on the *stored* row, before the refresh overwrites it.
        # Measuring after `refresh_row` counted nothing at all: that function
        # already raises on any error in the row it returns, so re-validating
        # its output was tautologically clean and both counters were always
        # zero — a report field that read as verification but asserted nothing.
        # What the report is meant to carry is how many stored verdicts the
        # current gate disagreed with, which is only observable before the
        # rewrite.
        for error in validate_refreshed_row(prepared):
            if error.startswith(("Sendable must", "Verdict must")):
                source_biconditional_violations += 1
            else:
                source_verdict_tier_violations += 1
        # Checked before the refresh, because the refresh is what stamps it.
        suppressed = bool(suppression_index) and record_matches_suppression(
            prepared, suppression_index
        )
        if suppressed:
            suppressed_rows += 1
        prepared = refresh_row(prepared, suppression_index=suppression_index)
        changed = {
            field
            for field in set(source_row) | set(prepared)
            if str(source_row.get(field, "")) != str(prepared.get(field, ""))
        }
        allowed_changes = {"Vertical", *VERDICT_REFRESH_FIELDS}
        if suppressed:
            # A suppression stamp is machine-owned and compliance-critical, but
            # `Skip Reason` sits outside the ordinary refresh contract. Widened
            # only for the rows that actually matched the ledger, so the guard
            # stays exactly as tight as before for every other row.
            allowed_changes |= {"Skip Reason", "Hard Disqualifiers", "hard_disqualifiers"}
        unauthorized = changed - allowed_changes
        if unauthorized:
            raise ValueError("preparation changed fields outside its machine-owned contract")
        rows.append(prepared)
    unused_overrides = sorted(set(overrides) - consumed_overrides)
    if unused_overrides:
        raise ValueError(f"unused vertical overrides: {len(unused_overrides)}")
    for field in ["Vertical", *VERDICT_REFRESH_FIELDS]:
        if field not in fieldnames:
            fieldnames.append(field)
    # Only when a lead was actually suppressed — see refresh_verdicts.
    if suppressed_rows and "Skip Reason" not in fieldnames:
        fieldnames.append("Skip Reason")
    verdict_counts = Counter(str(row.get("Verdict") or "") for row in rows)
    report = {
        "source_file_count": len(sorted_paths),
        "source_row_count": source_rows,
        "output_row_count": len(rows),
        "overwritten_rows": overwritten_rows,
        "skipped_no_upsert": skipped_no_upsert,
        "excluded_upsert_count": len(excluded_selected_keys),
        "excluded_upsert_hashes": sorted(
            hashlib.sha256(key.encode("utf-8")).hexdigest() for key in excluded_selected_keys
        ),
        # Renamed from `biconditional_violations`/`verdict_tier_violations`,
        # which were measured after the refresh and so were always zero. The
        # `source_` prefix says what they now count — disagreements in the
        # stored input — so a report diff cannot read the change from a
        # tautological zero to a real figure as a regression.
        "source_biconditional_violations": source_biconditional_violations,
        "source_verdict_tier_violations": source_verdict_tier_violations,
        # How many rows the live ledger stopped. Reported rather than silent:
        # a corpus that suppressed nothing and a corpus built with no ledger
        # configured produce the same verdict counts, and only one of them is
        # safe to upload.
        "suppressed_rows": suppressed_rows,
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "selected_source_provenance": {
            hashlib.sha256(key.encode("utf-8")).hexdigest(): {
                "source_path": str(path),
                "line_number": line_number,
            }
            for key, (path, line_number, _) in selected.items()
        },
    }
    return fieldnames, rows, report


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _load_overrides(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("vertical overrides must be valid JSON") from exc
    if not isinstance(loaded, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in loaded.items()):
        raise ValueError("vertical overrides must be a JSON object of string keys and values")
    return loaded


def _load_excluded_upsert_keys(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("excluded upsert keys must be valid JSON") from exc
    if not isinstance(loaded, list) or not all(isinstance(key, str) and key for key in loaded):
        raise ValueError("excluded upsert keys must be a JSON list of non-empty strings")
    if len(set(loaded)) != len(loaded):
        raise ValueError("excluded upsert keys must not contain duplicates")
    return loaded


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a lossless, refreshed Airtable upload corpus.")
    parser.add_argument("--source", type=Path, action="append", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vertical-overrides", type=Path)
    parser.add_argument("--excluded-upsert-keys", type=Path)
    parser.add_argument("--expect-source-rows", type=int)
    parser.add_argument("--expect-output-rows", type=int)
    parser.add_argument("--expect-skipped-no-key", type=int)
    parser.add_argument("--expect-excluded-upsert", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    args.source = [path for group in args.source for path in group]
    return args


def _verify_expected_counts(args: argparse.Namespace, report: dict[str, Any]) -> None:
    expected = {
        "expect-source-rows": (args.expect_source_rows, "source_row_count"),
        "expect-output-rows": (args.expect_output_rows, "output_row_count"),
        "expect-skipped-no-key": (args.expect_skipped_no_key, "skipped_no_upsert"),
        "expect-excluded-upsert": (args.expect_excluded_upsert, "excluded_upsert_count"),
    }
    for flag, (wanted, actual_key) in expected.items():
        if wanted is not None and wanted != report[actual_key]:
            raise ValueError(f"{flag} expected {wanted}, got {report[actual_key]}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Fail closed before reading a row, like the other entry points. This
    # rebuilds `Verdict` from stored CSV rows and produces the file
    # `upload_airtable.py` writes from, so an unconfigured ledger here is how an
    # opted-out lead is rebuilt to `send` and pushed back into the base.
    try:
        suppression_index = load_configured_suppression_index()
    except ComplianceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    try:
        source_paths = [Path(path) for path in args.source]
        output = Path(args.output)
        _validate_distinct_paths(
            [
                *((f"source[{index}]", source) for index, source in enumerate(source_paths, start=1)),
                ("output", output),
                ("report", args.report),
                ("vertical overrides", args.vertical_overrides),
                ("excluded upsert keys", args.excluded_upsert_keys),
            ]
        )
        fieldnames, rows, report = prepare_corpus(
            source_paths,
            _load_overrides(args.vertical_overrides),
            _load_excluded_upsert_keys(args.excluded_upsert_keys),
            suppression_index=suppression_index,
        )
        _verify_expected_counts(args, report)
        _write_csv(output, fieldnames, rows)
        public_report = {**report, "output": str(output)}
        if args.report:
            _write_report(args.report, public_report)
        print(json.dumps(public_report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"[prepare_airtable_corpus] REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
