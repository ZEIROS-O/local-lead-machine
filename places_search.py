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
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from lead_machine.compliance import (
    ComplianceError,
    has_suppression_match,
    load_configured_suppression_index,
)
from lead_machine.review_velocity import ReviewSnapshotStore
from lead_machine.scoring_profiles import infer_market, infer_vertical_key


TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Mirrors llm_client.TRANSIENT_HTTP_STATUSES: the statuses a retry can actually
# change. Everything else — a malformed query, a disabled key, an exhausted
# quota — will fail the same way however many times it is sent.
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class GoogleApiError(RuntimeError):
    """A non-2xx response from the Places API, carrying its status.

    Subclasses RuntimeError so existing `except RuntimeError` handlers up the
    call stack keep working unchanged.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_transient(self) -> bool:
        return self.status_code in TRANSIENT_HTTP_STATUSES

# Search uses places.* field paths. "nextPageToken" is a top-level field (not
# places.*) and must be in the mask to paginate; it does not raise the billing SKU.
TEXT_SEARCH_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.primaryType",
        "places.types",
        "nextPageToken",
    ]
)

# Places API (New) Text Search hard-caps a page at 20 results.
MAX_PAGE_SIZE = 20

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


def _distinct_business(kept: "Lead", candidate: "Lead") -> bool:
    """Are these plausibly two businesses rather than one listed twice?

    A distinct place_id with a different name is the signal. Google lists the
    same business more than once often enough that name equality alone would
    make this warn constantly.
    """
    if kept.place_id and candidate.place_id and kept.place_id == candidate.place_id:
        return False
    return slug(kept.business_name) != slug(candidate.business_name)


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
        raise GoogleApiError(
            f"Google API error {resp.status_code}: {resp.text[:1000]}",
            status_code=resp.status_code,
        )

    return resp.json()


def _text_search_page(
    headers: Dict[str, str], body: Dict[str, Any], *, retries: int = 2, backoff: float = 1.5
) -> Dict[str, Any]:
    """POST one Text Search page, retrying only what a retry can fix.

    This caught every RuntimeError, and `request_json` raises on any status at
    or above 400 — so a 400 for a malformed query, a 403 for a disabled key, or
    a quota error was retried three times with backoff before failing anyway.
    Only the statuses that clear on their own are retried now, plus a
    not-yet-ready pageToken, which is the transient case this existed for.
    Mocked request_json in tests never retries.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return request_json("POST", TEXT_SEARCH_URL, headers=headers, json_body=body)
        except GoogleApiError as exc:
            if not exc.is_transient:
                raise
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
        except RuntimeError as exc:
            # A transport failure with no status attached; worth another try.
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def text_search_paged(
    api_key: str,
    query: str,
    *,
    page_size: int,
    max_pages: int = 1,
    max_results: Optional[int] = None,
    sleep_seconds: float = 0.0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Paginated Places Text Search (New).

    Returns ``(places, pages_fetched)``. Combines up to ``max_pages`` pages (the API
    hard-caps at 60 results = 3 x 20), dedupes by place id within the call, and stops
    early when the API returns no ``nextPageToken``, the page cap is reached, or
    ``max_results`` places have been collected. Each page is a separate billable call.
    """
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    max_pages = max(1, int(max_pages))

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    # All paged requests must repeat the original params (textQuery, pageSize, ...).
    base_body = {
        "textQuery": query,
        "pageSize": page_size,
        "languageCode": "en",
        "regionCode": "US",
    }

    places: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    pages_fetched = 0
    page_token = ""

    while pages_fetched < max_pages:
        body = dict(base_body)
        if page_token:
            body["pageToken"] = page_token
        data = _text_search_page(headers, body)
        pages_fetched += 1

        for place in data.get("places", []) or []:
            place_id = place.get("id") or ""
            if place_id and place_id in seen_ids:
                continue
            if place_id:
                seen_ids.add(place_id)
            places.append(place)
            if max_results is not None and len(places) >= max_results:
                return places, pages_fetched

        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return places, pages_fetched


def text_search(api_key: str, query: str, max_results: int) -> List[Dict[str, Any]]:
    """Single-page Text Search (backward-compatible wrapper)."""
    places, _ = text_search_paged(api_key, query, page_size=max_results, max_pages=1)
    return places


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
    """LEGACY entry point — still live, still called, do not extend.

    `run_pipeline.py` runs this as stage 1, so it is a real paid path, not dead
    code. It is legacy in what it does rather than whether it runs: it reads
    `data/searches.csv`, a hand-maintained query list nothing in the current flow
    produces, and it does not prefilter, dedupe against a batch, or enrich.
    `make_search_batch.py` does all three from a JSON search config with geo
    slices, and is where new search behaviour belongs.

    An earlier version of this docstring said "not part of the current pipeline"
    while `run_pipeline.py` was calling it, which is how a reader concludes the
    suppression guard below does not matter. It does — see the comment on it.
    """
    load_dotenv()

    # Fires on every run_pipeline.py run, because run_pipeline still calls this
    # as stage 1. Worded as a "prefer" rather than a "wrong": an operator seeing
    # a [LEGACY] line during an ordinary batch should not go hunting for a fault.
    print(
        "[LEGACY] places_search.py runs an ad-hoc CSV query list without "
        "prefilter, dedupe or enrichment; prefer "
        "make_search_batch.py --config <search config>.",
        file=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        description="LEGACY ad-hoc Places search from a hand-written CSV. "
        "The pipeline uses make_search_batch.py."
    )
    parser.add_argument("--searches", default="data/searches.csv", help="CSV with query,max_results columns")
    parser.add_argument("--output", default="output/raw_places.csv", help="Output CSV path")
    parser.add_argument("--sleep", type=float, default=0.15, help="Seconds between Google calls")
    parser.add_argument(
        "--review-snapshot-db",
        default=os.getenv("REVIEW_SNAPSHOT_DB", "run_state/review_velocity.sqlite3"),
        help="SQLite path for longitudinal Google review-count observations",
    )
    args = parser.parse_args()

    # After parse_args, so --help works, and reported as an [ERROR] rather than
    # an unhandled traceback — same shape as make_search_batch.main and
    # enrich_leads.main. Legacy or not, run_pipeline.py still runs this file as
    # stage 1, so it is a live path to a paid Places call and the refusal has to
    # land before the first one rather than downstream of it.
    try:
        suppression_index = load_configured_suppression_index()
    except ComplianceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

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
    snapshot_store = ReviewSnapshotStore(args.review_snapshot_db)
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
                # Google bills on return, so the call counts even for a lead
                # dropped below. MAX_DETAILS_CALLS_PER_RUN caps spend, not kept
                # leads: skipping the increment for a call already paid for would
                # let a run quietly bill past its own cap and make the
                # "[DONE] Details calls" line understate the invoice.
                details_calls += 1
                lead = normalize_place(details, query)
            except Exception as exc:
                print(f"[ERROR] Details failed for place_id={place_id}: {exc}", file=sys.stderr)
                continue

            if not lead.business_name:
                continue

            if has_suppression_match(phones=[lead.phone], suppression_index=suppression_index):
                # Earliest point an opted-out business is recognisable here. The
                # Text Search page cannot be matched against the index at all:
                # TEXT_SEARCH_FIELD_MASK returns id, displayName,
                # formattedAddress and types, and a Place ID is deliberately not
                # a suppression identifier (see record_suppression_identifiers) —
                # so Details is the first payload carrying a phone, and paying
                # for it is unavoidable without raising the Text Search SKU on
                # every place to save it on a handful. What is avoidable is
                # everything after: this runs ahead of the review-velocity write
                # so a business that asked us to stop leaves no row in run_state,
                # and ahead of the raw CSV every later stage reads.
                print(f"[SKIP] Suppressed: {lead.business_name}")
                continue

            if lead.place_id and lead.reviews:
                snapshot_record = {"google": asdict(lead)}
                try:
                    snapshot_store.record_snapshot(
                        place_key=lead.place_id,
                        review_count=int(float(lead.reviews)),
                        rating=float(lead.rating) if lead.rating else None,
                        vertical=infer_vertical_key(snapshot_record),
                        market=infer_market(snapshot_record),
                        source="google_places",
                    )
                except Exception as exc:
                    # Own handler now that this sits outside the Details try, so a
                    # locked SQLite file is not reported as a Google failure.
                    # Still drops the lead, as it did when it shared that block.
                    print(
                        f"[ERROR] Review snapshot failed for place_id={place_id}: {exc}",
                        file=sys.stderr,
                    )
                    continue

            if lead.duplicate_key in leads_by_key:
                kept = leads_by_key[lead.duplicate_key]
                if lead.duplicate_key.startswith("phone:") and _distinct_business(kept, lead):
                    # Phone-first dedupe collapses anything sharing a number, so
                    # two genuinely different businesses behind one answering
                    # service or shared front desk silently become one lead.
                    # Name the collision rather than hiding it in a [DUP] line.
                    print(
                        f"[DUP COLLISION] {lead.duplicate_key} merged "
                        f"{lead.business_name!r} into {kept.business_name!r}; "
                        "different names share this phone number",
                        file=sys.stderr,
                    )
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
