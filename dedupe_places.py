#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Dict, List, Set

GENERIC_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "getjobber.com",
    "google.com",
    "maps.google.com",
    "business.site",
    "sites.google.com",
}

def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits

def normalize_domain(url: str) -> str:
    if not url:
        return ""
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.split("/")[0]

def slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

def row_keys(row: Dict[str, str]) -> Set[str]:
    keys = set()

    place_id = (row.get("place_id") or "").strip()
    if place_id:
        keys.add(f"place:{place_id}")

    phone = normalize_phone(row.get("phone", ""))
    if phone:
        keys.add(f"phone:{phone}")

    maps = (row.get("google_maps_url") or "").strip()
    if maps:
        keys.add(f"maps:{maps}")

    domain = normalize_domain(row.get("website_url", ""))
    if domain and domain not in GENERIC_DOMAINS:
        keys.add(f"domain:{domain}")

    name = slug(row.get("business_name", ""))
    city = slug(row.get("city", ""))
    if name and city:
        keys.add(f"name_city:{name}:{city}")

    return keys

def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_exclude_keys(paths: List[Path]) -> Set[str]:
    keys = set()
    for path in paths:
        for row in read_csv(path):
            keys.update(row_keys(row))
    return keys

def write_csv(path: Path, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exclude", nargs="*", default=[])
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    exclude_paths = [Path(p) for p in args.exclude]

    rows = read_csv(input_path)
    if not rows:
        raise SystemExit(f"No rows found in {input_path}")

    fieldnames = list(rows[0].keys())
    exclude_keys = load_exclude_keys(exclude_paths)

    kept = []
    seen_keys = set()
    skipped_previous = 0
    skipped_in_batch = 0

    for row in rows:
        keys = row_keys(row)

        if keys & exclude_keys:
            skipped_previous += 1
            continue

        if keys & seen_keys:
            skipped_in_batch += 1
            continue

        kept.append(row)
        seen_keys.update(keys)

    write_csv(output_path, kept, fieldnames)

    print(f"[DONE] Input rows: {len(rows)}")
    print(f"[DONE] Exclude files: {len(exclude_paths)}")
    print(f"[DONE] Skipped previous-batch duplicates: {skipped_previous}")
    print(f"[DONE] Skipped in-batch duplicates: {skipped_in_batch}")
    print(f"[DONE] Kept rows: {len(kept)}")
    print(f"[DONE] Wrote: {output_path}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
