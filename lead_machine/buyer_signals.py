from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import quote_plus, urlparse

import requests

from .evidence import THIRD_PARTY_TOOLS, _detect_from_text


UNKNOWN_ADS = {
    "google_ad_transparency": "unknown",
    "meta_ad_library": "unknown",
    "lsa_badge": "unknown",
    "errors": [],
}

REVIEW_VELOCITY_THRESHOLDS = {
    "live_min_reviews": 75,
    "live_min_rating": 4.5,
    "steady_min_reviews": 5,
    "steady_min_rating": 4.0,
}

PAID_CRM = {
    "Jobber",
    "Housecall Pro",
    "GHL",
    "ServiceTitan",
    "Thryv",
    "Urable",
    "Booksy",
    "Square Appointments",
    "Mobile Tech RX",
}

ADS_TIMEOUT_SECONDS = 3


def build_buyer_intent(record: Dict[str, Any], *, ads_check: bool = False) -> Dict[str, Any]:
    detected_tools = _detected_tools(record)
    paid_tools = sorted(set(detected_tools) & PAID_CRM)
    has_paid_crm = bool(paid_tools)
    crm_evidence = [f"Detected paid CRM/booking tool: {tool}" for tool in paid_tools]

    review_velocity = _review_velocity(record)
    ads = _ads_signals(record, ads_check=ads_check)

    known_ad_yes = any(
        ads.get(field) == "yes"
        for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge")
    )
    money_signal = has_paid_crm or known_ad_yes or review_velocity["velocity_band"] == "live"

    evidence: List[str] = []
    evidence.extend(crm_evidence)
    if review_velocity["velocity_band"] in {"live", "steady"}:
        evidence.extend(review_velocity["evidence"])
    for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge"):
        if ads.get(field) == "yes":
            evidence.append(f"{field.replace('_', ' ')} detected")

    buyer_intent = {
        "crm_booking_stack": {
            "detected_tools": detected_tools,
            "has_paid_crm": has_paid_crm,
            "evidence": crm_evidence,
        },
        "review_velocity": review_velocity,
        "ads": ads,
        "money_signal": bool(money_signal),
        "evidence": evidence,
    }
    record["buyer_intent"] = buyer_intent
    return buyer_intent


def ensure_buyer_intent(record: Dict[str, Any], *, ads_check: bool = False) -> Dict[str, Any]:
    buyer_intent = record.get("buyer_intent")
    if _valid_buyer_intent(buyer_intent):
        return buyer_intent
    return build_buyer_intent(record, ads_check=ads_check)


def has_money_signal(record: Dict[str, Any]) -> bool:
    buyer_intent = _buyer_intent_from_record(record)
    if not buyer_intent:
        raw = record.get("Money Signal") or record.get("money_signal")
        return str(raw).strip().lower() in {"yes", "true", "1"}
    return bool(buyer_intent.get("money_signal"))


def buyer_intent_summary(record: Dict[str, Any]) -> str:
    buyer_intent = _buyer_intent_from_record(record)
    if not buyer_intent:
        return "money_signal=unknown; tools=none; review_velocity=unknown; ads=unknown"

    stack = buyer_intent.get("crm_booking_stack") or {}
    review_velocity = buyer_intent.get("review_velocity") or {}
    ads = buyer_intent.get("ads") or {}
    tools = stack.get("detected_tools") or []
    if isinstance(tools, str):
        tools = [part.strip() for part in tools.replace(",", ";").split(";") if part.strip()]
    known_ads = {
        key: value
        for key, value in ads.items()
        if key != "errors" and str(value or "").strip().lower() in {"yes", "no"}
    }
    ads_text = ", ".join(f"{key}={value}" for key, value in sorted(known_ads.items())) or "unknown"
    return (
        f"money_signal={'yes' if bool(buyer_intent.get('money_signal')) else 'no'}; "
        f"tools={', '.join(str(tool) for tool in tools) or 'none'}; "
        f"review_velocity={review_velocity.get('velocity_band') or 'unknown'}; "
        f"ads={ads_text}"
    )


def _buyer_intent_from_record(record: Dict[str, Any]) -> Dict[str, Any]:
    buyer_intent = record.get("buyer_intent")
    if isinstance(buyer_intent, dict):
        return buyer_intent
    raw = record.get("Raw Evidence")
    if raw:
        try:
            parsed = raw if isinstance(raw, dict) else json.loads(str(raw))
        except Exception:
            parsed = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("buyer_intent"), dict):
            return parsed["buyer_intent"]
    return {}


