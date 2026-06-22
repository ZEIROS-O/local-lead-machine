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

REVIEW_DEMAND_THRESHOLDS = {
    "detailing": {
        "early_proof_min": 5,
        "ideal_growth_min": 15,
        "established_min": 126,
        "mature_min": 401,
    },
    "default": {
        "early_proof_min": 5,
        "ideal_growth_min": 15,
        "established_min": 76,
        "mature_min": 201,
    },
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
BUYER_INTENT_LOGIC_VERSION = 2


def build_buyer_intent(record: Dict[str, Any], *, ads_check: bool = False) -> Dict[str, Any]:
    detected_tools = _detected_tools(record)
    paid_tools = sorted(set(detected_tools) & PAID_CRM)
    has_paid_crm = bool(paid_tools)
    crm_evidence = [f"Detected paid CRM/booking tool: {tool}" for tool in paid_tools]

    review_profile = _review_profile(record)
    review_velocity = _legacy_review_velocity(record, review_profile)
    ads = _ads_signals(record, ads_check=ads_check)

    known_ad_yes = any(
        ads.get(field) == "yes"
        for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge")
    )
    money_signal = has_paid_crm or known_ad_yes

    evidence: List[str] = []
    evidence.extend(crm_evidence)
    if review_profile["demand_band"] != "thin":
        evidence.extend(review_profile["evidence"])
    for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge"):
        if ads.get(field) == "yes":
            evidence.append(f"{field.replace('_', ' ')} detected")

    buyer_intent = {
        "logic_version": BUYER_INTENT_LOGIC_VERSION,
        "crm_booking_stack": {
            "detected_tools": detected_tools,
            "has_paid_crm": has_paid_crm,
            "evidence": crm_evidence,
        },
        "review_profile": review_profile,
        "review_demand_signal": review_profile,
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
    return _buyer_intent_has_money_signal(buyer_intent)


def buyer_intent_summary(record: Dict[str, Any]) -> str:
    buyer_intent = _buyer_intent_from_record(record)
    if not buyer_intent:
        return "money_signal=unknown; tools=none; review_demand_band=unknown; ads=unknown"

    stack = buyer_intent.get("crm_booking_stack") or {}
    review_signal = _review_signal_from_buyer_intent(buyer_intent)
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
        f"money_signal={'yes' if _buyer_intent_has_money_signal(buyer_intent) else 'no'}; "
        f"tools={', '.join(str(tool) for tool in tools) or 'none'}; "
        f"review_demand_band={review_signal.get('demand_band') or review_signal.get('velocity_band') or 'unknown'}; "
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
    if _buyer_intent_logic_version(value) < BUYER_INTENT_LOGIC_VERSION:
        return False
    return all(key in value for key in ("crm_booking_stack", "review_velocity", "ads", "money_signal", "evidence"))


def _buyer_intent_logic_version(value: Dict[str, Any]) -> int:
    raw_version = value.get("logic_version", value.get("version"))
    try:
        return int(raw_version)
    except (TypeError, ValueError):
        return 0


def _buyer_intent_has_money_signal(buyer_intent: Dict[str, Any]) -> bool:
    stack = buyer_intent.get("crm_booking_stack") or {}
    if stack.get("has_paid_crm"):
        return True

    ads = buyer_intent.get("ads") or {}
    return any(
        ads.get(field) == "yes"
        for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge")
    )


def _review_signal_from_buyer_intent(buyer_intent: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("review_demand_signal", "review_profile", "review_velocity"):
        value = buyer_intent.get(key)
        if isinstance(value, dict):
            return value
    return {}


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


def review_demand_band(record: Dict[str, Any]) -> str:
    return str(_review_profile(record).get("demand_band") or "thin")


def _review_profile(record: Dict[str, Any]) -> Dict[str, Any]:
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

    thresholds = REVIEW_DEMAND_THRESHOLDS["detailing" if _vertical_family(record) == "detailing" else "default"]
    if reviews >= thresholds["mature_min"]:
        band = "mature"
    elif reviews >= thresholds["established_min"]:
        band = "established"
    elif reviews >= thresholds["ideal_growth_min"]:
        band = "ideal_growth"
    elif reviews >= thresholds["early_proof_min"]:
        band = "early_proof"
    else:
        band = "thin"

    evidence = [f"{reviews} Google reviews with {rating:g} rating"] if reviews or rating else []
    return {
        "demand_band": band,
        "source": "count_only",
        "reviews": reviews,
        "rating": rating,
        "evidence": evidence,
    }


def _legacy_review_velocity(record: Dict[str, Any], review_profile: Dict[str, Any]) -> Dict[str, Any]:
    reviews = int(review_profile.get("reviews") or 0)
    rating = float(review_profile.get("rating") or 0.0)
    if reviews >= 75 and rating >= 4.5:
        band = "live"
    elif reviews >= 5 and rating >= 4.0:
        band = "steady"
    else:
        band = "thin"
    return {
        "velocity_band": band,
        "source": "count_only",
        "evidence": list(review_profile.get("evidence") or []),
    }


def _vertical_family(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    parts = [
        record.get("vertical"),
        record.get("Vertical"),
        google.get("vertical"),
        google.get("source_query"),
        google.get("category"),
    ]
    text = " ".join(str(part or "").lower() for part in parts)
    if any(term in text for term in ("detailing", "detailer", "ceramic coating", "paint correction", "ppf")):
        return "detailing"
    return "default"


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
