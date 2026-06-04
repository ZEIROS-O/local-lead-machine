#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from lead_machine.icp_lanes import LANES, classify_icp_lane, classify_records_by_lane, lane_sort_key


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        records.append(record)
    return records


def write_lane_report(records: Iterable[Dict[str, Any]], output_path: Path, *, input_label: str = "") -> None:
    record_list = list(records)
    grouped = classify_records_by_lane(record_list)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = ["# ICP Lane Report", ""]
    if input_label:
        lines.extend([f"Input: `{input_label}`", ""])

    lines.extend(["## Counts", ""])
    for lane in LANES:
        lines.append(f"- {lane}: {len(grouped.get(lane, []))}")
    lines.append("")

    for lane in LANES:
        lane_records = sorted(grouped.get(lane, []), key=lambda record: lane_sort_key(record, lane))
        lines.extend([f"## {lane}", ""])
        if not lane_records:
            lines.extend(["No records.", ""])
            continue

        lines.append(
            "| Business | Rating / Reviews | Website | Score / Tier | Send Trigger | Reasons | Blockers |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for record in lane_records[:20]:
            result = classify_icp_lane(record)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _md(_business_name(record)),
                        _md(_rating_reviews(record)),
                        _md(_website(record)),
                        _md(f"{result.score} / {result.tier}"),
                        _md(_send_trigger(record) or _first(result.reasons)),
                        _md("; ".join(result.reasons)),
                        _md("; ".join(result.blockers)),
                    ]
                )
                + " |"
            )
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def _business_name(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    return _pick(google, "business_name", "Business Name", "name") or _pick(
        record, "business_name", "Business Name", "name"
    )


def _rating_reviews(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    rating = _pick(google, "rating", "Rating", "google_rating") or _pick(record, "rating", "Rating")
    reviews = _pick(google, "reviews", "Reviews", "google_review_count") or _pick(record, "reviews", "Reviews")
    return f"{rating or '-'} / {reviews or '-'}"


def _website(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    homepage = website.get("homepage") if isinstance(website.get("homepage"), dict) else {}
    return (
        _pick(google, "website", "website_url", "Website URL")
        or _pick(record, "website_url", "Website URL")
        or _pick(website, "final_url")
        or _pick(homepage, "url")
        or "-"
    )


def _send_trigger(record: Dict[str, Any]) -> str:
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    return _pick(record, "send_trigger", "Send Trigger") or _pick(evidence, "send_trigger")


def _pick(values: Dict[str, Any], *keys: str) -> str:
    if not isinstance(values, dict):
        return ""
    for key in keys:
        value = values.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, (dict, list, tuple, set)):
                continue
            return str(value).strip()
    return ""


def _first(values: List[str]) -> str:
    return values[0] if values else ""


def _md(value: str) -> str:
    cleaned = str(value or "").replace("|", "\\|")
    return " ".join(cleaned.split())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input enriched JSONL path")
    parser.add_argument("--output", required=True, help="Output markdown report path")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    output_path = Path(args.output)
    write_lane_report(read_jsonl(input_path), output_path, input_label=str(input_path))
    print(f"[DONE] ICP lane report: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