def _valid_buyer_intent(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(key in value for key in ("crm_booking_stack", "review_velocity", "ads", "money_signal", "evidence"))


def _detected_tools(record: Dict[str, Any]) -> List[str]:
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    existing = evidence.get("third_party_tools") or []
    if isinstance(existing, str):
        existing = [part.strip() for part in existing.replace(",", ";").split(";") if part.strip()]
    if not isinstance(existing, list):
        existing = []

    text = _record_text(record)
    detected = _detect_from_text(text, THIRD_PARTY_TOOLS)
    return sorted({str(tool).strip() for tool in [*existing, *detected] if str(tool).strip()})


def _record_text(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    homepage = website.get("homepage", {}) if isinstance(website.get("homepage"), dict) else {}
    pages = website.get("pages", {}) if isinstance(website.get("pages"), dict) else {}
    parts = [
        google.get("business_name", ""),
        google.get("category", ""),
        google.get("website_url", ""),
        website.get("final_url", ""),
        website.get("combined_text", ""),
        homepage.get("title", ""),
        homepage.get("meta", ""),
        homepage.get("text", ""),
        " ".join(str(link) for link in website.get("social_links") or []),
        " ".join(str(tool) for tool in evidence.get("third_party_tools") or []),
    ]
    for page in pages.values():
        if isinstance(page, dict):
            parts.extend([page.get("url", ""), page.get("title", ""), page.get("text", "")])
    return " ".join(str(part or "") for part in parts)


def _review_velocity(record: Dict[str, Any]) -> Dict[str, Any]:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    rating = _safe_float(
        record.get("Rating")
        or record.get("rating")
        or record.get("google_rating")
        or google.get("rating")
    )
    reviews = _safe_int(
        record.get("Reviews")
        or record.get("reviews")
        or record.get("google_review_count")
        or google.get("reviews")
    )

    thresholds = REVIEW_VELOCITY_THRESHOLDS
    if reviews >= thresholds["live_min_reviews"] and rating >= thresholds["live_min_rating"]:
        band = "live"
    elif reviews >= thresholds["steady_min_reviews"] and rating >= thresholds["steady_min_rating"]:
        band = "steady"
    else:
        band = "thin"

    evidence = [f"{reviews} Google reviews with {rating:g} rating"] if reviews or rating else []
    return {
        "velocity_band": band,
        "source": "count_only",
        "evidence": evidence,
    }


def _ads_signals(record: Dict[str, Any], *, ads_check: bool) -> Dict[str, Any]:
    ads = {
        "google_ad_transparency": "unknown",
        "meta_ad_library": "unknown",
        "lsa_badge": "unknown",
        "errors": [],
    }
    if not ads_check:
        return ads

    checks = (
        ("google_ad_transparency", _check_google_ad_transparency),
        ("meta_ad_library", _check_meta_ad_library),
        ("lsa_badge", _check_lsa_badge),
    )
    for field, check in checks:
        try:
            ads[field] = check(record)
        except Exception as exc:  # pragma: no cover - defensive path, behavior asserted via no-raise contract.
            ads[field] = "unknown"
            ads["errors"].append(f"{field}: {exc}")
    return ads


def _check_google_ad_transparency(record: Dict[str, Any]) -> str:
    domain = _record_domain(record)
    if not domain:
        return "unknown"
    url = f"https://adstransparency.google.com/?region=US&domain={quote_plus(domain)}"
    response = requests.get(url, timeout=ADS_TIMEOUT_SECONDS)
    if response.status_code >= 500:
        return "unknown"
    return "unknown"


def _check_meta_ad_library(record: Dict[str, Any]) -> str:
    page = _first_social_link(record, "facebook.com")
    if not page:
        return "unknown"
    url = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&search_type=page&view_all_page_id={quote_plus(page)}"
    response = requests.get(url, timeout=ADS_TIMEOUT_SECONDS)
    if response.status_code >= 500:
        return "unknown"
    return "unknown"


def _check_lsa_badge(record: Dict[str, Any]) -> str:
    text = _record_text(record).lower()
    if "google guaranteed" in text or "local services ads" in text:
        return "yes"
    return "unknown"


def _record_domain(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    url = str(website.get("final_url") or google.get("website_url") or record.get("Website URL") or "")
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.lower().replace("www.", "")


def _first_social_link(record: Dict[str, Any], domain: str) -> str:
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    for link in website.get("social_links") or []:
        if domain in str(link).lower():
            return str(link)
    return ""


def _safe_int(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
