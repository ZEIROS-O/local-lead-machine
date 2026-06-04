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
    "HubSpot": ["hubspot"],
    "Jotform": ["jotform"],
    "Typeform": ["typeform"],
    "Gravity Forms": ["gravity forms", "gravityforms", "gform_wrapper"],
    "WPForms": ["wpforms"],
    "Formstack": ["formstack"],
    "Squarespace Form": ["squarespace form", "sqs-block-form"],
    "Wix Form": ["wix form", "wixforms"],
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

FORM_ABSENCE_FLAGS = {
    "no_form_detected",
    "missing_contact_path",
    "missing_estimate_path",
}

STATIC_FETCH_REVIEW_FLAGS = {
    "static_fetch_failed",
    "homepage_fetch_failed",
    "rendered_check_needed",
    "manual_visual_review_needed",
}

HARD_SITE_FAILURE_FLAGS = {
    "dns_domain_failure",
    "homepage_http_hard_failure",
    "ssl_cert_hard_failure",
    "broken_redirect",
    "rendered_load_failed",
    "parked_dead_domain",
}

VISUAL_AUDIT_SCORE_FIELDS = [
    "visual_quality_score",
    "premium_perception_score",
    "mobile_visual_score",
    "hero_clarity_score",
    "cta_visibility_score",
    "gallery_visibility_score",
    "trust_proof_visibility_score",
]

PAIN_FLAG_RULES = {
    "premium_perception_gap": (22, "Premium work is undersold by the owned website", ["premium_perception_gap"]),
    "premium_brand_mismatch": (22, "Premium branding is not matched by the website", ["premium_perception_gap"]),
    "owned_site_undersells_google_proof": (
        20,
        "Owned site undersells stronger Google/social proof",
        ["premium_perception_gap"],
    ),
    "cheap_website": (18, "Website looks cheap relative to the work", ["premium_perception_gap"]),
    "site_looks_cheap": (18, "Website looks cheap relative to the work", ["premium_perception_gap"]),
    "outdated_site": (18, "Website looks outdated", ["premium_perception_gap"]),
    "lawn_maintenance_positioning": (
        18,
        "Website positioning looks like low-ticket lawn maintenance",
        ["premium_perception_gap", "service_clarity_pain"],
    ),
    "weak_project_gallery": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "weak_gallery": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "buried_project_photos": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "project_photos_buried": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "portfolio_presentation_pain": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "weak_mobile_path": (16, "Weak mobile quote path", ["mobile_usability_pain", "estimate_path_pain"]),
    "bad_mobile_status": (16, "Weak mobile quote path", ["mobile_usability_pain", "estimate_path_pain"]),
    "mobile_usability_pain": (16, "Weak mobile quote path", ["mobile_usability_pain", "estimate_path_pain"]),
    "low_visible_trust": (12, "No visible trust proof near CTA", ["trust_proof_pain"]),
    "reviews_not_near_cta": (12, "Reviews are not used near the conversion point", ["trust_proof_pain"]),
    "trust_proof_pain": (12, "No visible trust proof near CTA", ["trust_proof_pain"]),
    "weak_services_page": (12, "Unclear service presentation", ["service_clarity_pain"]),
    "unclear_service_pages": (12, "Unclear service presentation", ["service_clarity_pain"]),
    "high_ticket_services_unclear": (
        14,
        "High-ticket services are not shown clearly",
        ["service_clarity_pain"],
    ),
    "service_clarity_pain": (12, "Unclear service presentation", ["service_clarity_pain"]),
    "followup_or_missed_call_pain": (
        12,
        "Follow-up or missed-call path is visibly weak",
        ["followup_or_missed_call_pain"],
    ),
    "missed_call_path": (
        12,
        "Follow-up or missed-call path is visibly weak",
        ["followup_or_missed_call_pain"],
    ),
    "followup_gap": (
        12,
        "Follow-up or missed-call path is visibly weak",
        ["followup_or_missed_call_pain"],
    ),
}


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
    normalized_flags = {str(flag).strip().lower() for flag in flags}
    rendered_status = _clean(site.get("rendered_site_status")).lower()
    final_url = _clean(site.get("final_url")).lower()
    text = _clean(site.get("combined_text") or site.get("homepage", {}).get("text"))

    if status in {"no website", "missing"}:
        return "missing"
    if rendered_status == "broken" or normalized_flags & HARD_SITE_FAILURE_FLAGS:
        return "broken"
    if rendered_status == "loaded" and status not in {"social only", "social_only", "facebook only", "jobber only", "jobber_only"}:
        review_flags = normalized_flags - STATIC_FETCH_REVIEW_FLAGS
        return "weak" if review_flags else "okay"
    if status in {"broken website", "broken"}:
        return "broken"
    if status in {"social only", "social_only", "facebook only"}:
        return "social_only"
    if status in {"jobber only", "jobber_only"}:
        return "jobber_only"
    if "facebook.com" in final_url or "instagram.com" in final_url:
        return "social_only"
    if "getjobber.com" in final_url or "jobber" in text.lower():
        return "jobber_only"
    if normalized_flags & STATIC_FETCH_REVIEW_FLAGS and not text:
        return "unknown"
    if not text and status and status not in {"unknown", "loaded", "loads", "okay", "strong"}:
        return "broken"
    if normalized_flags:
        return "weak"
    if status in {"loaded", "loads", "okay", "strong"}:
        return "okay"
    return "unknown"


