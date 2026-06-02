#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List

from lead_machine.feedback import compute_feedback_metrics


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_report(path: Path, metrics: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Calibration Report",
        "",
        f"- Total records: {int(metrics['total_records'])}",
        f"- AI-rated A leads: {int(metrics['a_leads'])}",
        f"- A-lead human approval rate: {metrics['a_lead_human_approval_rate']:.1%}",
        f"- Average human review minutes per A lead: {metrics['average_a_review_minutes']:.2f}",
        f"- Sampled skips: {int(metrics['sampled_skips'])}",
        f"- Sampled false-skip rate: {metrics['sampled_false_skip_rate']:.1%}",
        "",
        "Targets:",
        "- 70%+ A-lead human approval",
        "- Under 3 minutes human review per A lead",
        "- Fewer than 10% obvious false skips on sampled Skips",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Feedback CSV")
    parser.add_argument("--output", required=True, help="Markdown report path")
    args = parser.parse_args()

    rows = read_csv(Path(args.input))
    metrics = compute_feedback_metrics(rows)
    write_report(Path(args.output), metrics)

    print(f"[DONE] Rows: {len(rows)}")
    print(f"[DONE] Report: {args.output}")
    print(f"[METRIC] A approval: {metrics['a_lead_human_approval_rate']:.1%}")
    print(f"[METRIC] A review minutes: {metrics['average_a_review_minutes']:.2f}")
    print(f"[METRIC] False skip rate: {metrics['sampled_false_skip_rate']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
