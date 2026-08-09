#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import AbstractSet, Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from enrich_leads import (
    SLOW_ENRICH_SECONDS,
    JsonlAppender,
    build_enrichment_error_record,
    elapsed_seconds,
    enrichment_log_fields,
    enrich_row,
    enrich_rows_concurrently,
    format_elapsed,
    resolve_enrich_workers,
    resolve_per_lead_timeout,
)
from places_search import getenv_int, normalize_place, place_details, text_search_paged
from lead_machine.compliance import ComplianceError, load_configured_suppression_index
from lead_machine.corpus_index import (
    CROSS_BATCH_DEDUPE_CLASSES,
    PlaceRegistry,
    is_toll_free,
    primary_key,
    registry_path_for,
)
from lead_machine.review_velocity import ReviewSnapshotStore
from lead_machine.schema import GENERIC_DOMAINS, normalize_domain, normalize_phone, slug
from lead_machine.verticals import resolve_profile


DEFAULT_REVIEW_SNAPSHOT_DB = "run_state/review_velocity.sqlite3"

# Place Details concurrency (PLACES_DETAILS_WORKERS). See resolve_details_workers.
DEFAULT_DETAILS_WORKERS = 6
MAX_DETAILS_WORKERS = 8

# Default ceiling on billed Place Details calls per run (MAX_DETAILS_CALLS_PER_RUN).
# See resolve_max_details. Same name and same number as the cap the legacy
# places_search CLI already reads, so the knob means one thing repo-wide.
DEFAULT_MAX_DETAILS_CALLS_PER_RUN = 400

REQUIRED_CONFIG_FIELDS = ["batch", "vertical", "location", "limit_per_query", "queries"]

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

# The nursery / tree / snow / fertilization / commercial-maintenance exclusions
# that used to be module constants here are now `adjacency_exclusions` on the
# hardscaping profile. They were applied to every vertical, so a plumber or a
# water-restoration firm saying "property maintenance" or "facility maintenance"
# — ordinary vocabulary in those markets — was dropped before enrichment.


