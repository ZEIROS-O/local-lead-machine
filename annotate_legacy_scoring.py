#!/usr/bin/env python3
"""Annotate pre-v3 scored CSVs without changing their verdicts."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List


LEGACY_FIELDS = {
    "Scoring Policy Version": "v2-legacy",
    "Legacy Scoring": "Yes",
    "Revalidation Recommended": "Yes",
}


def annotate(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    output: List[Dict[str, str]] = []
    for row in rows:
        updated = dict(row)
        version = str(updated.get("Scoring Policy Version") or "").strip()
        if not version or version.startswith("v2"):
            for key, value in LEGACY_FIELDS.items():
                updated[key] = value
        output.append(updated)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True, help="New CSV path; the input is never overwritten")
    args = parser.parse_args()
    source = Path(args.input)
    destination = Path(args.output)
    if source.resolve() == destination.resolve():
        raise SystemExit("[REFUSE] --output must differ from --input")
    with source.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    annotated = annotate(rows)
    fields = list(rows[0].keys()) if rows else []
    for key in LEGACY_FIELDS:
        if key not in fields:
            fields.append(key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(annotated)
    print(f"[ANNOTATED] rows={len(annotated)} output={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