def _derive_visible_pain_profile(
    *,
    site: Dict[str, Any],
    site_status: str,
    contact_form_detected: str,
    estimate_form_detected: str,
    form_strength: str,
    cta_strength: str,
    phones: List[Any],
    tools: List[str],
    phone_consistency: str = "unknown",
) -> Dict[str, Any]:
    flags = {str(flag).strip().lower() for flag in (site.get("site_issue_flags") or [])}
    score = 0
    reasons: List[str] = []
    categories: List[str] = []

    def add(points: int, reason: str, pain_categories: List[str] | None = None) -> None:
        nonlocal score
        if reason in reasons:
            for category in pain_categories or []:
                if category not in categories:
                    categories.append(category)
            return
        score += points
        reasons.append(reason)
        for category in pain_categories or []:
            if category not in categories:
                categories.append(category)

    normalized_status = _clean(site_status).lower()
    strong_estimate_form = (
        _clean(estimate_form_detected).lower() == "yes" and _clean(form_strength).lower() == "strong"
    )

    if normalized_status == "missing":
        add(40, "No owned website found", ["no_site_or_social_only_pain"])
    elif normalized_status == "broken":
        add(40, "Website appears broken", ["broken_site_pain"])
    elif normalized_status == "social_only":
        add(35, "Social-only web presence", ["no_site_or_social_only_pain"])
    elif normalized_status == "jobber_only":
        add(32, "Jobber-only web presence", ["no_site_or_social_only_pain"])

    if flags & HARD_SITE_FAILURE_FLAGS and normalized_status != "broken":
        add(40, "Website appears broken", ["broken_site_pain"])
    if "non_standard_site_or_booking_url" in flags or "non_standard_site_or_booking" in flags:
        add(28, "Non-standard booking or social URL", ["no_site_or_social_only_pain"])
    if "broken_form" in flags or "form_error" in flags:
        add(30, "Estimate form appears broken", ["estimate_path_pain"])

    if not strong_estimate_form:
        if _clean(estimate_form_detected).lower() == "no" or "missing_estimate_path" in flags:
            add(20, "No estimate form detected", ["estimate_path_pain"])
        if _clean(contact_form_detected).lower() == "no" or "missing_contact_path" in flags:
            add(14, "No contact form detected", ["estimate_path_pain"])
        normalized_form_strength = _clean(form_strength).lower()
        if normalized_form_strength == "weak":
            add(18, "Weak estimate form", ["estimate_path_pain"])
        elif normalized_form_strength == "basic":
            add(12, "Basic estimate form only", ["estimate_path_pain"])
        if _clean(cta_strength).lower() in {"none", "weak"} or "no_clear_cta_detected" in flags:
            add(14, "No clear estimate CTA", ["estimate_path_pain"])
        if "buried_cta" in flags:
            add(12, "Estimate CTA appears buried", ["estimate_path_pain"])
        if "only_phone_email_path" in flags:
            add(14, "Only phone/email estimate path", ["estimate_path_pain"])

    if not phones or "phone_not_visible" in flags or "buried_phone" in flags:
        add(8, "Phone number hard to find", ["estimate_path_pain"])

    for flag, (points, reason, pain_categories) in PAIN_FLAG_RULES.items():
        if flag in flags:
            add(points, reason, pain_categories)

    if "generic_copy_signals" in flags:
        if site.get("high_ticket_terms"):
            add(
                14,
                "Premium work is undersold by generic/template website copy",
                ["premium_perception_gap"],
            )
        else:
            add(7, "Generic/template website copy")

    if tools and normalized_status in {"jobber_only", "social_only"}:
        add(6, "Owned website path is not primary", ["no_site_or_social_only_pain"])

    if _clean(phone_consistency).lower() == "different":
        add(12, "Phone/routing signal differs across Google and site", ["followup_or_missed_call_pain"])

    return {
        "visible_pain_score": min(score, 100),
        "visible_pain_reasons": reasons,
        "visible_pain_categories": categories,
        "send_trigger": reasons[0] if reasons else "",
    }


