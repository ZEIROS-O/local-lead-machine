from __future__ import annotations

import re
from typing import Any, Dict, List


TRACKING_TERMS = {
    "Google Analytics": ["google-analytics", "gtag(", "gtm.js", "googletagmanager"],
    "Meta Pixel": ["fbq(", "facebook pixel", "connect.facebook.net"],
    "CallRail": ["callrail"],
}

THIRD_PARTY_TOOLS = {
    "Jobber": ["getjobber.com", "client hub", "jobber"],
    "Angi": ["angi.com", "homeadvisor"],
    "GHL": ["gohighlevel", "leadconnector"],
    "Housecall Pro": ["housecallpro"],
}

TRUST_TERMS = [
    "google guaranteed",
    "licensed",
    "insured",
    "bbb",
    "award",
    "years of experience",
    "family owned",
]

SERVICE_AREA_TERMS = [
    "service area",
    "areas we serve",
    "serving",
    "northeast ohio",
    "cleveland",
    "akron",
    "lake county",
]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _evidence_item(
    items: List[Dict[str, Any]],
    *,
    field: str,
    value: Any,
    source_url: str = "",
    snippet: str = "",
    kind: str = "fact",
    confidence: str = "high",
) -> None:
    if value in ("", None, [], {}):
        return
    item_id = f"ev-{field}-{len(items) + 1}"
    item_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", item_id).strip("-").lower()
    items.append(
        {
            "id": item_id,
            "kind": kind,
            "source_url": source_url,
            "field": field,
            "value": value,
            "snippet": snippet,
            "confidence": confidence,
        }
    )


def _detect_from_text(text: str, mapping: Dict[str, List[str]]) -> List[str]:
    lowered = text.lower()
    found = []
    for label, terms in mapping.items():
        if any(term.lower() in lowered for term in terms):
            found.append(label)
    return sorted(found)


def _status_from_site(site: Dict[str, Any]) -> str:
    status = _clean(site.get("site_status")).lower()
    flags = set(site.get("site_issue_flags") or [])
    final_url = _clean(site.get("final_url")).lower()
    text = _clean(site.get("combined_text") or site.get("homepage", {}).get("text"))

    if status in {"no website", "missing"}:
        return "missing"
    if status in {"broken website", "broken"}:
        return "broken"
    if "facebook.com" in final_url or "instagram.com" in final_url:
        return "social_only"
    if "getjobber.com" in final_url or "jobber" in text.lower():
        return "jobber_only"
    if "homepage_fetch_failed" in flags:
        return "broken"
    if not text and status:
        return "broken"
    if flags:
        return "weak"
    if status in {"loads", "okay", "strong"}:
        return "okay"
    return "unknown"


def build_evidence_profile(enriched: Dict[str, Any]) -> Dict[str, Any]:
    site = enriched.get("website", {}) or {}
    google = enriched.get("google", {}) or {}
    screenshots = enriched.get("screenshots", {}) or {}
    derived = enriched.get("derived", {}) or {}

    homepage = site.get("homepage", {}) or {}
    pages = site.get("pages", {}) or {}
    combined_text = " ".join(
        [
            _clean(homepage.get("title")),
            _clean(homepage.get("meta")),
            _clean(site.get("combined_text")),
            " ".join(_clean(page.get("text")) for page in pages.values() if isinstance(page, dict)),
        ]
    )
    lowered = combined_text.lower()
    final_url = _clean(site.get("final_url") or homepage.get("url") or google.get("website_url"))
    items: List[Dict[str, Any]] = []

    site_status = _status_from_site(site)
    tools = _detect_from_text(" ".join([combined_text, final_url]), THIRD_PARTY_TOOLS)
    tracking = _detect_from_text(combined_text, TRACKING_TERMS)
    trust_signals = sorted({term for term in TRUST_TERMS if term in lowered})
    service_area = sorted({term for term in SERVICE_AREA_TERMS if term in lowered})
    services = sorted(set(site.get("high_ticket_terms") or []))

    ctas = site.get("cta_terms") or []
    emails = site.get("emails_found") or []
    phones = site.get("phones_found") or []
    social_links = site.get("social_links") or []

    _evidence_item(
        items,
        field="google_rating",
        value=f"{google.get('rating', '')} stars / {google.get('reviews', '')} reviews".strip(),
        source_url=google.get("google_maps_url", ""),
    )
    _evidence_item(items, field="site_status", value=site_status, source_url=final_url)
    _evidence_item(items, field="site_issue_flags", value=", ".join(site.get("site_issue_flags") or []), source_url=final_url)
    _evidence_item(items, field="third_party_tools", value=", ".join(tools), source_url=final_url)
    _evidence_item(items, field="contact_form", value=bool(site.get("forms_found")), source_url=final_url)
    _evidence_item(items, field="cta_terms", value=", ".join(ctas), source_url=final_url)
    _evidence_item(items, field="email_visible", value=", ".join(emails), source_url=final_url)
    _evidence_item(items, field="phone_visible", value=", ".join(phones), source_url=final_url)
    _evidence_item(items, field="services_detected", value=", ".join(services), source_url=final_url)
    _evidence_item(items, field="service_area_detected", value=", ".join(service_area), source_url=final_url)
    _evidence_item(items, field="trust_signals", value=", ".join(trust_signals), source_url=final_url)
    _evidence_item(items, field="tracking_detected", value=", ".join(tracking), source_url=final_url)
    _evidence_item(items, field="phone_consistency", value=derived.get("phone_consistency", "unknown"), source_url=final_url)
    if screenshots.get("desktop") or screenshots.get("mobile"):
        _evidence_item(
            items,
            field="screenshots",
            value={"desktop": screenshots.get("desktop", ""), "mobile": screenshots.get("mobile", "")},
            kind="screenshot",
            confidence="medium",
        )

    return {
        "site_status": site_status,
        "mobile_status": "unknown",
        "has_estimate_cta": any("estimate" in str(cta).lower() or "quote" in str(cta).lower() for cta in ctas),
        "has_contact_form": bool(site.get("forms_found")),
        "has_online_booking": any(tool in tools for tool in ["Jobber", "Housecall Pro", "GHL"]),
        "phone_visible": bool(phones),
        "email_visible": bool(emails),
        "services_detected": services,
        "service_area_detected": service_area,
        "trust_signals": trust_signals,
        "social_links": social_links,
        "third_party_tools": tools,
        "tracking_detected": tracking,
        "screenshots": {"desktop": screenshots.get("desktop", ""), "mobile": screenshots.get("mobile", "")},
        "raw_flags": site.get("site_issue_flags") or [],
        "phone_consistency": derived.get("phone_consistency", "unknown"),
        "evidence_items": items,
    }
