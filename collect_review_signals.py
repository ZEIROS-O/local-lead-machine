#!/usr/bin/env python3
"""Bounded two-step DataForSEO review collection for scoring v3.

Submit is intentionally separate from collect because DataForSEO's standard
review endpoint is asynchronous. Both commands fail closed on credentials,
task count, and the per-run dollar cap.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from lead_machine.external_signals import DataForSEOReviewsClient, RunBudget, select_deep_enrichment


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def client_from_env() -> DataForSEOReviewsClient:
    max_usd = float(os.getenv("MAX_NEW_DATA_USD_PER_RUN", "10"))
    estimate = float(os.getenv("DATAFORSEO_ESTIMATED_USD_PER_REVIEW_TASK", "0.20"))
    return DataForSEOReviewsClient(
        login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        budget=RunBudget(max_usd=max_usd),
        estimated_usd_per_task=estimate,
    )


def raw_evidence(row: Dict[str, str]) -> Dict[str, Any]:
    try:
        value = json.loads(row.get("Raw Evidence") or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def task_for_row(row: Dict[str, str]) -> Dict[str, str]:
    raw = raw_evidence(row)
    google = raw.get("google") if isinstance(raw.get("google"), dict) else {}
    place_id = str(row.get("Place Key") or google.get("place_id") or "").strip()
    business_name = str(row.get("Business Name") or google.get("business_name") or "").strip()
    city = str(row.get("City") or google.get("city") or "").strip()
    return {
        "place_id": place_id,
        "business_name": business_name,
        "location_name": f"{city}, Ohio, United States" if city else "Ohio, United States",
        "tag": place_id or business_name,
    }


def submit(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.input))
    selected = select_deep_enrichment(rows, limit=args.limit, reject_sample=args.reject_sample)
    tasks = [task_for_row(dict(row)) for row in selected]
    tasks = [task for task in tasks if task["place_id"] or task["business_name"]]
    response_tasks = client_from_env().submit_reviews(tasks, depth=args.depth)
    receipt = {
        "source": "dataforseo_google_reviews_newest",
        "input": str(args.input),
        "depth": args.depth,
        "selected": tasks,
        "tasks": response_tasks,
    }
    write_json(Path(args.output), receipt)
    print(f"[SUBMITTED] review tasks={len(response_tasks)} output={args.output}")
    return 0


def collect(args: argparse.Namespace) -> int:
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    tasks = receipt.get("tasks") if isinstance(receipt.get("tasks"), list) else []
    results: List[Dict[str, Any]] = []
    client = client_from_env()
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            continue
        result = client.get_reviews(str(task["id"]))
        tag = task.get("data", {}).get("tag") if isinstance(task.get("data"), dict) else ""
        result["tag"] = tag or ""
        results.append(result)
    write_jsonl(Path(args.output), results)
    complete = sum(1 for result in results if result.get("state") == "complete")
    print(f"[COLLECTED] complete={complete} pending={len(results) - complete} output={args.output}")
    return 0


def merge(args: argparse.Namespace) -> int:
    records = read_jsonl(Path(args.input))
    signals = read_jsonl(Path(args.signals))
    by_key: Dict[str, Dict[str, Any]] = {}
    for signal in signals:
        for key in (signal.get("place_id"), signal.get("tag")):
            if key:
                by_key[str(key)] = signal
    merged: List[Dict[str, Any]] = []
    for record in records:
        google = record.get("google") if isinstance(record.get("google"), dict) else {}
        place_id = str(google.get("place_id") or google.get("place_id_or_unique_key") or "")
        signal = by_key.get(place_id)
        updated = dict(record)
        if signal and signal.get("state") == "complete":
            updated["review_items"] = signal.get("items") or []
            updated["review_collection"] = {
                "source": signal.get("source"),
                "observed_at": signal.get("observed_at"),
                "depth": int(signal.get("items_count") or len(signal.get("items") or [])),
                "reviews_count": signal.get("reviews_count"),
                "cost_usd": signal.get("cost_usd"),
            }
        merged.append(updated)
    write_jsonl(Path(args.output), merged)
    print(f"[MERGED] records={len(merged)} output={args.output}")
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser("submit", help="Submit bounded newest-first review tasks")
    submit_parser.add_argument("--input", required=True, help="Scored CSV containing v3 shadow fields")
    submit_parser.add_argument("--output", required=True, help="Task receipt JSON")
    submit_parser.add_argument("--limit", type=int, default=25)
    submit_parser.add_argument("--reject-sample", type=int, default=5)
    submit_parser.add_argument("--depth", type=int, default=60)
    submit_parser.set_defaults(handler=submit)

    collect_parser = subparsers.add_parser("collect", help="Collect submitted tasks once ready")
    collect_parser.add_argument("--receipt", required=True)
    collect_parser.add_argument("--output", required=True, help="Review signal JSONL")
    collect_parser.set_defaults(handler=collect)

    merge_parser = subparsers.add_parser("merge", help="Merge collected signals into enriched JSONL")
    merge_parser.add_argument("--input", required=True, help="Original enriched JSONL")
    merge_parser.add_argument("--signals", required=True, help="Collected review signal JSONL")
    merge_parser.add_argument("--output", required=True, help="Enriched JSONL with review items")
    merge_parser.set_defaults(handler=merge)

    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
