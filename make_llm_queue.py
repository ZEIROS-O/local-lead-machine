#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from lead_machine.evidence import build_evidence_profile
from lead_machine.llm_queue import write_review_queue


CORE_EVIDENCE_FIELDS = (
    "visible_pain_score",
    "visible_pain_reasons",
    "send_trigger",
)
RENDERED_EVIDENCE_FIELDS = (
    "rendered_site_status",
    "rendered_status_reason",
    "desktop_screenshot_path",
    "mobile_screenshot_path",
    "visual_review_needed",
    "visual_quality_score",
    "premium_perception_score",
    "mobile_visual_score",
    "hero_clarity_score",
    "cta_visibility_score",
    "gallery_visibility_score",
    "trust_proof_visibility_score",
    "visual_pain_reasons",
    "screenshots",
)


def ensure_evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    evidence = record.get("evidence")
    if isinstance(evidence, dict) and all(key in evidence for key in CORE_EVIDENCE_FIELDS):
        if all(key in evidence for key in RENDERED_EVIDENCE_FIELDS):
            return record
        rebuilt = build_evidence_profile(record)
        merged = dict(evidence)
        for key in RENDERED_EVIDENCE_FIELDS:
            if key not in merged and key in rebuilt:
                merged[key] = rebuilt[key]
        record = dict(record)
        record["evidence"] = merged
        return record

    record = dict(record)
    record["evidence"] = build_evidence_profile(record)
    return record


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records.append(ensure_evidence(record))
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
