#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from enrich_leads import enrich_row
from places_search import normalize_place, place_details, text_search
from lead_machine.schema import GENERIC_DOMAINS, normalize_domain, normalize_phone, slug


REQUIRED_CONFIG_FIELDS = ["batch", "vertical", "location", "limit_per_query", "queries", "geo_slices"]

REQUIRED_FINAL_FIELDS = [
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
    "Place Key",
    "Place ID",
    "Source Query",
    "Source Geo",
    "Source Batch",
]

INTERNAL_AIRTABLE_FIELDS = [
    "Evidence Refs",
    "Safe Outreach Claims",
    "Unsafe Claims",
    "Minimum Evidence Passed",
    "Packet Eligibility Reason",
]

CSV_FIELD_ORDER = [
    "business_name",
    "category",
    "city",
    "address",
    "phone",
    "email",
    "website_url",
    "google_maps_url",
    "rating",
    "reviews",
    "place_id",
    "place_key",
    "business_status",
    "source_query",
    "source_geo",
    "source_search",
    "source_batch",
    "vertical",
    "duplicate_key",
] + REQUIRED_FINAL_FIELDS + ["Source Search"]

FRANCHISE_OR_CHAIN_TERMS = [
    "brightview",
    "groundworks",
    "siteone",
    "trugreen",
    "lawn doctor",
    "u.s. lawns",
    "us lawns",
    "weed man",
    "davey tree",
]

NURSERY_GARDEN_TERMS = ["nursery", "garden center", "landscape supply", "mulch supply", "stone yard"]
TREE_ONLY_TERMS = ["tree service", "tree removal", "arborist", "stump grinding"]
SNOW_ONLY_TERMS = ["snow removal only", "snow-only", "snow plowing only", "snow removal"]
FERTILIZATION_ONLY_TERMS = ["lawn fertilization", "fertilization", "weed control", "turf care"]
COMMERCIAL_MAINTENANCE_TERMS = [
    "commercial maintenance only",
    "commercial maintenance",
    "facility maintenance",
    "property maintenance",
]


