#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Dict, List


FIELDS = [
    "Business Name",
    "Owner Name",
    "Vertical",
    "City",
    "Phone",
    "Email",
    "Website URL",
    "Google Maps URL",
    "Reviews",
    "Rating",
    "Current Website Quality",
    "Best Service Angle",
    "Specific Observation",
    "Mockup URL",
    "Video URL",
    "Outreach Channel",
    "Status",
    "Last Contacted",
    "Next Follow-Up",
    "Assigned To",
    "Lead Priority",
    "Offer Fit",
    "Site Status",
    "Evidence Notes",
    "AI Confidence",
    "Manual Reviewed",
    "Skip Reason",
    "Desktop Screenshot Path",
    "Mobile Screenshot Path",
    "Created Time",
    "Last Modified Time",
]

RAW_FIELDS = [
    "business_name",
    "category",
    "city",
    "address",
    "phone",
    "website_url",
    "google_maps_url",
    "rating",
    "reviews",
    "place_id",
    "business_status",
    "source_query",
    "duplicate_key",
]


def clean(value: str) -> str:
    value = (value or "").strip()
    if value.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return ""
    return value


def slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_domain(url: str) -> str:
    url = clean(url).lower()
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.split("/")[0]


def duplicate_key(record: Dict[str, str]) -> str:
    phone = normalize_phone(record.get("Phone", ""))
    if phone:
        return f"phone:{phone}"

    domain = normalize_domain(record.get("Website URL", ""))
    if domain:
        return f"domain:{domain}"

    return f"name_city:{slug(record.get('Business Name', ''))}:{slug(record.get('City', ''))}"


def parse_records(text: str) -> List[Dict[str, str]]:
    # Match field labels at the start of a line, even when value is glued to label:
    # Business NameA & C Landscaping Inc
    labels = sorted(FIELDS, key=len, reverse=True)
    pattern = re.compile(r"(?m)^(" + "|".join(re.escape(f) for f in labels) + r")")

    matches = list(pattern.finditer(text))
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}

    for idx, match in enumerate(matches):
        field = match.group(1)
        value_start = match.end()
        value_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        value = text[value_start:value_end].strip()

        if field == "Business Name" and current.get("Business Name"):
            records.append(current)
            current = {}

        current[field] = clean(value)

    if current.get("Business Name"):
        records.append(current)

    normalized = []
    for record in records:
        normalized.append({field: clean(record.get(field, "")) for field in FIELDS})

    return normalized


def to_raw_row(record: Dict[str, str]) -> Dict[str, str]:
    return {
        "business_name": record.get("Business Name", ""),
        "category": record.get("Vertical", ""),
        "city": record.get("City", ""),
        "address": "",
        "phone": record.get("Phone", ""),
        "website_url": record.get("Website URL", ""),
        "google_maps_url": record.get("Google Maps URL", ""),
        "rating": record.get("Rating", ""),
        "reviews": record.get("Reviews", ""),
        "place_id": "",
        "business_status": "OPERATIONAL",
        "source_query": "manual_existing",
        "duplicate_key": duplicate_key(record),
    }


def write_csv(path: Path, rows: List[Dict[str, str]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/manual_existing_records.txt")
    parser.add_argument("--raw-output", default="output/raw_places_manual_existing.csv")
    parser.add_argument("--records-output", default="output/manual_existing_records_parsed.csv")
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    records = parse_records(text)
    raw_rows = [to_raw_row(record) for record in records]

    write_csv(Path(args.records_output), records, FIELDS)
    write_csv(Path(args.raw_output), raw_rows, RAW_FIELDS)

    print(f"[DONE] Parsed records: {len(records)}")
    print(f"[DONE] Raw output: {args.raw_output}")
    print(f"[DONE] Parsed record output: {args.records_output}")

    for record in records:
        print(f'- {record.get("Business Name")} | {record.get("Phone")} | {record.get("Website URL") or "NO WEBSITE"}')

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
