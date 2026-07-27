#!/usr/bin/env python3
"""Summarise where the LLM grade and the ICP closeability ceiling disagree.

`fit_ceiling` runs only inside `score_record`, the deterministic path. In
`--mode api-llm` — the mode production runs — `Lead Priority` is a bare
threshold on the LLM total and the closeability layer never executes. Batch 3
attaches the ceiling to LLM rows as shadow columns rather than enforcing it,
because neither grader has been shown right. This reads that shadow data.

The output is the input to that decision: if the ceiling is usually correct
where they disagree, wire it in; if it mostly demotes leads that close, delete
it. Until then it costs three CSV columns and changes nothing.

Reads scored CSVs, not enriched JSONL — the shadow columns are written by the
scorer, so `make_icp_lane_report.py` (which reads JSONL) cannot see them.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

SHADOW_COLUMNS = ("ICP Ceiling", "ICP Ceiling Flags", "Grade Disagreement")

# Scored CSVs embed the full enriched record in Raw Evidence, which routinely
# exceeds the stdlib default of 131072.
csv.field_size_limit(sys.maxsize)


def read_scored_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if rows and not any(column in rows[0] for column in SHADOW_COLUMNS):
        raise ValueError(
            f"{path} has no ICP ceiling shadow columns. It was scored before Batch 3, "
            "or scored on the deterministic path where the ceiling is enforced rather "
            "than shadowed."
        )
    return rows


def summarise(rows: Iterable[Dict[str, str]]) -> Dict[str, Any]:
    row_list = [row for row in rows if _clean(row.get("ICP Ceiling"))]
    disagreements = [row for row in row_list if _clean(row.get("Grade Disagreement"))]

    by_pair: Counter[str] = Counter()
    by_flag: Counter[str] = Counter()
    sendable_disagreements = 0
    for row in disagreements:
        by_pair[_clean(row.get("Grade Disagreement"))] += 1
        for flag in _split_flags(row.get("ICP Ceiling Flags")):
            by_flag[flag] += 1
        if _clean(row.get("Sendable")).lower() == "yes":
            sendable_disagreements += 1

    return {
        "judged": len(row_list),
        "disagreements": len(disagreements),
        "sendable_disagreements": sendable_disagreements,
        "by_pair": by_pair,
        "by_flag": by_flag,
        "grade_by_ceiling": Counter(
            (_clean(row.get("Lead Priority")), _clean(row.get("ICP Ceiling"))) for row in row_list
        ),
        "rows": disagreements,
    }


def render_report(summary: Dict[str, Any], *, input_labels: List[str]) -> str:
    lines: List[str] = ["# Grade Disagreement Report", ""]
    lines.extend(
        [
            "`Lead Priority` is the grade that ships. `ICP Ceiling` is what the",
            "closeability layer would have capped it to, computed in shadow and",
            "enforced nowhere. A disagreement is a lead graded *better* than its",
            "ceiling; a grade at or below the ceiling is the ceiling doing nothing.",
            "",
        ]
    )
    if input_labels:
        lines.extend(["Inputs:", ""])
        lines.extend(f"- `{label}`" for label in input_labels)
        lines.append("")

    judged = summary["judged"]
    disagreements = summary["disagreements"]
    share = f"{100.0 * disagreements / judged:.1f}%" if judged else "n/a"
    lines.extend(
        [
            "## Counts",
            "",
            f"- Rows with a computed ceiling: {judged}",
            f"- Disagreements: {disagreements} ({share})",
            f"- Disagreements that are currently Sendable: {summary['sendable_disagreements']}",
            "",
        ]
    )

    if summary["by_pair"]:
        lines.extend(["## Disagreement shape", "", "| Grade vs ceiling | Count |", "| --- | --- |"])
        for pair, count in summary["by_pair"].most_common():
            lines.append(f"| {pair} | {count} |")
        lines.append("")

    if summary["by_flag"]:
        lines.extend(
            [
                "## Why the ceiling capped them",
                "",
                "The reason the closeability layer gave. If these read as real",
                "objections, enforce the ceiling; if they read as noise, delete it.",
                "",
                "| Ceiling flag | Count |",
                "| --- | --- |",
            ]
        )
        for flag, count in summary["by_flag"].most_common():
            lines.append(f"| {_md(flag)} | {count} |")
        lines.append("")

    lines.extend(["## Grade against ceiling", "", "| Grade | Ceiling | Count |", "| --- | --- | --- |"])
    for (grade, ceiling), count in sorted(summary["grade_by_ceiling"].items()):
        lines.append(f"| {_md(grade)} | {_md(ceiling)} | {count} |")
    lines.append("")

    if summary["rows"]:
        lines.extend(
            [
                "## Disagreeing leads",
                "",
                "| Business | Grade | Ceiling | Sendable | Ceiling flags |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in summary["rows"]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _md(row.get("Business Name")),
                        _md(row.get("Lead Priority")),
                        _md(row.get("ICP Ceiling")),
                        _md(row.get("Sendable")),
                        _md(row.get("ICP Ceiling Flags")),
                    ]
                )
                + " |"
            )
        lines.append("")

    return "\n".join(lines)


def _split_flags(value: Any) -> List[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _md(value: Any) -> str:
    return _clean(value).replace("|", "\\|") or "-"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, nargs="+", help="Scored CSV path(s)")
    parser.add_argument("--output", default="", help="Markdown output path; prints to stdout if omitted")
    args = parser.parse_args()

    rows: List[Dict[str, str]] = []
    labels: List[str] = []
    for raw_path in args.input:
        path = Path(raw_path)
        try:
            rows.extend(read_scored_csv(path))
        except (OSError, ValueError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        labels.append(str(path))

    report = render_report(summarise(rows), input_labels=labels)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"[DONE] Wrote: {output_path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