def _load_geo_slices_ref(ref: str, *, source_path: Path) -> List[str]:
    cleaned = ref.strip()
    if not cleaned:
        return []
    ref_path = Path(cleaned)
    if not ref_path.suffix:
        ref_path = ref_path.with_suffix(".json")
    if not ref_path.is_absolute():
        if ref_path.parent == Path("."):
            ref_path = source_path.parent / "geo_slices" / ref_path
        else:
            ref_path = source_path.parent / ref_path
    try:
        data = json.loads(ref_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Search config geo_slices_ref not found: {cleaned}") from exc
    return _string_list(data)


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
    page_size: int = 0
    max_pages_per_query: int = 1
    max_results_per_query: Optional[int] = None
    source_path: Path = Path("")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source_path: Path) -> "SearchConfig":
        missing = [field for field in REQUIRED_CONFIG_FIELDS if field not in data or data.get(field) in ("", None, [])]
        if missing:
            raise ValueError(f"Search config missing required fields: {', '.join(missing)}")

        queries = _string_list(data.get("queries"))
        geo_slices = _string_list(data.get("geo_slices"))
        if not geo_slices and data.get("geo_slices_ref"):
            geo_slices = _load_geo_slices_ref(str(data.get("geo_slices_ref") or ""), source_path=source_path)
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
        limit_per_query = min(limit_per_query, 20)

        # page_size defaults to limit_per_query so legacy configs keep their per-call
        # result count; capped at the API's 20-per-page ceiling.
        if data.get("page_size") in ("", None):
            page_size = limit_per_query
        else:
            try:
                page_size = int(data.get("page_size"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Search config field page_size must be an integer") from exc
        page_size = max(1, min(page_size, 20))

        # max_pages_per_query defaults to 1 -> identical single-page behavior/cost for
        # legacy configs that never declared it.
        max_pages = _int_or_default(data.get("max_pages_per_query"), 1)
        if max_pages <= 0:
            max_pages = 1

        if data.get("max_results_per_query") in ("", None):
            max_results_per_query: Optional[int] = None
        else:
            max_results_per_query = _int_or_default(
                data.get("max_results_per_query"), page_size * max_pages
            )

        return cls(
            batch=str(data.get("batch") or "").strip(),
            vertical=str(data.get("vertical") or "").strip(),
            location=str(data.get("location") or "").strip(),
            limit_per_query=limit_per_query,
            queries=queries,
            geo_slices=geo_slices,
            min_rating=_float_or_default(data.get("min_rating"), 0.0),
            min_reviews=_int_or_default(data.get("min_reviews"), 0),
            allow_missing_website=bool(data.get("allow_missing_website", True)),
            exclude_terms=_string_list(data.get("exclude_terms")),
            boost_terms=_string_list(data.get("boost_terms")),
            page_size=page_size,
            max_pages_per_query=max_pages,
            max_results_per_query=max_results_per_query,
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
            "page_size": self.effective_page_size,
            "max_pages_per_query": self.max_pages_per_query,
            "max_results_per_query": self.max_results_per_query,
        }

    @property
    def effective_page_size(self) -> int:
        """Per-call page size, falling back to limit_per_query for legacy configs."""
        return self.page_size if self.page_size > 0 else min(self.limit_per_query, 20)


@dataclass(frozen=True)
class ExpandedSearch:
    source_query: str
    source_geo: str
    search_text: str
    max_results: int
    page_size: int = 0
    max_pages: int = 1
    total_max_results: Optional[int] = None


@dataclass(frozen=True)
class SearchEfficiency:
    searches_run: int = 0
    page_size: int = 0
    max_pages_per_query: int = 1
    pages_fetched: int = 0
    total_places_returned: int = 0
    unique_raw_count: int = 0
    early_duplicate_count: int = 0


@dataclass(frozen=True)
class CorpusDedupe:
    """What the cross-batch registry did to this run.

    Reported because the saving is otherwise invisible: a batch that skips 300
    already-known businesses looks identical on the counts to a batch whose
    queries found nothing, and the first is the system working while the second
    is a config to fix. An operator has to be able to tell them apart.
    """

    enabled: bool = True
    registry_path: str = ""
    known_count: int = 0
    refresh_older_than_days: Optional[int] = None
    skipped_before_details: int = 0
    skipped_after_collection: int = 0
    registered: int = 0


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
    efficiency: SearchEfficiency = SearchEfficiency()
    corpus_dedupe: CorpusDedupe = CorpusDedupe()


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
    page_size = config.effective_page_size
    max_pages = config.max_pages_per_query
    total_max = config.max_results_per_query or page_size * max_pages
    expanded: List[ExpandedSearch] = []
    for query in config.queries:
        for geo in config.geo_slices:
            expanded.append(
                ExpandedSearch(
                    source_query=query,
                    source_geo=geo,
                    search_text=replace_location(query, base_location=config.location, geo=geo),
                    max_results=config.limit_per_query,
                    page_size=page_size,
                    max_pages=max_pages,
                    total_max_results=total_max,
                )
            )
    return expanded


def make_place_key(row: Mapping[str, Any]) -> str:
    """The row's identity key: place_id → phone → domain → name_city.

    The implementation moved to `lead_machine.corpus_index.primary_key` when the
    cross-batch registry landed, and this stayed as the name every caller here
    already used. One implementation on purpose: a registry that derived keys
    slightly differently from the in-batch dedupe would be a second opinion
    about what "the same business" is, and the stricter of the two would quietly
    stop excluding anything.
    """
    return primary_key(row)


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


def dedupe_search_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    prior_keys: Optional[AbstractSet[str]] = None,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Collapse a batch to one row per business, optionally against the corpus.

    `prior_keys` are the namespaced keys of businesses earlier batches already
    paid for (see `lead_machine.corpus_index`). They are checked *before* the
    in-batch classes so the reason counts stay honest about which duplicates
    were cross-batch. The early skip in `collect_raw_rows` already catches the
    place_id case before any billing; this pass exists for identity drift —
    Google re-issuing a place_id for a business we already know by phone or
    name+city.

    `domain` is deliberately NOT a cross-batch class. It is safe within one
    batch, where one metro rarely holds two of a chain, and unsafe across the
    corpus, where a shared domain is the normal shape of a franchise: measured
    on data/deduped, 38 domains are shared by differently-named businesses in
    different batches, covering 88 distinct prospects — five separate Wayne
    Dalton dealers, five Ace Handyman franchisees, three unrelated businesses
    behind one template host. Excluding on domain would silently delete the
    second, third and fourth of each, and because a row dropped here never
    reaches the registry it would be re-discovered and re-billed on every
    subsequent run: pay every time, never get the lead.
    """
    prior_keys = set(prior_keys or ())
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

        classes = (
            ("place_id", place_id, f"place:{place_id}", seen_place_ids),
            ("phone", phone, f"phone:{phone}", seen_phones),
            ("domain", domain, f"domain:{domain}", seen_domains),
            ("name_city", name_city, f"name_city:{name_city}", seen_name_cities),
        )

        reason = ""
        for name_of_class, value, namespaced, _seen in classes:
            if name_of_class not in CROSS_BATCH_DEDUPE_CLASSES:
                continue
            if name_of_class == "phone" and is_toll_free(value):
                continue
            if value and namespaced in prior_keys:
                reason = f"duplicate_{name_of_class}_prior_batch"
                break
        if not reason:
            for name_of_class, value, _namespaced, seen in classes:
                if value and value in seen:
                    reason = f"duplicate_{name_of_class}"
                    break

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

    profile = resolve_profile(config.vertical, source_query=_pick(row, "source_query", "Source Query"))
    for exclusion in profile.adjacency_exclusions:
        if _contains_any(text, list(exclusion.terms)) and not boosts:
            return PrefilterResult(True, exclusion.reason, boosts)

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


class DetailsBudget:
    """The hard ceiling on billed Place Details calls, held as one invariant.

    Every Place Details call costs money, so ``--max-details`` is a spend
    ceiling and not a hint. The pre-concurrency code spelled it as
    ``if details_count >= max_details: ... ; details_count += 1`` -- a
    check-then-act that is only safe because one thread ran both statements.
    Rather than leave that safety as a property of the call site, the reserve
    and the increment happen together under a lock here, so no caller can
    over-fetch by moving the reserve onto a worker thread.
    """

    def __init__(self, cap: Optional[int]) -> None:
        self._cap = cap
        self._count = 0
        self._lock = threading.Lock()

    @property
    def cap(self) -> Optional[int]:
        return self._cap

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._cap is not None and self._count >= self._cap

    def reserve(self) -> bool:
        """Claim one billed call. False once the cap is spent."""
        with self._lock:
            if self._cap is not None and self._count >= self._cap:
                return False
            self._count += 1
            return True


def resolve_max_details(max_details: Optional[int]) -> Optional[int]:
    """The effective ceiling on billed Place Details calls for one run.

    `DetailsBudget` was always a correct ceiling; what was missing was a
    number to give it. `--max-details` is optional and `run_agent_pipeline.py`
    never passes it, so `None` reached `DetailsBudget` and meant unlimited --
    on the paid path, with the per-call pacing sleep removed and six calls in
    flight, so an unattended run could bill without a brake roughly six times
    faster than the code this default replaces. An unspecified cap now means
    the configured default, never unlimited.

    Precedence, strictest source first:

    * an explicit `max_details` wins outright, including an explicit ``0``
      ("spend nothing" is an instruction, not a missing value);
    * otherwise `MAX_DETAILS_CALLS_PER_RUN`;
    * otherwise `DEFAULT_MAX_DETAILS_CALLS_PER_RUN`.

    Unlimited is still reachable, but only deliberately and only through the
    env: a negative `MAX_DETAILS_CALLS_PER_RUN` (e.g. ``-1``) restores the old
    behaviour. It cannot be a `--max-details` value, because
    `_validate_optional_cap` rejects a negative flag and ``0`` is already
    taken.
    """
    if max_details is not None:
        return max_details
    configured = getenv_int("MAX_DETAILS_CALLS_PER_RUN", DEFAULT_MAX_DETAILS_CALLS_PER_RUN)
    return None if configured < 0 else configured


def resolve_details_workers() -> int:
    """How many Place Details calls to keep in flight.

    Details lookups are one small HTTPS round-trip each and used to run
    strictly serially with a 0.15s pacing sleep between them, so a search that
    returned 60 places spent most of its wall-clock waiting. A bounded pool
    replaces both the serialisation and the sleep. The ceiling is deliberately
    low: the Places API bills and rate-limits per call, and the point of the
    pool is to hide latency, not to sprint into a 429.
    """
    return max(1, min(getenv_int("PLACES_DETAILS_WORKERS", DEFAULT_DETAILS_WORKERS), MAX_DETAILS_WORKERS))


def collect_raw_rows(
    config: SearchConfig,
    *,
    api_key: str,
    sleep_seconds: float,
    warnings: List[str],
    errors: List[str],
    max_searches: Optional[int] = None,
    max_details: Optional[int] = None,
    prior_place_ids: Optional[AbstractSet[str]] = None,
) -> Tuple[List[Dict[str, str]], List[str], Dict[str, int], SearchEfficiency]:
    raw_rows: List[Dict[str, str]] = []
    queries_run: List[str] = []
    duplicate_reason_counts: Counter[str] = Counter()
    seen_place_ids: set[str] = set()
    # Deliberately a second set rather than seeding `seen_place_ids`: that set
    # feeds `efficiency.unique_raw_count`, which means "distinct places this run
    # actually pulled", and seeding it would inflate the number by the size of
    # the whole corpus. Keeping them apart is also what lets the cross-batch
    # skip carry its own reason key.
    prior_place_ids = set(prior_place_ids or ())
    skipped_prior_place_ids: set[str] = set()
    max_details = resolve_max_details(max_details)
    details_budget = DetailsBudget(max_details)
    details_cap_reported = False
    collected = 0
    pages_fetched_total = 0
    total_places_returned = 0
    details_workers = resolve_details_workers()

    expanded_searches = expand_searches(config)
    searches = expanded_searches
    if max_searches is not None:
        searches = expanded_searches[:max_searches]
    _progress(f"Total query×geo searches: {len(expanded_searches)}")
    _progress(
        f"Search depth: page_size={config.effective_page_size} "
        f"max_pages_per_query={config.max_pages_per_query}"
    )
    if len(searches) != len(expanded_searches):
        _progress(f"Search cap active: running {len(searches)} of {len(expanded_searches)} searches")
    _progress(f"Place Details workers: {details_workers}")

    for search_index, search in enumerate(searches, start=1):
        queries_run.append(search.search_text)
        _progress(f"[SEARCH {search_index}/{len(searches)}] {search.search_text}")
        try:
            places, pages_fetched = text_search_paged(
                api_key,
                search.search_text,
                page_size=search.page_size,
                max_pages=search.max_pages,
                max_results=search.total_max_results,
                sleep_seconds=sleep_seconds,
            )
        except Exception as exc:
            errors.append(f"Text search failed for {search.search_text!r}: {exc}")
            _progress(f"[SEARCH {search_index}/{len(searches)}] result count: 0")
            continue
        pages_fetched_total += pages_fetched
        total_places_returned += len(places)
        _progress(
            f"[SEARCH {search_index}/{len(searches)}] result count: {len(places)} "
            f"(pages={pages_fetched})"
        )

        # Submit in place order, collect in place order. Every mutation of the
        # shared state below (seen_place_ids, raw_rows, warnings, errors,
        # duplicate_reason_counts) stays on this thread; the workers only do
        # the network call, exactly like enrich_rows_concurrently's contract.
        # Row order is observable -- raw rows go straight to
        # data/raw/<batch>.csv and dedupe_search_rows is first-wins -- so
        # completion order would change which duplicate survives.
        submitted: List[Tuple[str, Any]] = []
        pool = ThreadPoolExecutor(max_workers=details_workers)
        try:
            try:
                for place in places:
                    place_id = _pick(place, "id", "place_id")
                    if not place_id:
                        warnings.append(f"Search result missing place id for {search.search_text!r}")
                        continue
                    if place_id in seen_place_ids or place_id in skipped_prior_place_ids:
                        duplicate_reason_counts["duplicate_place_id_early"] += 1
                        continue
                    # The cross-batch skip, and the whole point of the registry:
                    # it lands *before* details_budget.reserve() below, so a
                    # business an earlier batch already paid for is never billed
                    # for Place Details again and never re-crawled. Counted once
                    # per distinct place_id, so the number is exactly the number
                    # of billed calls saved; later repeats of the same id inside
                    # this batch fall through to duplicate_place_id_early above,
                    # which keeps that key meaning what it always meant.
                    if place_id in prior_place_ids:
                        duplicate_reason_counts["duplicate_place_id_prior_batch"] += 1
                        skipped_prior_place_ids.add(place_id)
                        continue
                    seen_place_ids.add(place_id)
                    # Reserve the billed slot before the call is queued, never
                    # after. `details_budget.reserve()` is the only place the
                    # counter moves, and it moves atomically -- the cap is a
                    # money ceiling, so it must not depend on the caller
                    # happening to be single-threaded.
                    if not details_budget.reserve():
                        if not details_cap_reported:
                            # The cap may have come from --max-details or from
                            # MAX_DETAILS_CALLS_PER_RUN, so name both escapes
                            # rather than the flag the operator may not have
                            # passed.
                            warnings.append(
                                f"Hit max-details={max_details}; skipped remaining Place Details "
                                "calls. Raise --max-details or MAX_DETAILS_CALLS_PER_RUN to go further."
                            )
                            _progress(f"[DETAILS] hit max-details={max_details}; skipping remaining Place Details calls")
                            details_cap_reported = True
                        continue
                    submitted.append((place_id, pool.submit(place_details, api_key, place_id)))

                for place_id, future in submitted:
                    try:
                        details = future.result()
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
                        collected += 1
                        if collected % 10 == 0:
                            _progress(
                                f"[DETAILS] raw={len(raw_rows)} details={details_budget.count}"
                            )
            except KeyboardInterrupt:
                # Every place is submitted up front, so the default shutdown
                # (wait=True, cancel_futures=False) would drain the whole
                # remaining queue before the interrupt landed. Drop what has
                # not started; the in-flight calls finish in the `finally`,
                # since a thread cannot be killed mid-request.
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        finally:
            pool.shutdown(wait=True)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    efficiency = SearchEfficiency(
        searches_run=len(searches),
        page_size=config.effective_page_size,
        max_pages_per_query=config.max_pages_per_query,
        pages_fetched=pages_fetched_total,
        total_places_returned=total_places_returned,
        unique_raw_count=len(seen_place_ids),
        early_duplicate_count=duplicate_reason_counts.get("duplicate_place_id_early", 0),
    )
    return raw_rows, queries_run, dict(duplicate_reason_counts), efficiency


def run_search_batch(
    *,
    config_path: Path,
    batch: str = "",
    root: Path = Path("."),
    api_key: Optional[str] = None,
    no_screenshots: bool = True,
    rendered_site_check: bool = False,
    sleep_seconds: float = 0.15,
    max_searches: Optional[int] = None,
    max_details: Optional[int] = None,
    max_enrich: Optional[int] = None,
    suppression_index: Optional[AbstractSet[str]] = None,
    review_snapshot_db: Optional[Path | str] = None,
    refresh_older_than_days: Optional[int] = None,
    ignore_corpus: bool = False,
    place_registry_path: Optional[Path | str] = None,
) -> SearchRunResult:
    config = load_search_config(config_path)
    if batch and batch != config.batch:
        raise ValueError(f"BATCH={batch} does not match config batch={config.batch}")
    _validate_optional_cap("--max-searches", max_searches)
    _validate_optional_cap("--max-details", max_details)
    _validate_optional_cap("--max-enrich", max_enrich)
    _validate_optional_cap("--refresh-older-than", refresh_older_than_days)

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

    # The corpus ledger is read before the first billed call, because that is
    # the only moment at which knowing a business is already ours can still save
    # anything. `--ignore-corpus` skips the read entirely for a deliberate
    # re-scrape.
    registry = PlaceRegistry(place_registry_path or registry_path_for(root))
    prior_place_ids: set[str] = set()
    prior_keys: set[str] = set()
    if not ignore_corpus:
        # exclude_batch: this batch's own history never blocks it, so a rerun
        # under --overwrite or after a crash still searches instead of silently
        # returning nothing.
        prior_place_ids = registry.known_place_ids(refresh_older_than_days, exclude_batch=config.batch)
        prior_keys = registry.known_keys(refresh_older_than_days, exclude_batch=config.batch)
        _progress(
            f"Corpus registry: {len(registry)} known businesses "
            f"({len(prior_place_ids)} with a place_id) from {registry.path}"
        )
        if registry.malformed_lines:
            warnings.append(
                f"Place registry has {registry.malformed_lines} unreadable line(s); "
                "rerun `python -m lead_machine.corpus_index --rebuild` to heal it."
            )
        if refresh_older_than_days is not None:
            _progress(
                f"Corpus refresh window: re-admitting businesses last seen more than "
                f"{refresh_older_than_days} day(s) ago"
            )
    else:
        _progress("Corpus registry: ignored (--ignore-corpus)")

    raw_rows, queries_run, early_duplicate_reason_counts, efficiency = collect_raw_rows(
        config,
        api_key=api_key,
        sleep_seconds=sleep_seconds,
        warnings=warnings,
        errors=errors,
        max_searches=max_searches,
        max_details=max_details,
        prior_place_ids=prior_place_ids,
    )
    _progress(f"Raw count: {len(raw_rows)}")
    write_csv_rows(raw_path, raw_rows)

    deduped_rows, duplicate_reason_counts = dedupe_search_rows(raw_rows, prior_keys=prior_keys)
    prior_batch_after_collection = sum(
        count for reason, count in duplicate_reason_counts.items() if reason.endswith("_prior_batch")
    )
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

    # Register here, beside the deduped write, not at the end of the run: an
    # interrupt during the long enrichment pass must not lose the record of
    # calls we already paid for and let the next batch pay again. The rows are
    # on disk either way, so a half-enriched batch is re-enrichable from
    # data/deduped/<batch>.csv without touching the Places API.
    #
    # `raw_rows`, not `deduped_rows`: the billed set is every row that got a
    # Place Details call, and `dedupe_search_rows` has just discarded some of
    # them. A row dropped as a cross-batch duplicate is the dangerous case —
    # it was billed, and registering only survivors would leave it unknown, so
    # the next run would discover it, bill it, and discard it again, forever.
    # `record` folds by key, so in-batch repeats still collapse to one entry.
    registered = registry.record(raw_rows, batch=config.batch)
    _progress(f"Registered {registered} business(es) in {registry.path}")

    corpus_dedupe = CorpusDedupe(
        enabled=not ignore_corpus,
        registry_path=_relative(registry.path),
        known_count=len(prior_place_ids),
        refresh_older_than_days=refresh_older_than_days,
        skipped_before_details=early_duplicate_reason_counts.get("duplicate_place_id_prior_batch", 0),
        skipped_after_collection=prior_batch_after_collection,
        registered=registered,
    )

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

    # Review velocity is only ever computable if something banks the counts.
    # This entry point ran without a store, so run_state/review_velocity.sqlite3
    # never accumulated history and every lead scored here reported
    # velocity_band=unknown, source=insufficient_history — a whole signal the
    # rubric asks for and the production path could not supply.
    review_snapshot_store = ReviewSnapshotStore(
        review_snapshot_db or os.getenv("REVIEW_SNAPSHOT_DB", DEFAULT_REVIEW_SNAPSHOT_DB)
    )

    enriched_records: List[Dict[str, Any]] = []
    rows_to_enrich = passed_rows
    if max_enrich is not None:
        rows_to_enrich = passed_rows[:max_enrich]
        if len(passed_rows) > len(rows_to_enrich):
            warnings.append(f"Hit --max-enrich={max_enrich}; skipped remaining enrichment rows")
            _progress(f"Enrichment cap active: {len(rows_to_enrich)} of {len(passed_rows)} rows")

    browser_work = not no_screenshots or rendered_site_check
    workers = resolve_enrich_workers(browser_work=browser_work)
    per_lead_timeout = resolve_per_lead_timeout(workers)
    _progress(f"Enrichment workers: {workers} (per-lead timeout {per_lead_timeout:.0f}s)")

    def _enrich(row: Dict[str, str]) -> Dict[str, Any]:
        return enrich_row(
            row,
            screenshots_dir=screenshots_dir,
            take_shots=not no_screenshots,
            rendered_site_check=rendered_site_check,
            suppression_index=suppression_index,
            review_snapshot_store=review_snapshot_store,
            per_lead_timeout=per_lead_timeout,
        )

    # Records stream to disk as they finish, so an interrupted run keeps the
    # scraping it already paid for instead of losing the batch at the final
    # write.
    with JsonlAppender(enriched_path) as appender:

        def _on_complete(
            enrich_index: int,
            row: Dict[str, str],
            record: Dict[str, Any] | None,
            error: str,
            elapsed: float,
        ) -> None:
            business_name = _pick(row, "business_name", "Business Name") or "unknown"
            website_url = _pick(row, "website_url", "Website URL")
            if record is None:
                errors.append(f"Enrichment failed for {business_name}: {error}")
                # Keep the lead. This entry point used to drop it while
                # enrich_leads kept an error record for the same failure.
                record = build_enrichment_error_record(row, error=error, elapsed=elapsed)
            appender.append(record)
            enriched_records.append(record)
            reported = float(record.get("enrichment", {}).get("elapsed_seconds") or elapsed)
            fields = enrichment_log_fields(record, fallback_error=error)
            _progress(
                f"[ENRICH {enrich_index}/{len(rows_to_enrich)}] {business_name} "
                f"elapsed={format_elapsed(reported)}s "
                f"site_status={fields['site_status']} "
                f"pages_checked={fields['pages_checked']} "
                f"errors={fields['error']}"
            )
            if reported > SLOW_ENRICH_SECONDS:
                _progress(f"[SLOW ENRICH] {business_name} elapsed={format_elapsed(reported)}s website={website_url}")

        enrich_rows_concurrently(
            rows_to_enrich, workers=workers, enrich=_enrich, on_complete=_on_complete
        )

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
        efficiency=efficiency,
        corpus_dedupe=corpus_dedupe,
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

    corpus = result.corpus_dedupe
    if corpus.refresh_older_than_days is None:
        refresh_line = "skip every business the corpus already knows"
    else:
        refresh_line = (
            f"re-admit businesses last seen more than {corpus.refresh_older_than_days} day(s) ago "
            "(--refresh-older-than)"
        )
    lines.extend(
        [
            "",
            "## Cross-Batch Corpus Dedupe",
            "",
            f"- Corpus dedupe: {'enabled' if corpus.enabled else 'disabled (--ignore-corpus)'}",
            f"- Registry: {corpus.registry_path}",
            f"- Known place_ids excluded at run start: {corpus.known_count}",
            f"- Refresh window: {refresh_line}",
            f"- Place Details calls skipped (duplicate_place_id_prior_batch): {corpus.skipped_before_details}",
            f"- Rows dropped post-collection (*_prior_batch): {corpus.skipped_after_collection}",
            f"- Businesses registered by this run: {corpus.registered}",
            "",
            "A high skip count is the machine working, not a thin batch: those are",
            "businesses earlier batches already paid Place Details for and crawled.",
        ]
    )

    eff = result.efficiency
    leads_per_search = result.enriched_count / eff.searches_run if eff.searches_run else 0.0
    leads_per_page = result.enriched_count / eff.pages_fetched if eff.pages_fetched else 0.0
    lines.extend(
        [
            "",
            "## Search Efficiency",
            "",
            f"- Total query×geo searches: {eff.searches_run}",
            f"- Configured page size: {eff.page_size}",
            f"- Max pages per query: {eff.max_pages_per_query}",
            f"- Pages actually fetched: {eff.pages_fetched}",
            f"- Raw results: {eff.total_places_returned}",
            f"- Unique raw results: {eff.unique_raw_count}",
            f"- Deduped results: {result.deduped_count}",
            f"- Early duplicate count: {eff.early_duplicate_count}",
            f"- ICP pass count: {result.icp_pass_count}",
            f"- Enriched count: {result.enriched_count}",
            f"- Unique enriched leads per search: {leads_per_search:.2f}",
            f"- Unique enriched leads per Places page: {leads_per_page:.2f}",
        ]
    )
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
    parser.add_argument(
        "--rendered-site-check",
        action="store_true",
        help="Use Playwright browser rendering to classify loaded/broken/timeout site status during enrichment",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.15,
        help="Seconds between text-search pages and between searches. Place Details calls are no "
             "longer paced by this -- PLACES_DETAILS_WORKERS bounds them instead.",
    )
    parser.add_argument("--max-searches", type=int, default=None, help="Optional cap on expanded query×geo searches")
    parser.add_argument(
        "--max-details", type=int, default=None,
        help="Hard cap on billed Place Details calls. Reserved atomically, so the concurrent "
             "fetch cannot exceed it. Omitting this does not mean unlimited: it falls back to "
             f"MAX_DETAILS_CALLS_PER_RUN (default {DEFAULT_MAX_DETAILS_CALLS_PER_RUN}). Set that "
             "env var negative for genuinely unlimited.",
    )
    parser.add_argument("--max-enrich", type=int, default=None, help="Optional cap on enrichment rows")
    parser.add_argument(
        "--refresh-older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help="Re-admit businesses the corpus registry last saw more than DAYS ago. Unset (the "
             "default) skips every known business, which is what stops the same place_id being "
             "billed for Place Details once per overlapping metro config.",
    )
    parser.add_argument(
        "--ignore-corpus",
        action="store_true",
        help="Ignore run_state/place_registry.jsonl entirely for a deliberate re-scrape. Every "
             "known business is re-searched, re-billed and re-crawled.",
    )
    parser.add_argument(
        "--review-snapshot-db",
        default=os.getenv("REVIEW_SNAPSHOT_DB", DEFAULT_REVIEW_SNAPSHOT_DB),
        help="SQLite path containing longitudinal Google review-count observations",
    )
    args = parser.parse_args()

    try:
        # This is the entry point the pipeline actually runs, so it is the one
        # that has to fail closed. Loading here rather than inside
        # run_search_batch keeps the refusal ahead of the first paid Places call.
        suppression_index = load_configured_suppression_index()
        result = run_search_batch(
            config_path=Path(args.config),
            batch=args.batch,
            api_key=os.getenv("GOOGLE_PLACES_API_KEY", ""),
            no_screenshots=args.no_screenshots,
            rendered_site_check=args.rendered_site_check,
            sleep_seconds=args.sleep,
            max_searches=args.max_searches,
            max_details=args.max_details,
            max_enrich=args.max_enrich,
            suppression_index=suppression_index,
            review_snapshot_db=args.review_snapshot_db,
            refresh_older_than_days=args.refresh_older_than,
            ignore_corpus=args.ignore_corpus,
        )
    except (FileNotFoundError, RuntimeError, ValueError, ComplianceError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Enrichment streams each record to output/enriched_<batch>.jsonl as it
        # completes, so an interrupt here still keeps everything scraped before
        # it landed -- there is just no final report/CSV for this run.
        print("[STOP] Interrupted; partial enriched output was kept on disk.", file=sys.stderr)
        return 1

    print(f"[DONE] Raw results: {result.raw_count} -> {result.raw_path}")
    print(
        f"[DONE] Corpus dedupe: skipped {result.corpus_dedupe.skipped_before_details} known "
        f"business(es) before Place Details; registered {result.corpus_dedupe.registered}"
    )
    print(f"[DONE] Deduped: {result.deduped_count} -> {result.deduped_path}")
    print(f"[DONE] ICP pass: {result.icp_pass_count}")
    print(f"[DONE] Enriched: {result.enriched_count} -> {result.enriched_path}")
    print(f"[DONE] Report: {result.report_path}")
    print(f"[NEXT] make batch BATCH={result.batch} BATCH_SIZE=5 LIMIT=10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
