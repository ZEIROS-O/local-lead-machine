#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from lead_machine.evidence import build_evidence_profile
from lead_machine.llm_queue import write_review_queue


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if "evidence" not in record:
                record["evidence"] = build_evidence_profile(record)
            records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input enriched JSONL")
    parser.add_argument("--output-dir", required=True, help="Review queue directory")
    parser.add_argument("--vertical", default="service", help="Fallback vertical")
    parser.add_argument("--limit", type=int, default=0, help="Optional max prompt count")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    records = read_jsonl(input_path)
    if args.limit > 0:
        records = records[: args.limit]

    paths = write_review_queue(records, Path(args.output_dir), fallback_vertical=args.vertical)

    print(f"[DONE] Input: {input_path}")
    print(f"[DONE] Queue dir: {args.output_dir}")
    print(f"[DONE] Prompts written: {len(paths)}")
    print(f"[NEXT] Paste model JSON outputs into: {Path(args.output_dir) / 'scores'}")

    for path in paths[:10]:
        print(f"[PROMPT] {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
