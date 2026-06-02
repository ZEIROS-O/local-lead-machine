from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


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


@dataclass(frozen=True)
class CanonicalLead:
    business_name: str
    vertical: str
    address: str
    city: str
    phone: str
    website: str
    google_rating: Optional[float]
    google_review_count: Optional[int]
    maps_url: str
    source_batch: str
    place_id_or_unique_key: str
    source_query: str = ""
    category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def clean(value: Any) -> str:
    return str(value or "").strip()


def pick(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return clean(value)
    return ""


def safe_float(value: Any) -> Optional[float]:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    if value in ("", None):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_domain(url: str) -> str:
    url = clean(url).lower()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = re.sub(r"^www\.", "", host)
    return host


def slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_lead(
    row: Dict[str, Any],
    *,
    fallback_vertical: str = "service",
    source_batch: str = "",
) -> CanonicalLead:
    business_name = pick(row, "business_name", "Business Name", "name")
    vertical = pick(row, "vertical", "Vertical") or clean(fallback_vertical)
    address = pick(row, "address", "Address")
    city = pick(row, "city", "City")
    phone = pick(row, "phone", "Phone")
    website = pick(row, "website", "website_url", "Website URL")
    maps_url = pick(row, "maps_url", "google_maps_url", "Google Maps URL")
    place_id = pick(row, "place_id", "Place ID", "place_id_or_unique_key")
    duplicate_key = pick(row, "duplicate_key", "Duplicate Key")
    source_query = pick(row, "source_query", "Source Query")
    category = pick(row, "category", "Category", "Google category")

    provisional = CanonicalLead(
        business_name=business_name,
        vertical=vertical,
        address=address,
        city=city,
        phone=phone,
        website=website,
        google_rating=safe_float(pick(row, "google_rating", "rating", "Rating")),
        google_review_count=safe_int(pick(row, "google_review_count", "reviews", "Reviews")),
        maps_url=maps_url,
        source_batch=source_batch or pick(row, "source_batch", "Source Batch") or source_query,
        place_id_or_unique_key=place_id,
        source_query=source_query,
        category=category,
    )

    if place_id:
        key = place_id
    elif duplicate_key:
        key = duplicate_key
    else:
        keys = row_dedupe_keys(provisional)
        key = keys[0].split(":", 1)[1] if keys else slug(business_name)

    return CanonicalLead(
        business_name=business_name,
        vertical=vertical,
        address=address,
        city=city,
        phone=phone,
        website=website,
        google_rating=provisional.google_rating,
        google_review_count=provisional.google_review_count,
        maps_url=maps_url,
        source_batch=provisional.source_batch,
        place_id_or_unique_key=key,
        source_query=source_query,
        category=category,
    )


def row_dedupe_keys(lead: CanonicalLead) -> List[str]:
    keys: List[str] = []

    if lead.place_id_or_unique_key and not str(lead.place_id_or_unique_key).startswith(
        ("phone:", "domain:", "name_city:")
    ):
        keys.append(f"place:{lead.place_id_or_unique_key}")

    phone = normalize_phone(lead.phone)
    if phone:
        keys.append(f"phone:{phone}")

    domain = normalize_domain(lead.website)
    if domain and domain not in GENERIC_DOMAINS:
        keys.append(f"domain:{domain}")

    name = slug(lead.business_name)
    city = slug(lead.city)
    if name and city:
        keys.append(f"name_city:{name}:{city}")

    return keys

