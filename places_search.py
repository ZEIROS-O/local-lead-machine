#!/usr/bin/env python3
"""
Phase 5: Google Places search.

Reads search queries from data/searches.csv.
Calls Places API (New) Text Search.
Calls Place Details for each result.
Dedupes results.
Writes output/raw_places.csv.

Run:
    source .venv/bin/activate
    python places_search.py --searches data/searches.csv --output output/raw_places.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv


TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Search uses places.* field paths.
TEXT_SEARCH_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.primaryType",
        "places.types",
    ]
)

# Details does NOT use places.* prefixes.
# Do not use "*". Keep this strict to avoid pulling unnecessary paid fields.
PLACE_DETAILS_FIELD_MASK = ",".join(
    [
        "id",
        "displayName",
        "formattedAddress",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "websiteUri",
        "rating",
        "userRatingCount",
        "googleMapsUri",
        "businessStatus",
        "primaryType",
        "types",
        "regularOpeningHours",
    ]
)


@dataclass
class Lead:
    business_name: str = ""
    category: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    website_url: str = ""
    google_maps_url: str = ""
    rating: str = ""
    reviews: str = ""
    place_id: str = ""
    business_status: str = ""
    source_query: str = ""
    duplicate_key: str = ""


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D+", "", phone or "")


def normalize_domain(url: str) -> str:
    if not url:
        return ""
    cleaned = url.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = re.sub(r"^www\.", "", cleaned)
    cleaned = cleaned.split("/")[0]
    return cleaned


def slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def extract_city(address: str) -> str:
    """
    Best-effort extraction for addresses like:
    '123 Main St, Chardon, OH 44024, USA'
    """
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 2:
        return parts[-3] if len(parts) >= 4 else parts[-2]
    return ""


def make_duplicate_key(name: str, city: str, phone: str, website_url: str, place_id: str) -> str:
    phone_digits = normalize_phone(phone)
    if phone_digits:
        return f"phone:{phone_digits}"

    domain = normalize_domain(website_url)
    if domain:
        return f"domain:{domain}"

    if place_id:
        return f"place:{place_id}"

    return f"name_city:{slug(name)}_{slug(city)}"


def read_searches(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing searches file: {path}")

    rows: List[Dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "query" not in reader.fieldnames:
            raise ValueError("data/searches.csv must include a 'query' column")

        for row in reader:
            query = (row.get("query") or "").strip()
            if not query:
                continue

            max_results_raw = (row.get("max_results") or "10").strip()
            try:
                max_results = int(max_results_raw)
            except ValueError:
                max_results = 10

            rows.append({"query": query, "max_results": max(1, min(max_results, 20))})

    return rows


def request_json(method: str, url: str, *, headers: Dict[str, str], json_body: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
    try:
        if method.upper() == "POST":
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        elif method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=timeout)
        else:
            raise ValueError(f"Unsupported method: {method}")
    except requests.RequestException as exc:
        raise RuntimeError(f"HTTP request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise RuntimeError(f"Google API error {resp.status_code}: {resp.text[:1000]}")

    return resp.json()


def text_search(api_key: str, query: str, max_results: int) -> List[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }

    body = {
        "textQuery": query,
        "maxResultCount": max_results,
        "languageCode": "en",
        "regionCode": "US",
    }

    data = request_json("POST", TEXT_SEARCH_URL, headers=headers, json_body=body)
    return data.get("places", []) or []


def place_details(api_key: str, place_id: str) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACE_DETAILS_FIELD_MASK,
    }

    url = PLACE_DETAILS_URL.format(place_id=place_id)
    return request_json("GET", url, headers=headers)


def normalize_place(details: Dict[str, Any], source_query: str) -> Lead:
    display = details.get("displayName") or {}
    name = display.get("text") or ""
    address = details.get("formattedAddress") or ""
    city = extract_city(address)

    phone = details.get("nationalPhoneNumber") or details.get("internationalPhoneNumber") or ""
    website = details.get("websiteUri") or ""
    maps_url = details.get("googleMapsUri") or ""

    category = details.get("primaryType") or ""
    if not category:
        types = details.get("types") or []
        category = types[0] if types else ""

    rating = details.get("rating")
    reviews = details.get("userRatingCount")

    lead = Lead(
        business_name=name,
        category=category,
        city=city,
        address=address,
        phone=phone,
        website_url=website,
        google_maps_url=maps_url,
        rating="" if rating is None else str(rating),
        reviews="" if reviews is None else str(reviews),
        place_id=details.get("id") or "",
        business_status=details.get("businessStatus") or "",
        source_query=source_query,
    )

    lead.duplicate_key = make_duplicate_key(
        lead.business_name,
        lead.city,
        lead.phone,
        lead.website_url,
        lead.place_id,
    )
    return lead


def write_csv(path: Path, leads: Iterable[Lead]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = list(asdict(Lead()).keys())

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for lead in leads:
            writer.writerow(asdict(lead))


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--searches", default="data/searches.csv", help="CSV with query,max_results columns")
    parser.add_argument("--output", default="output/raw_places.csv", help="Output CSV path")
    parser.add_argument("--sleep", type=float, default=0.15, help="Seconds between Google calls")
    args = parser.parse_args()

    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] Missing GOOGLE_PLACES_API_KEY in .env", file=sys.stderr)
        return 1

    max_search_calls = getenv_int("MAX_SEARCH_CALLS_PER_RUN", 3)
    max_details_calls = getenv_int("MAX_DETAILS_CALLS_PER_RUN", 50)
    max_total_places = getenv_int("MAX_TOTAL_PLACES_PER_RUN", 50)

    searches_path = Path(args.searches)
    output_path = Path(args.output)

    searches = read_searches(searches_path)
    if not searches:
        print("[ERROR] No searches found in data/searches.csv", file=sys.stderr)
        return 1

    print(f"[INFO] Loaded {len(searches)} searches")
    print(f"[INFO] Caps: searches={max_search_calls}, details={max_details_calls}, total_places={max_total_places}")

    leads_by_key: Dict[str, Lead] = {}
    search_calls = 0
    details_calls = 0

    for search in searches:
        if search_calls >= max_search_calls:
            print("[STOP] Hit MAX_SEARCH_CALLS_PER_RUN")
            break

        query = search["query"]
        max_results = min(int(search["max_results"]), max_total_places)

        print(f"[SEARCH] {query!r} max_results={max_results}")

        try:
            places = text_search(api_key, query, max_results)
            search_calls += 1
        except Exception as exc:
            print(f"[ERROR] Text search failed for {query!r}: {exc}", file=sys.stderr)
            continue

        print(f"[INFO] Found {len(places)} places")

        for place in places:
            if details_calls >= max_details_calls:
                print("[STOP] Hit MAX_DETAILS_CALLS_PER_RUN")
                break

            if len(leads_by_key) >= max_total_places:
                print("[STOP] Hit MAX_TOTAL_PLACES_PER_RUN")
                break

            place_id = place.get("id") or ""
            if not place_id:
                continue

            try:
                details = place_details(api_key, place_id)
                details_calls += 1
                lead = normalize_place(details, query)
            except Exception as exc:
                print(f"[ERROR] Details failed for place_id={place_id}: {exc}", file=sys.stderr)
                continue

            if not lead.business_name:
                continue

            if lead.duplicate_key in leads_by_key:
                print(f"[DUP] {lead.business_name} ({lead.duplicate_key})")
            else:
                leads_by_key[lead.duplicate_key] = lead
                print(f"[ADD] {lead.business_name} | {lead.city} | {lead.rating} | {lead.reviews}")

            time.sleep(args.sleep)

        time.sleep(args.sleep)

    leads = list(leads_by_key.values())
    write_csv(output_path, leads)

    print(f"[DONE] Search calls: {search_calls}")
    print(f"[DONE] Details calls: {details_calls}")
    print(f"[DONE] Unique leads: {len(leads)}")
    print(f"[DONE] Wrote: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