@dataclass(frozen=True)
class SearchConfig:
    batch: str
    vertical: str
    location: str
    limit_per_query: int
    queries: List[str]
    geo_slices: List[str]
    min_rating: float = 0.0
    min_reviews: int = 0
    allow_missing_website: bool = True
    exclude_terms: List[str] = None  # type: ignore[assignment]
    boost_terms: List[str] = None  # type: ignore[assignment]
    source_path: Path = Path("")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source_path: Path) -> "SearchConfig":
        missing = [field for field in REQUIRED_CONFIG_FIELDS if field not in data or data.get(field) in ("", None, [])]
        if missing:
            raise ValueError(f"Search config missing required fields: {', '.join(missing)}")

        queries = _string_list(data.get("queries"))
        geo_slices = _string_list(data.get("geo_slices"))
        if not queries:
            raise ValueError("Search config field queries must contain at least one query")
        if not geo_slices:
            raise ValueError("Search config field geo_slices must contain at least one geo slice")

        try:
            limit_per_query = int(data.get("limit_per_query"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Search config field limit_per_query must be an integer") from exc
        if limit_per_query <= 0:
            raise ValueError("Search config field limit_per_query must be greater than 0")

        return cls(
            batch=str(data.get("batch") or "").strip(),
            vertical=str(data.get("vertical") or "").strip(),
            location=str(data.get("location") or "").strip(),
            limit_per_query=min(limit_per_query, 20),
            queries=queries,
            geo_slices=geo_slices,
            min_rating=_float_or_default(data.get("min_rating"), 0.0),
            min_reviews=_int_or_default(data.get("min_reviews"), 0),
            allow_missing_website=bool(data.get("allow_missing_website", True)),
            exclude_terms=_string_list(data.get("exclude_terms")),
            boost_terms=_string_list(data.get("boost_terms")),
            source_path=source_path,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch": self.batch,
            "vertical": self.vertical,
            "location": self.location,
            "limit_per_query": self.limit_per_query,
            "queries": list(self.queries),
            "geo_slices": list(self.geo_slices),
            "min_rating": self.min_rating,
            "min_reviews": self.min_reviews,
            "allow_missing_website": self.allow_missing_website,
            "exclude_terms": list(self.exclude_terms or []),
            "boost_terms": list(self.boost_terms or []),
        }


@dataclass(frozen=True)
class ExpandedSearch:
    source_query: str
    source_geo: str
    search_text: str
    max_results: int


@dataclass(frozen=True)
class PrefilterResult:
    should_skip: bool
    reason: str = ""
    boost_terms: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchRunResult:
    batch: str
    vertical: str
    config_path: Path
    raw_path: Path
    deduped_path: Path
    enriched_path: Path
    report_path: Path
    queries_run: List[str]
    geo_slices: List[str]
    raw_count: int
    deduped_count: int
    icp_pass_count: int
    enriched_count: int
    skipped_count: int
    skip_reason_counts: Dict[str, int]
    duplicate_reason_counts: Dict[str, int]
    top_candidate_names: List[str]
    warnings: List[str]
    errors: List[str]


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _float_or_default(value: Any, default: float) -> float:
    if value in ("", None):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_default(value: Any, default: int) -> int:
    if value in ("", None):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _validate_optional_cap(name: str, value: Optional[int]) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _progress(message: str) -> None:
    print(message, flush=True)


def _inline_counts(items: Mapping[str, int]) -> str:
    if not items:
        return "none"
    return ", ".join(f"{key}: {value}" for key, value in sorted(items.items()))


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _pick(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return _clean(value)
    return ""


def _row_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if is_dataclass(row):
        return asdict(row)
    return dict(getattr(row, "__dict__", {}) or {})


def load_search_config(path: Path) -> SearchConfig:
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Search config must be a JSON object: {path}")
    return SearchConfig.from_dict(data, source_path=path)


def replace_location(query: str, *, base_location: str, geo: str) -> str:
    query = re.sub(r"\s+", " ", query or "").strip()
    geo = re.sub(r"\s+", " ", geo or "").strip()
    base_location = re.sub(r"\s+", " ", base_location or "").strip()
    if not query:
        return geo
    if base_location and re.search(re.escape(base_location), query, flags=re.IGNORECASE):
        return re.sub(re.escape(base_location), geo, query, count=1, flags=re.IGNORECASE).strip()
    if geo and not re.search(re.escape(geo), query, flags=re.IGNORECASE):
        return f"{query} {geo}".strip()
    return query


def expand_searches(config: SearchConfig) -> List[ExpandedSearch]:
    expanded: List[ExpandedSearch] = []
    for query in config.queries:
        for geo in config.geo_slices:
            expanded.append(
                ExpandedSearch(
                    source_query=query,
                    source_geo=geo,
                    search_text=replace_location(query, base_location=config.location, geo=geo),
                    max_results=config.limit_per_query,
                )
            )
    return expanded


def make_place_key(row: Mapping[str, Any]) -> str:
    place_id = _pick(row, "place_id", "Place ID")
    if place_id:
        return place_id

    phone = normalize_phone(_pick(row, "phone", "Phone"))
    if phone:
        return f"phone:{phone}"

    domain = normalize_domain(_pick(row, "website_url", "Website URL", "website"))
    if domain and domain not in GENERIC_DOMAINS:
        return f"domain:{domain}"

    name = slug(_pick(row, "business_name", "Business Name", "name"))
    city = slug(_pick(row, "city", "City"))
    if name and city:
        return f"name_city:{name}:{city}"

    return name or "unknown"


def normalize_search_row(
    row: Mapping[str, Any],
    *,
    config: SearchConfig,
    source_query: str,
    source_geo: str,
    source_search: str,
) -> Dict[str, str]:
    source = _row_dict(row)
    name = _pick(source, "business_name", "Business Name", "name")
    city = _pick(source, "city", "City")
    phone = _pick(source, "phone", "Phone")
    website = _pick(source, "website_url", "Website URL", "website")
    maps_url = _pick(source, "google_maps_url", "Google Maps URL", "maps_url")
    rating = _pick(source, "rating", "Rating", "google_rating")
    reviews = _pick(source, "reviews", "Reviews", "google_review_count")
    place_id = _pick(source, "place_id", "Place ID")
    email = _pick(source, "email", "Email")
    category = _pick(source, "category", "Category", "Google category")
    address = _pick(source, "address", "Address")
    status = _pick(source, "business_status", "Business Status")

    base: Dict[str, str] = {
        "business_name": name,
        "category": category,
        "city": city,
        "address": address,
        "phone": phone,
        "email": email,
        "website_url": website,
        "google_maps_url": maps_url,
        "rating": rating,
        "reviews": reviews,
        "place_id": place_id,
        "business_status": status,
        "source_query": source_query,
        "source_geo": source_geo,
        "source_search": source_search,
        "source_batch": config.batch,
        "vertical": config.vertical,
    }
    place_key = make_place_key(base)
    base["place_key"] = place_key
    base["duplicate_key"] = place_key if not place_id else f"place:{place_id}"

    base.update(
        {
            "Business Name": name,
            "Owner Name": _pick(source, "owner_name", "Owner Name"),
            "Vertical": config.vertical,
            "City": city,
            "Phone": phone,
            "Email": email,
            "Website URL": website,
            "Google Maps URL": maps_url,
            "Reviews": reviews,
            "Rating": rating,
            "Place Key": place_key,
            "Place ID": place_id,
            "Source Query": source_query,
            "Source Geo": source_geo,
            "Source Batch": config.batch,
            "Source Search": source_search,
        }
    )
    return base


def dedupe_search_rows(rows: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    kept: List[Dict[str, str]] = []
    duplicate_reasons: Counter[str] = Counter()
    seen_place_ids: set[str] = set()
    seen_phones: set[str] = set()
    seen_domains: set[str] = set()
    seen_name_cities: set[str] = set()

    for source in rows:
        row = {str(key): _clean(value) for key, value in dict(source).items()}
        place_id = _pick(row, "place_id", "Place ID")
        phone = normalize_phone(_pick(row, "phone", "Phone"))
        domain = normalize_domain(_pick(row, "website_url", "Website URL"))
        if domain in GENERIC_DOMAINS:
            domain = ""
        name_city = ""
        name = slug(_pick(row, "business_name", "Business Name"))
        city = slug(_pick(row, "city", "City"))
        if name and city:
            name_city = f"{name}:{city}"

        reason = ""
        if place_id and place_id in seen_place_ids:
            reason = "duplicate_place_id"
        elif phone and phone in seen_phones:
            reason = "duplicate_phone"
        elif domain and domain in seen_domains:
            reason = "duplicate_domain"
        elif name_city and name_city in seen_name_cities:
            reason = "duplicate_name_city"

        if reason:
            duplicate_reasons[reason] += 1
            continue

        kept.append(row)
        if place_id:
            seen_place_ids.add(place_id)
        if phone:
            seen_phones.add(phone)
        if domain:
            seen_domains.add(domain)
        if name_city:
            seen_name_cities.add(name_city)

    return kept, dict(duplicate_reasons)


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms if term)


def _matched_terms(text: str, terms: Iterable[str]) -> Tuple[str, ...]:
    lowered = text.lower()
    return tuple(sorted({term for term in terms if term and term.lower() in lowered}))


def prefilter_row(row: Mapping[str, Any], config: SearchConfig) -> PrefilterResult:
    boost_text = " ".join(
        [
            _pick(row, "business_name", "Business Name"),
            _pick(row, "category", "Category"),
        ]
    ).lower()
    text = " ".join(
        [
            _pick(row, "business_name", "Business Name"),
            _pick(row, "category", "Category"),
            _pick(row, "city", "City"),
            _pick(row, "address", "Address"),
            _pick(row, "source_query", "Source Query"),
            _pick(row, "source_geo", "Source Geo"),
            _pick(row, "website_url", "Website URL"),
        ]
    ).lower()
    boosts = _matched_terms(boost_text, config.boost_terms or [])

    if not normalize_phone(_pick(row, "phone", "Phone")):
        return PrefilterResult(True, "missing_phone", boosts)

    if _contains_any(text, FRANCHISE_OR_CHAIN_TERMS) or _contains_any(text, ["franchise", "national chain"]):
        return PrefilterResult(True, "franchise_or_chain", boosts)

    if _contains_any(text, NURSERY_GARDEN_TERMS) and not boosts:
        return PrefilterResult(True, "nursery_or_garden_center", boosts)

    if _contains_any(text, TREE_ONLY_TERMS) and not boosts:
        return PrefilterResult(True, "tree_service_only", boosts)

    if _contains_any(text, SNOW_ONLY_TERMS) and not boosts:
        return PrefilterResult(True, "snow_only", boosts)

    if _contains_any(text, FERTILIZATION_ONLY_TERMS) and not boosts:
        return PrefilterResult(True, "lawn_fertilization_only", boosts)

    if _contains_any(text, COMMERCIAL_MAINTENANCE_TERMS) and not boosts:
        return PrefilterResult(True, "commercial_maintenance_only", boosts)

    for term in config.exclude_terms or []:
        lowered = term.lower()
        if lowered in {"franchise", "national chain", "garden center", "nursery", "tree service"}:
            continue
        if lowered and lowered in text and not boosts:
            return PrefilterResult(True, f"excluded_term:{term}", boosts)

    website = _pick(row, "website_url", "Website URL")
    if not website and not config.allow_missing_website:
        return PrefilterResult(True, "missing_website", boosts)

    rating = _float_or_default(_pick(row, "rating", "Rating"), -1.0)
    if rating >= 0 and config.min_rating and rating < config.min_rating:
        return PrefilterResult(True, "rating_below_min", boosts)

    reviews_raw = _pick(row, "reviews", "Reviews")
    reviews = _int_or_default(reviews_raw, -1)
    strong_missing_site_fit = bool(config.allow_missing_website and not website and boosts)
    if reviews >= 0 and config.min_reviews and reviews < config.min_reviews and not strong_missing_site_fit:
        return PrefilterResult(True, "review_count_below_min", boosts)

    return PrefilterResult(False, "", boosts)


def write_csv_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    rows = [dict(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(CSV_FIELD_ORDER)
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def collect_raw_rows(
    config: SearchConfig,
    *,
    api_key: str,
    sleep_seconds: float,
    warnings: List[str],
    errors: List[str],
    max_searches: Optional[int] = None,
    max_details: Optional[int] = None,
) -> Tuple[List[Dict[str, str]], List[str], Dict[str, int]]:
    raw_rows: List[Dict[str, str]] = []
    queries_run: List[str] = []
    duplicate_reason_counts: Counter[str] = Counter()
    seen_place_ids: set[str] = set()
    details_count = 0
    details_cap_reported = False

    expanded_searches = expand_searches(config)
    searches = expanded_searches
    if max_searches is not None:
        searches = expanded_searches[:max_searches]
    _progress(f"Total query×geo searches: {len(expanded_searches)}")
    if len(searches) != len(expanded_searches):
        _progress(f"Search cap active: running {len(searches)} of {len(expanded_searches)} searches")

    for search_index, search in enumerate(searches, start=1):
        queries_run.append(search.search_text)
        _progress(f"[SEARCH {search_index}/{len(searches)}] {search.search_text}")
        try:
            places = text_search(api_key, search.search_text, search.max_results)
        except Exception as exc:
            errors.append(f"Text search failed for {search.search_text!r}: {exc}")
            _progress(f"[SEARCH {search_index}/{len(searches)}] result count: 0")
            continue
        _progress(f"[SEARCH {search_index}/{len(searches)}] result count: {len(places)}")

        for place in places:
            place_id = _pick(place, "id", "place_id")
            if not place_id:
                warnings.append(f"Search result missing place id for {search.search_text!r}")
                continue
            if place_id in seen_place_ids:
                duplicate_reason_counts["duplicate_place_id_early"] += 1
                continue
            seen_place_ids.add(place_id)
            if max_details is not None and details_count >= max_details:
                if not details_cap_reported:
                    warnings.append(f"Hit --max-details={max_details}; skipped remaining Place Details calls")
                    _progress(f"[DETAILS] hit max-details={max_details}; skipping remaining Place Details calls")
                    details_cap_reported = True
                continue
            details_count += 1
            try:
                details = place_details(api_key, place_id)
                lead = normalize_place(details, search.search_text)
                row = normalize_search_row(
                    _row_dict(lead),
                    config=config,
                    source_query=search.source_query,
                    source_geo=search.source_geo,
                    source_search=search.search_text,
                )
                if not row.get("business_name"):
                    warnings.append(f"Place details missing business name for place_id={place_id}")
                    continue
                raw_rows.append(row)
            except Exception as exc:
                errors.append(f"Place details failed for place_id={place_id}: {exc}")
                continue
            finally:
                if details_count and details_count % 10 == 0:
                    _progress(f"[DETAILS] raw={len(raw_rows)} details={details_count}")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return raw_rows, queries_run, dict(duplicate_reason_counts)


def run_search_batch(
    *,
    config_path: Path,
    batch: str = "",
    root: Path = Path("."),
    api_key: Optional[str] = None,
    no_screenshots: bool = True,
    sleep_seconds: float = 0.15,
    max_searches: Optional[int] = None,
    max_details: Optional[int] = None,
    max_enrich: Optional[int] = None,
) -> SearchRunResult:
    config = load_search_config(config_path)
    if batch and batch != config.batch:
        raise ValueError(f"BATCH={batch} does not match config batch={config.batch}")
    _validate_optional_cap("--max-searches", max_searches)
    _validate_optional_cap("--max-details", max_details)
    _validate_optional_cap("--max-enrich", max_enrich)

    api_key = (api_key or os.getenv("GOOGLE_PLACES_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("Missing GOOGLE_PLACES_API_KEY")

    root = Path(root)
    raw_path = root / "data" / "raw" / f"{config.batch}.csv"
    deduped_path = root / "data" / "deduped" / f"{config.batch}.csv"
    enriched_path = root / "output" / f"enriched_{config.batch}.jsonl"
    report_path = root / "reports" / f"search_{config.batch}.md"
    screenshots_dir = root / "screenshots" / config.batch

    warnings: List[str] = []
    errors: List[str] = []
    raw_rows, queries_run, early_duplicate_reason_counts = collect_raw_rows(
        config,
        api_key=api_key,
        sleep_seconds=sleep_seconds,
        warnings=warnings,
        errors=errors,
        max_searches=max_searches,
        max_details=max_details,
    )
    _progress(f"Raw count: {len(raw_rows)}")
    write_csv_rows(raw_path, raw_rows)

    deduped_rows, duplicate_reason_counts = dedupe_search_rows(raw_rows)
    duplicate_reason_counts = {
        **duplicate_reason_counts,
        **{
            reason: duplicate_reason_counts.get(reason, 0) + count
            for reason, count in early_duplicate_reason_counts.items()
        },
    }
    _progress(f"Deduped count: {len(deduped_rows)}")
    _progress(f"Duplicate reason counts: {_inline_counts(duplicate_reason_counts)}")
    write_csv_rows(deduped_path, deduped_rows)

    passed_rows: List[Dict[str, str]] = []
    skip_reason_counts: Counter[str] = Counter()
    for row in deduped_rows:
        gate = prefilter_row(row, config)
        if gate.should_skip:
            skip_reason_counts[gate.reason or "unknown"] += 1
            continue
        row = dict(row)
        if gate.boost_terms:
            row["icp_boost_terms"] = ", ".join(gate.boost_terms)
        passed_rows.append(row)
    _progress(f"ICP pass count: {len(passed_rows)}; skip count: {sum(skip_reason_counts.values())}")
    _progress(f"ICP skip reason counts: {_inline_counts(skip_reason_counts)}")

    enriched_records: List[Dict[str, Any]] = []
    rows_to_enrich = passed_rows
    if max_enrich is not None:
        rows_to_enrich = passed_rows[:max_enrich]
        if len(passed_rows) > len(rows_to_enrich):
            warnings.append(f"Hit --max-enrich={max_enrich}; skipped remaining enrichment rows")
            _progress(f"Enrichment cap active: {len(rows_to_enrich)} of {len(passed_rows)} rows")
    for enrich_index, row in enumerate(rows_to_enrich, start=1):
        _progress(f"[ENRICH {enrich_index}/{len(rows_to_enrich)}] {_pick(row, 'business_name', 'Business Name') or 'unknown'}")
        try:
            enriched_records.append(
                enrich_row(row, screenshots_dir=screenshots_dir, take_shots=not no_screenshots)
            )
        except Exception as exc:
            errors.append(f"Enrichment failed for {row.get('business_name') or row.get('Business Name')}: {exc}")
    write_jsonl(enriched_path, enriched_records)

    top_candidate_names = [
        _pick(row, "business_name", "Business Name")
        for row in passed_rows[:10]
        if _pick(row, "business_name", "Business Name")
    ]
    result = SearchRunResult(
        batch=config.batch,
        vertical=config.vertical,
        config_path=config_path,
        raw_path=raw_path,
        deduped_path=deduped_path,
        enriched_path=enriched_path,
        report_path=report_path,
        queries_run=queries_run,
        geo_slices=list(config.geo_slices),
        raw_count=len(raw_rows),
        deduped_count=len(deduped_rows),
        icp_pass_count=len(passed_rows),
        enriched_count=len(enriched_records),
        skipped_count=sum(skip_reason_counts.values()),
        skip_reason_counts=dict(skip_reason_counts),
        duplicate_reason_counts=dict(duplicate_reason_counts),
        top_candidate_names=top_candidate_names,
        warnings=warnings,
        errors=errors,
    )
    write_search_report(result)
    _progress(f"Report: {result.report_path}")
    _progress(f"Next command: make batch BATCH={result.batch} BATCH_SIZE=5 LIMIT=10")
    return result


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _count_lines(items: Mapping[str, int]) -> List[str]:
    if not items:
        return ["- none"]
    return [f"- {key}: {value}" for key, value in sorted(items.items())]


def write_search_report(result: SearchRunResult) -> None:
    result.report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Search Batch: {result.batch}",
        "",
        f"- Batch name: {result.batch}",
        f"- Vertical: {result.vertical}",
        f"- Config path: {_relative(result.config_path)}",
        "",
        "## Search Inputs",
        "",
        "Queries run:",
    ]
    lines.extend(f"- {query}" for query in result.queries_run)
    lines.extend(["", "Geo slices used:"])
    lines.extend(f"- {geo}" for geo in result.geo_slices)
    lines.extend(
        [
            "",
            "## Counts",
            "",
            f"- Raw result count: {result.raw_count}",
            f"- Deduped count: {result.deduped_count}",
            f"- ICP pass count: {result.icp_pass_count}",
            f"- Enriched count: {result.enriched_count}",
            f"- Skipped count: {result.skipped_count}",
            "",
            "Skip reasons count:",
        ]
    )
    lines.extend(_count_lines(result.skip_reason_counts))
    lines.extend(["", "Duplicate reasons count:"])
    lines.extend(_count_lines(result.duplicate_reason_counts))
    lines.extend(["", "## Top Candidates", ""])
    lines.extend(f"- {name}" for name in result.top_candidate_names[:10])
    if not result.top_candidate_names:
        lines.append("- none")
    lines.extend(["", "## Warnings And Errors", "", "Warnings:"])
    lines.extend(f"- {warning}" for warning in result.warnings)
    if not result.warnings:
        lines.append("- none")
    lines.extend(["", "Errors:"])
    lines.extend(f"- {error}" for error in result.errors)
    if not result.errors:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            f"- Raw results: {_relative(result.raw_path)}",
            f"- Deduped results: {_relative(result.deduped_path)}",
            f"- Enriched JSONL: {_relative(result.enriched_path)}",
            f"- Search report: {_relative(result.report_path)}",
            "",
            "Next command:",
            f"make batch BATCH={result.batch} BATCH_SIZE=5 LIMIT=10",
            "",
        ]
    )
    result.report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Search config JSON path")
    parser.add_argument("--batch", default="", help="Optional batch guard; must match config batch")
    parser.add_argument("--no-screenshots", action="store_true", help="Skip screenshots during enrichment")
    parser.add_argument("--sleep", type=float, default=0.15, help="Seconds between Google/detail calls")
    parser.add_argument("--max-searches", type=int, default=None, help="Optional cap on expanded query×geo searches")
    parser.add_argument("--max-details", type=int, default=None, help="Optional cap on Place Details calls")
    parser.add_argument("--max-enrich", type=int, default=None, help="Optional cap on enrichment rows")
    args = parser.parse_args()

    try:
        result = run_search_batch(
            config_path=Path(args.config),
            batch=args.batch,
            api_key=os.getenv("GOOGLE_PLACES_API_KEY", ""),
            no_screenshots=args.no_screenshots,
            sleep_seconds=args.sleep,
            max_searches=args.max_searches,
            max_details=args.max_details,
            max_enrich=args.max_enrich,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(f"[DONE] Raw results: {result.raw_count} -> {result.raw_path}")
    print(f"[DONE] Deduped: {result.deduped_count} -> {result.deduped_path}")
    print(f"[DONE] ICP pass: {result.icp_pass_count}")
    print(f"[DONE] Enriched: {result.enriched_count} -> {result.enriched_path}")
    print(f"[DONE] Report: {result.report_path}")
    print(f"[NEXT] make batch BATCH={result.batch} BATCH_SIZE=5 LIMIT=10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
