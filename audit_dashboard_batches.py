#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lead_machine.dashboard.data import DashboardRepository
from lead_machine.dashboard.legacy import load_legacy_batches


LITERAL_ARTIFACT_KEYS = ("raw", "deduped", "enriched", "scored", "packets")


def audit_repository(root: Path | str) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    repo = DashboardRepository(root_path)
    response = repo.batches(lifecycle="all", force=True)
    discrepancies: list[dict[str, str]] = []
    unknowns: list[dict[str, str]] = []
    lifecycle_counts = {"active": 0, "legacy": 0}
    canonical_names = {batch["batch"] for batch in response["items"]}

    for batch in response["items"]:
        lifecycle_counts[batch["lifecycle"]] += 1
        for key in LITERAL_ARTIFACT_KEYS:
            artifact = batch["artifacts"].get(key)
            if (
                artifact
                and artifact["rows"] is not None
                and batch["counts"][key] != artifact["rows"]
            ):
                discrepancies.append(
                    {
                        "batch": batch["batch"],
                        "field": key,
                        "message": "summary count differs from artifact rows",
                    }
                )
        if batch["reconciliation"]["status"] == "unknown":
            unknowns.append(
                {
                    "batch": batch["batch"],
                    "field": "reconciliation",
                    "message": "explicitly unresolved",
                }
            )
        if (
            batch["lifecycle"] == "active"
            and batch["counts"]["scored"]
            and batch["current_sendability"]["status"] != "evaluated"
        ):
            discrepancies.append(
                {
                    "batch": batch["batch"],
                    "field": "current_sendability",
                    "message": "active scored batch is not current-schema compatible",
                }
            )

    registered_names = set(load_legacy_batches(root_path))
    for missing in sorted(registered_names - canonical_names):
        discrepancies.append(
            {
                "batch": missing,
                "field": "lifecycle",
                "message": "legacy registry entry has no canonical batch",
            }
        )

    return {
        "batch_count": response["total"],
        "lifecycle_counts": lifecycle_counts,
        "unknowns": unknowns,
        "discrepancies": discrepancies,
        "ok": not discrepancies,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit dashboard counts and lifecycle contracts without writing pipeline artifacts."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = audit_repository(args.root)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
