#!/usr/bin/env python3
"""Read-only collectors for optional scoring-v3 website and map evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from lead_machine.external_signals import PageSpeedClient, RunBudget, SemrushMapRankClient


def write(path: str, value: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def merge_signal(input_path: str, signal_path: str, output_path: str) -> None:
    rows = read_jsonl(Path(input_path))
    signal = json.loads(Path(signal_path).read_text(encoding="utf-8"))
    observation = signal.get("observation") if isinstance(signal, dict) else None
    if not isinstance(observation, dict):
        raise ValueError("signal file does not contain a normalized observation")
    place_id = str(signal.get("place_id") or "")
    url = str(signal.get("url") or "")
    matches = 0
    for row in rows:
        google = row.get("google") if isinstance(row.get("google"), dict) else {}
        website = row.get("website") if isinstance(row.get("website"), dict) else {}
        row_place = str(google.get("place_id") or google.get("place_id_or_unique_key") or "")
        row_url = str(website.get("url") or google.get("website_url") or "")
        if (place_id and row_place == place_id) or (url and row_url == url):
            existing = row.get("signal_observations") if isinstance(row.get("signal_observations"), list) else []
            by_id = {
                str(item.get("signal_id")): dict(item)
                for item in existing
                if isinstance(item, dict) and item.get("signal_id")
            }
            by_id[str(observation.get("signal_id") or len(by_id))] = observation
            row["signal_observations"] = list(by_id.values())
            matches += 1
    if matches != 1:
        raise ValueError(f"signal merge expected exactly one matching lead, found {matches}")
    write_jsonl(Path(output_path), rows)


def semrush_client() -> SemrushMapRankClient:
    return SemrushMapRankClient(
        token=os.getenv("SEMRUSH_API_TOKEN", "").strip(),
        budget=RunBudget(max_units=int(os.getenv("MAX_SEMRUSH_API_UNITS_PER_RUN", "2500"))),
    )


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    campaigns = subparsers.add_parser("semrush-campaigns")
    campaigns.add_argument("--query", default="")
    campaigns.add_argument("--output", required=True)
    metrics = subparsers.add_parser("semrush-metrics")
    metrics.add_argument("--campaign-id", required=True)
    metrics.add_argument("--keyword-id", required=True)
    metrics.add_argument("--place-id", required=True)
    metrics.add_argument("--cid", default="")
    metrics.add_argument("--query", required=True)
    metrics.add_argument("--grid", required=True)
    metrics.add_argument("--device", required=True)
    metrics.add_argument("--location", required=True)
    metrics.add_argument("--output", required=True)
    pagespeed = subparsers.add_parser("pagespeed")
    pagespeed.add_argument("--url", required=True)
    pagespeed.add_argument("--strategy", choices=["mobile", "desktop"], default="mobile")
    pagespeed.add_argument("--output", required=True)
    merge = subparsers.add_parser("merge", help="Merge one normalized external observation into enriched JSONL")
    merge.add_argument("--input", required=True)
    merge.add_argument("--signal", required=True)
    merge.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "semrush-campaigns":
            result = semrush_client().list_campaigns(query=args.query)
        elif args.command == "semrush-metrics":
            result = semrush_client().keyword_metrics(
                campaign_id=args.campaign_id,
                keyword_id=args.keyword_id,
                place_id=args.place_id,
                cid=args.cid,
                query=args.query,
                grid=args.grid,
                device=args.device,
                location=args.location,
            )
        elif args.command == "merge":
            merge_signal(args.input, args.signal, args.output)
            print(f"[MERGED] signal={args.signal} output={args.output}")
            return 0
        else:
            result = PageSpeedClient(
                api_key=os.getenv("GOOGLE_PAGESPEED_API_KEY", "").strip()
            ).analyze(args.url, strategy=args.strategy)
        write(args.output, result)
        print(f"[COLLECTED] source={args.command} output={args.output}")
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
