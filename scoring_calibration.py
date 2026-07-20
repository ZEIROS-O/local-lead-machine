#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from lead_machine.scoring_calibration import attach_predictions, build_blind_sample, cohens_kappa, evaluate_calibration


def read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sample(args: argparse.Namespace) -> int:
    primary, audit = build_blind_sample(read_rows(Path(args.input)))
    write_rows(Path(args.output), primary)
    write_rows(Path(args.audit_output), audit)
    print(f"[SAMPLE] primary={len(primary)} audit={len(audit)}")
    return 0


def evaluate(args: argparse.Namespace) -> int:
    rows = attach_predictions(read_rows(Path(args.input)), read_rows(Path(args.predictions)))
    pairs = None
    if args.audit_input:
        audit_rows = read_rows(Path(args.audit_input))
        primary_by_key = {
            row.get("Calibration Lead Key", ""): row.get("Human Disposition", "").strip().lower()
            for row in rows
        }
        pairs = [
            (primary_by_key.get(row.get("Calibration Lead Key", ""), ""), row.get("Human Disposition", "").strip().lower())
            for row in audit_rows
        ]
    report = evaluate_calibration(rows, audit_pairs=pairs)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["launch_passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    sample_parser = subparsers.add_parser("sample")
    sample_parser.add_argument("--input", required=True)
    sample_parser.add_argument("--output", required=True)
    sample_parser.add_argument("--audit-output", required=True)
    sample_parser.set_defaults(handler=sample)
    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--input", required=True)
    evaluate_parser.add_argument("--predictions", required=True, help="Original scored CSV used to restore hidden predictions")
    evaluate_parser.add_argument("--audit-input", default="")
    evaluate_parser.add_argument("--output", default="")
    evaluate_parser.set_defaults(handler=evaluate)
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
