#!/usr/bin/env python3
"""Refresh machine-owned verdict fields from the current sendability gate."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

import refresh_packet_tiers
from lead_machine.csv_validation import read_validated_dict_rows
from lead_machine.sendability import (
    VERDICT_PACKET_TIERS,
    apply_sendability,
    evaluate_sendability,
)
from lead_machine.verdict import apply_verdict


VERDICT_REFRESH_FIELDS = [
    *refresh_packet_tiers.REFRESH_FIELDS,
    "Verdict",
    "Verdict Reasons",
]


class VerdictRefreshError(ValueError):
    """Raised when a refreshed row is inconsistent with the authority."""


def raise_csv_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def output_fieldnames(existing: Iterable[str]) -> list[str]:
    fieldnames = list(existing)
    for field in VERDICT_REFRESH_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    return fieldnames


def validate_refreshed_row(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    authority = evaluate_sendability(row)
    stored_sendable = str(row.get("Sendable") or "").strip().casefold() == "yes"
    if stored_sendable != bool(authority["sendable"]):
        errors.append("Sendable must be true exactly when current authority is sendable")

    verdict = str(row.get("Verdict") or "").strip()
    if bool(authority["sendable"]) != (verdict == "send"):
        errors.append("Verdict must be send exactly when current authority is sendable")
    expected_tier = VERDICT_PACKET_TIERS.get(verdict)
    if expected_tier is None:
        errors.append(f"unknown verdict {verdict!r}")
    elif str(row.get("Packet Tier") or "").strip() != expected_tier:
        errors.append(f"Packet Tier must be {expected_tier!r} for Verdict {verdict!r}")
    return errors


def refresh_row(row: dict[str, Any]) -> dict[str, Any]:
    projected = apply_verdict(apply_sendability(row))
    updated = dict(row)
    for field in VERDICT_REFRESH_FIELDS:
        updated[field] = projected.get(field, "")
    errors = validate_refreshed_row(updated)
    if errors:
        raise VerdictRefreshError("; ".join(errors))
    return updated


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    raise_csv_limit()
    with path.open(newline="", encoding="utf-8") as handle:
        return read_validated_dict_rows(handle, source=str(path))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
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


def _project(path: Path) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    fieldnames, rows = _read_csv(path)
    refreshed_rows = [refresh_row(row) for row in rows]
    verdict_counts = Counter(str(row["Verdict"]) for row in refreshed_rows)
    report = {"rows": len(refreshed_rows), "verdict_counts": dict(sorted(verdict_counts.items()))}
    return fieldnames, refreshed_rows, report


def refresh_csv(
    input_path: Path | str,
    output_path: Path | str | None = None,
    *,
    in_place: bool = False,
) -> dict[str, Any]:
    source = Path(input_path)
    if output_path is None and not in_place:
        raise ValueError("refresh_csv requires an explicit output_path or in_place=True")
    if output_path is not None and in_place:
        raise ValueError("output_path and in_place=True are mutually exclusive")

    destination = source if in_place else Path(output_path)
    if not in_place and destination.resolve() == source.resolve():
        raise ValueError("same-path writes require in_place=True")
    fieldnames, rows, report = _project(source)
    _write_csv(destination, output_fieldnames(fieldnames), rows)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh stored verdicts from current sendability gates.")
    parser.add_argument("input", type=Path)
    write_mode = parser.add_mutually_exclusive_group()
    write_mode.add_argument("--output", type=Path)
    write_mode.add_argument("--in-place", action="store_true", help="Atomically replace the input CSV.")
    parser.add_argument("--check", action="store_true", help="Print the projection report without writing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.check:
            _, _, report = _project(args.input)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            if args.output is None and not args.in_place:
                raise ValueError("normal use requires --output or --in-place")
            report = refresh_csv(args.input, args.output, in_place=args.in_place)
            destination = args.input if args.in_place else args.output
            print(json.dumps({**report, "output": str(destination)}, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"[refresh_verdicts] REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
