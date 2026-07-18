#!/usr/bin/env python3
"""Refresh packet-tier fields in an existing scored CSV without LLM calls."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

from lead_machine.commercial_backfill import COMMERCIAL_FIELD_ALIASES, backfill_legacy_commercial_fit
from lead_machine.score_schema import SCORE_SCHEMA_VERSION
from lead_machine.sendability import apply_sendability


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


def refresh_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_version = str(row.get("Score Schema Version") or row.get("score_schema_version") or "").strip()
    migrated = row if raw_version == str(SCORE_SCHEMA_VERSION) else backfill_legacy_commercial_fit(row)
    refreshed = apply_sendability(migrated)
    updated = dict(row)
    for field in REFRESH_FIELDS:
        updated[field] = refreshed.get(field, "")
    return updated


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    raise_csv_limit()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return [], []
        return list(reader.fieldnames), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def refresh_csv(input_path: Path | str, output_path: Path | str | None = None) -> int:
    source = Path(input_path)
    destination = Path(output_path) if output_path is not None else source

    fieldnames, rows = read_csv(source)
    final_fieldnames = output_fieldnames(fieldnames)
    refreshed_rows = [refresh_row(row) for row in rows]

    if destination == source:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=str(source.parent),
            prefix=f".{source.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            writer = csv.DictWriter(tmp, fieldnames=final_fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(refreshed_rows)
        temp_path.replace(source)
    else:
        write_csv(destination, final_fieldnames, refreshed_rows)

    return len(refreshed_rows)


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    count = refresh_csv(args.input, args.output)
    destination = args.output or args.input
    print(f"[refresh_packet_tiers] refreshed {count} rows -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