def build_evidence_profile(enriched: Dict[str, Any]) -> Dict[str, Any]:
    site = enriched.get("website", {}) or {}
    google = enriched.get("google", {}) or {}
    screenshots = enriched.get("screenshots", {}) or {}
    derived = enriched.get("derived", {}) or {}
    desktop_screenshot_path = _clean(screenshots.get("desktop") or site.get("desktop_screenshot_path"))
    mobile_screenshot_path = _clean(screenshots.get("mobile") or site.get("mobile_screenshot_path"))
    rendered_site_status = _clean(site.get("rendered_site_status")) or "Unknown"
    rendered_status_reason = _clean(site.get("rendered_status_reason"))
    visual_review_needed = _clean(site.get("visual_review_needed")) or _clean(derived.get("visual_review_needed")) or "No"
    visual_pain_reasons = site.get("visual_pain_reasons") or []
    if isinstance(visual_pain_reasons, str):
        visual_pain_reasons = [part.strip() for part in re.split(r"[;\n]+", visual_pain_reasons) if part.strip()]
    if not isinstance(visual_pain_reasons, list):
        visual_pain_reasons = []
    visual_scores = {
        field: _clean(site.get(field)) or "Unknown"
        for field in VISUAL_AUDIT_SCORE_FIELDS
    }

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
    contact_form_detected = _clean(site.get("contact_form_detected")) or (
        "Yes" if site.get("forms_found") else "No"
    )
    estimate_form_detected = _clean(site.get("estimate_form_detected")) or "Unknown"
    form_strength = _clean(site.get("form_strength")) or "None"
    form_detection_method = _clean(site.get("form_detection_method")) or "none"
    cta_strength = _clean(site.get("cta_strength")) or ("Basic" if ctas else "None")
    emails = site.get("emails_found") or []
    phones = site.get("phones_found") or []
    social_links = site.get("social_links") or []
    visible_pain = _derive_visible_pain_profile(
        site=site,
        site_status=site_status,
        contact_form_detected=contact_form_detected,
        estimate_form_detected=estimate_form_detected,
        form_strength=form_strength,
        cta_strength=cta_strength,
        phones=phones,
        tools=tools,
        phone_consistency=derived.get("phone_consistency", "unknown"),
    )

    _evidence_item(
        items,
        field="google_rating",
        value=f"{google.get('rating', '')} stars / {google.get('reviews', '')} reviews".strip(),
        source_url=google.get("google_maps_url", ""),
    )
    _evidence_item(items, field="site_status", value=site_status, source_url=final_url)
    _evidence_item(
        items,
        field="site_issue_flags",
        value=", ".join(site.get("site_issue_flags") or []) or "none",
        source_url=final_url,
    )
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
    _evidence_item(items, field="contact_form_detected", value=contact_form_detected, source_url=final_url)
    _evidence_item(items, field="estimate_form_detected", value=estimate_form_detected, source_url=final_url)
    _evidence_item(items, field="form_strength", value=form_strength, source_url=final_url)
    _evidence_item(items, field="form_detection_method", value=form_detection_method, source_url=final_url)
    _evidence_item(items, field="cta_strength", value=cta_strength, source_url=final_url)
    _evidence_item(items, field="rendered_site_status", value=rendered_site_status, source_url=final_url)
    _evidence_item(items, field="rendered_status_reason", value=rendered_status_reason, source_url=final_url)
    _evidence_item(items, field="desktop_screenshot_path", value=desktop_screenshot_path, source_url=final_url, kind="screenshot", confidence="medium")
    _evidence_item(items, field="mobile_screenshot_path", value=mobile_screenshot_path, source_url=final_url, kind="screenshot", confidence="medium")
    _evidence_item(items, field="visual_review_needed", value=visual_review_needed, source_url=final_url, confidence="medium")
    _evidence_item(items, field="visual_pain_reasons", value=", ".join(str(reason) for reason in visual_pain_reasons if str(reason).strip()), source_url=final_url, confidence="medium")
    _evidence_item(items, field="visible_pain_score", value=visible_pain["visible_pain_score"], source_url=final_url)
    _evidence_item(
        items,
        field="visible_pain_reasons",
        value=", ".join(visible_pain["visible_pain_reasons"]),
        source_url=final_url,
    )
    _evidence_item(
        items,
        field="visible_pain_categories",
        value=", ".join(visible_pain["visible_pain_categories"]),
        source_url=final_url,
    )
    _evidence_item(items, field="send_trigger", value=visible_pain["send_trigger"], source_url=final_url)
    if desktop_screenshot_path or mobile_screenshot_path:
        _evidence_item(
            items,
            field="screenshots",
            value={"desktop": desktop_screenshot_path, "mobile": mobile_screenshot_path},
            kind="screenshot",
            confidence="medium",
        )

    return {
        "site_status": site_status,
        "mobile_status": "unknown",
        "has_estimate_cta": any("estimate" in str(cta).lower() or "quote" in str(cta).lower() for cta in ctas),
        "has_contact_form": contact_form_detected == "Yes" or bool(site.get("forms_found")),
        "contact_form_detected": contact_form_detected,
        "estimate_form_detected": estimate_form_detected,
        "form_strength": form_strength,
        "form_detection_method": form_detection_method,
        "cta_strength": cta_strength,
        "rendered_site_status": rendered_site_status,
        "rendered_status_reason": rendered_status_reason,
        "desktop_screenshot_path": desktop_screenshot_path,
        "mobile_screenshot_path": mobile_screenshot_path,
        "visual_review_needed": visual_review_needed,
        **visual_scores,
        "visual_pain_reasons": [str(reason).strip() for reason in visual_pain_reasons if str(reason).strip()],
        "visible_pain_score": visible_pain["visible_pain_score"],
        "visible_pain_reasons": visible_pain["visible_pain_reasons"],
        "visible_pain_categories": visible_pain["visible_pain_categories"],
        "send_trigger": visible_pain["send_trigger"],
        "has_online_booking": any(tool in tools for tool in ["Jobber", "Housecall Pro", "GHL"]),
        "phone_visible": bool(phones),
        "email_visible": bool(emails),
        "services_detected": services,
        "service_area_detected": service_area,
        "trust_signals": trust_signals,
        "social_links": social_links,
        "third_party_tools": tools,
        "tracking_detected": tracking,
        "screenshots": {"desktop": desktop_screenshot_path, "mobile": mobile_screenshot_path},
        "raw_flags": site.get("site_issue_flags") or [],
        "phone_consistency": derived.get("phone_consistency", "unknown"),
        "evidence_items": items,
    }
