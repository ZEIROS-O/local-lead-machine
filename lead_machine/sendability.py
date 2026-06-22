from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Sequence, Set

from .buyer_signals import buyer_intent_summary, has_money_signal as buyer_has_money_signal, review_demand_band


MIN_SENDABLE_SCORE = 65
MIN_SENDABLE_CONFIDENCE = 0.75

SCORE_BLOCKER = "Score below B threshold"
CONVERSION_ISSUE_BLOCKER = "Missing concrete conversion issue"
SEND_TRIGGER_BLOCKER = "Missing concrete send trigger"
DEMAND_BLOCKER = "Missing proof-of-demand signal"
RATING_BLOCKER = "Rating below 4.0 minimum"
REVIEW_COUNT_BLOCKER = "Review count below sendable minimum"
VISIBLE_PAIN_BLOCKER = "Visible conversion pain too weak for business maturity"
MONEY_SIGNAL_BLOCKER = "Missing buyer-intent money signal"
STRONG_FORM_NO_PAIN_BLOCKER = "Strong estimate form exists / no concrete visible conversion issue"
ADVANCED_OPTIMIZATION_BLOCKER = "Advanced Optimization without concrete visible pain"
FORM_ABSENCE_CLAIM_BLOCKER = "Strong estimate form conflicts with missing-form claim"
ESTIMATE_FUNNEL_STRONG_FORM_BLOCKER = "Estimate Funnel blocked by strong estimate form without estimate-path weakness"
ANGLE_BLOCKER = "Missing clear outreach angle"
OPENER_BLOCKER = "Missing evidence-backed opener"
EVIDENCE_COUNT_BLOCKER = "Not enough evidence references"
CONFIDENCE_BLOCKER = "Low confidence"
NO_SAFE_CLAIM_BLOCKER = "No safe outreach claim"
SAFE_CLAIM_REF_BLOCKER = "Safe outreach claim missing evidence reference"
INVALID_REF_BLOCKER = "Invalid evidence reference"

EVIDENCE_BLOCKERS = {
    CONVERSION_ISSUE_BLOCKER,
    SEND_TRIGGER_BLOCKER,
    VISIBLE_PAIN_BLOCKER,
    STRONG_FORM_NO_PAIN_BLOCKER,
    ADVANCED_OPTIMIZATION_BLOCKER,
    FORM_ABSENCE_CLAIM_BLOCKER,
    ESTIMATE_FUNNEL_STRONG_FORM_BLOCKER,
    DEMAND_BLOCKER,
    OPENER_BLOCKER,
    EVIDENCE_COUNT_BLOCKER,
    NO_SAFE_CLAIM_BLOCKER,
    SAFE_CLAIM_REF_BLOCKER,
    INVALID_REF_BLOCKER,
}

CONFIDENCE_VALUES = {
    "high": 1.0,
    "medium": 0.75,
    "low": 0.5,
}

CONCRETE_SITE_STATUSES = {
    "missing",
    "no website",
    "no_website",
    "broken",
    "broken website",
    "broken_website",
    "social only",
    "social_only",
    "jobber only",
    "jobber_only",
    "weak",
}

HARD_CONCRETE_SITE_STATUSES = CONCRETE_SITE_STATUSES - {"weak"}

CONCRETE_ISSUE_FLAGS = {
    "dns_domain_failure",
    "homepage_http_hard_failure",
    "ssl_cert_hard_failure",
    "broken_redirect",
    "rendered_load_failed",
    "parked_dead_domain",
    "non_standard_site_or_booking_url",
    "non_standard_site_or_booking",
    "premium_perception_gap",
    "premium_brand_mismatch",
    "owned_site_undersells_google_proof",
    "cheap_website",
    "site_looks_cheap",
    "outdated_site",
    "lawn_maintenance_positioning",
    "weak_project_gallery",
    "weak_gallery",
    "buried_project_photos",
    "project_photos_buried",
    "portfolio_presentation_pain",
    "no_form_detected",
    "no_clear_cta_detected",
    "missing_contact_path",
    "missing_estimate_path",
    "weak_mobile_path",
    "buried_phone",
    "phone_not_visible",
    "generic_copy_signals",
    "weak_services_page",
    "weak_basic_form",
    "only_phone_email_path",
    "broken_form",
    "form_error",
    "bad_mobile_status",
    "buried_cta",
    "low_visible_trust",
    "reviews_not_near_cta",
    "unclear_service_pages",
    "high_ticket_services_unclear",
    "service_clarity_pain",
    "mobile_usability_pain",
    "trust_proof_pain",
    "followup_or_missed_call_pain",
    "missed_call_path",
    "followup_gap",
}

FORM_ABSENCE_FLAGS = {
    "no_form_detected",
    "missing_contact_path",
    "missing_estimate_path",
}

ESTIMATE_PATH_WEAKNESS_FLAGS = {
    "no_clear_cta_detected",
    "weak_mobile_path",
    "bad_mobile_status",
    "buried_cta",
    "only_phone_email_path",
    "broken_form",
    "form_error",
    "low_visible_trust",
    "reviews_not_near_cta",
    "weak_basic_form",
    "mobile_usability_pain",
    "trust_proof_pain",
}

NON_FORM_VISIBLE_PAIN_CATEGORIES = {
    "premium_perception_gap",
    "portfolio_presentation_pain",
    "trust_proof_pain",
    "mobile_usability_pain",
    "service_clarity_pain",
    "no_site_or_social_only_pain",
    "broken_site_pain",
    "followup_or_missed_call_pain",
}

CONCRETE_VISIBLE_PAIN_CATEGORIES = NON_FORM_VISIBLE_PAIN_CATEGORIES | {"estimate_path_pain"}

FORM_ABSENCE_PATTERNS = [
    r"\bno estimate form\b",
    r"\bmissing estimate form\b",
    r"\bno contact form\b",
    r"\bmissing contact form\b",
    r"\bno contact path\b",
    r"\bmissing contact path\b",
    r"\bno estimate path\b",
    r"\bmissing estimate path\b",
    r"\bno estimate funnel\b",
    r"\bno way to request (a )?(quote|estimate)\b",
    r"\bneed(s)? an estimate path\b",
]

ESTIMATE_PATH_WEAKNESS_PATTERNS = [
    r"\bweak estimate form\b",
    r"\bbasic estimate form\b",
    r"\bestimate form appears broken\b",
    r"\bbroken form\b",
    r"\bform error\b",
    r"\bno clear estimate cta\b",
    r"\bestimate cta appears buried\b",
    r"\bburied cta\b",
    r"\bonly phone/email estimate path\b",
    r"\bweak mobile (quote|estimate) path\b",
    r"\bbad mobile\b",
    r"\bmobile quote path\b",
    r"\btrust proof near cta\b",
    r"\breviews? (are )?not used near\b",
]

GENERIC_OPTIMIZATION_REASONS = {
    "generic/template website copy",
    "minor optimization angle only",
}

CONCRETE_ISSUE_FIELDS = {
    "site_status",
    "site_issue_flags",
    "rendered_site_status",
    "contact_form",
    "contact_form_detected",
    "estimate_form_detected",
    "form_strength",
    "cta_terms",
    "cta_strength",
    "phone_visible",
    "third_party_tools",
    "screenshots",
}


def normalize_evidence_refs(value: Any) -> List[str]:
    if isinstance(value, dict):
        for key in ("evidence_refs", "Evidence Refs", "evidence_links", "Evidence Links"):
            if key in value:
                return normalize_evidence_refs(value.get(key))
        return []

    refs = _split_refs(value)
    normalized: List[str] = []
    seen: Set[str] = set()
    for ref in refs:
        if ref and ref not in seen:
            normalized.append(ref)
            seen.add(ref)
    return normalized


def normalize_safe_outreach_claims(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("safe_outreach_claims", "Safe Outreach Claims"):
            if key in value:
                return normalize_safe_outreach_claims(value.get(key))
        return []

    value = _loads_json_if_possible(value)
    if value in ("", None, [], {}):
        return []

    if isinstance(value, str):
        return _parse_flattened_safe_claims(value)

    if isinstance(value, dict):
        items: Iterable[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = value
    else:
        items = [value]

    claims: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            claim = str(item.get("claim") or "").strip()
            refs = normalize_evidence_refs(item.get("evidence_refs"))
        else:
            claim = str(item or "").strip()
            refs = []
        if claim:
            claims.append({"claim": claim, "evidence_refs": refs})
    return claims


def flatten_safe_outreach_claims(value: Any) -> str:
    parts: List[str] = []
    for claim in normalize_safe_outreach_claims(value):
        refs = ",".join(claim.get("evidence_refs") or [])
        parts.append(f"{claim.get('claim', '')} [{refs}]")
    return "; ".join(parts)


def normalize_unsafe_claims(value: Any) -> List[str]:
    if isinstance(value, dict):
        for key in ("unsafe_claims", "Unsafe Claims"):
            if key in value:
                return normalize_unsafe_claims(value.get(key))
        return []

    value = _loads_json_if_possible(value)
    if value in ("", None, [], {}):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(";") if part.strip()]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def flatten_unsafe_claims(value: Any) -> str:
    return "; ".join(normalize_unsafe_claims(value))


def evaluate_sendability(row: Dict[str, Any]) -> Dict[str, Any]:
    blockers: List[str] = []
    total_score = _parse_score(row)
    confidence = normalize_confidence(_pick(row, "confidence", "AI Confidence"))
    evidence_refs = normalize_evidence_refs(row)
    safe_claims = normalize_safe_outreach_claims(row)
    known_refs = known_evidence_ids(row)

    if total_score < MIN_SENDABLE_SCORE:
        blockers.append(SCORE_BLOCKER)

    concrete_issue = has_concrete_conversion_issue(row)
    strong_estimate_form = has_strong_estimate_form(row)
    send_trigger = get_send_trigger(row)
    visible_pain_score = get_visible_pain_score(row)
    visible_pain_reasons = get_visible_pain_reasons(row)
    rating = _rating(row)
    reviews = _review_count(row)

    if not concrete_issue:
        blockers.append(CONVERSION_ISSUE_BLOCKER)

    if not send_trigger:
        blockers.append(SEND_TRIGGER_BLOCKER)

    if strong_estimate_form and has_form_absence_claim(row):
        blockers.append(FORM_ABSENCE_CLAIM_BLOCKER)

    if (
        strong_estimate_form
        and _best_angle(row).lower() == "estimate funnel"
        and not has_estimate_path_weakness_beyond_form_existence(row)
    ):
        blockers.append(ESTIMATE_FUNNEL_STRONG_FORM_BLOCKER)

    if strong_estimate_form and not concrete_issue:
        blockers.append(STRONG_FORM_NO_PAIN_BLOCKER)

    if _best_angle(row).lower() == "advanced optimization" and (
        not concrete_issue or is_generic_optimization_only(row)
    ):
        blockers.append(ADVANCED_OPTIMIZATION_BLOCKER)

    if rating and rating < 4.0:
        blockers.append(RATING_BLOCKER)

    if reviews < 5 and not is_exceptional_low_review_fit(row):
        blockers.append(REVIEW_COUNT_BLOCKER)

    money_signal = has_money_signal(row)
    review_band = _review_demand_band(row)
    required_pain = required_visible_pain_score(reviews, review_band=review_band, money_signal=money_signal)
    if not is_exceptional_low_review_fit(row) and visible_pain_score < required_pain:
        blockers.append(VISIBLE_PAIN_BLOCKER)

    if not has_demand_signal(row):
        blockers.append(DEMAND_BLOCKER)

    if not _pick(row, "best_angle", "Offer Fit", "Best Service Angle"):
        blockers.append(ANGLE_BLOCKER)

    if not has_evidence_backed_opener(row, safe_claims=safe_claims):
        blockers.append(OPENER_BLOCKER)

    if len(evidence_refs) < 2:
        blockers.append(EVIDENCE_COUNT_BLOCKER)

    if confidence < MIN_SENDABLE_CONFIDENCE:
        blockers.append(CONFIDENCE_BLOCKER)

    if not safe_claims:
        blockers.append(NO_SAFE_CLAIM_BLOCKER)

    if safe_claims and any(not claim.get("evidence_refs") for claim in safe_claims):
        blockers.append(SAFE_CLAIM_REF_BLOCKER)

    if has_invalid_refs(evidence_refs, safe_claims, known_refs):
        blockers.append(INVALID_REF_BLOCKER)

    minimum_evidence_passed = not any(blocker in EVIDENCE_BLOCKERS for blocker in blockers)

    return {
        "sendable": not blockers,
        "send_blockers": blockers,
        "minimum_evidence_passed": minimum_evidence_passed,
        "safe_outreach_claims": safe_claims,
        "unsafe_claims": normalize_unsafe_claims(row),
        "evidence_refs": evidence_refs,
        "confidence_score": confidence,
        "send_trigger": send_trigger,
        "visible_pain_score": visible_pain_score,
        "visible_pain_reasons": visible_pain_reasons,
        "money_signal": money_signal,
        "buyer_intent_signals": buyer_intent_summary(row),
    }


def apply_sendability(row: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(row)
    result = evaluate_sendability(updated)
    blockers = "; ".join(result["send_blockers"])

    updated["Sendable"] = "Yes" if result["sendable"] else "No"
    updated["Minimum Evidence Passed"] = "Yes" if result["minimum_evidence_passed"] else "No"
    updated["Send Blockers"] = blockers
    updated["Sendability Blockers"] = blockers
    updated["Evidence Refs"] = json.dumps(result["evidence_refs"], ensure_ascii=False)
    updated["Safe Outreach Claims"] = flatten_safe_outreach_claims(result["safe_outreach_claims"])
    updated["Unsafe Claims"] = flatten_unsafe_claims(result["unsafe_claims"])
    updated["Send Trigger"] = result["send_trigger"]
    updated["send_trigger"] = result["send_trigger"]
    updated["Visible Pain Score"] = result["visible_pain_score"]
    updated["visible_pain_score"] = result["visible_pain_score"]
    updated["Visible Pain Reasons"] = "; ".join(result["visible_pain_reasons"])
    updated["visible_pain_reasons"] = result["visible_pain_reasons"]
    updated["Money Signal"] = "Yes" if result["money_signal"] else "No"
    updated["money_signal"] = updated["Money Signal"]
    updated["Buyer Intent Signals"] = result["buyer_intent_signals"]
    updated["buyer_intent_signals"] = result["buyer_intent_signals"]

    if updated.get("Packet Eligible") == "Yes" and not result["sendable"]:
        updated["Packet Eligible"] = ""
        updated["Packet Eligibility Reason"] = f"Blocked by sendability: {blockers}"

    return updated


def normalize_confidence(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value or "").strip().lower()
    if cleaned in CONFIDENCE_VALUES:
        return CONFIDENCE_VALUES[cleaned]
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def has_concrete_conversion_issue(row: Dict[str, Any]) -> bool:
    status = _clean(_pick(row, "Site Status", "site_status")).lower()
    quality = _clean(_pick(row, "Current Website Quality")).lower()
    strong_estimate_form = has_strong_estimate_form(row)

    if status in HARD_CONCRETE_SITE_STATUSES or quality in {"n/a"}:
        return True
    if has_specific_visible_pain(row):
        return True
    if not strong_estimate_form and (status in CONCRETE_SITE_STATUSES or quality == "poor"):
        return True

    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)

    website_status = _clean(website.get("site_status")).lower()
    profile_status = _clean(evidence.get("site_status")).lower()
    if website_status in HARD_CONCRETE_SITE_STATUSES or profile_status in HARD_CONCRETE_SITE_STATUSES:
        return True
    if not strong_estimate_form and (
        website_status in CONCRETE_SITE_STATUSES or profile_status in CONCRETE_SITE_STATUSES
    ):
        return True

    flags = set(str(flag).lower() for flag in (website.get("site_issue_flags") or evidence.get("raw_flags") or []))
    if strong_estimate_form:
        flags = flags - FORM_ABSENCE_FLAGS
    if flags & CONCRETE_ISSUE_FLAGS:
        if strong_estimate_form and not (
            flags & (ESTIMATE_PATH_WEAKNESS_FLAGS | concrete_non_form_flags())
        ):
            pass
        else:
            return True

    if not strong_estimate_form and evidence.get("has_contact_form") is False:
        return True
    if not strong_estimate_form and _clean(evidence.get("contact_form_detected")).lower() == "no":
        return True
    if not strong_estimate_form and _clean(evidence.get("estimate_form_detected")).lower() == "no":
        return True
    if _clean(evidence.get("form_strength")).lower() in {"weak", "basic"}:
        return True
    if evidence.get("has_estimate_cta") is False and profile_status in {"weak", "missing", "broken"}:
        return True
    if evidence.get("phone_visible") is False:
        return True
    if evidence.get("third_party_tools") and profile_status in {"jobber_only", "social_only"}:
        return True

    for item in evidence_items(row):
        field = _clean(item.get("field")).lower()
        value = item.get("value")
        value_text = _clean(value).lower()
        if field not in CONCRETE_ISSUE_FIELDS:
            continue
        if field == "site_status" and value_text in CONCRETE_SITE_STATUSES:
            if strong_estimate_form and value_text == "weak":
                continue
            return True
        if field == "rendered_site_status" and value_text == "broken":
            return True
        if field == "site_issue_flags" and any(flag in value_text for flag in CONCRETE_ISSUE_FLAGS):
            if strong_estimate_form and not any(
                flag in value_text for flag in (ESTIMATE_PATH_WEAKNESS_FLAGS | concrete_non_form_flags())
            ):
                continue
            return True
        if field == "contact_form" and value is False and not strong_estimate_form:
            return True
        if field in {"contact_form_detected", "estimate_form_detected"} and value_text == "no" and not strong_estimate_form:
            return True
        if field == "form_strength" and value_text in {"weak", "basic"}:
            return True
        if field == "phone_visible" and value is False:
            return True
        if field == "cta_terms" and value in ("", None, [], {}):
            return True
        if field == "cta_strength" and value_text in {"none", "weak"} and not strong_estimate_form:
            return True
        if field == "third_party_tools" and any(tool in value_text for tool in ("jobber", "housecall", "leadconnector")):
            return True

    return False


def concrete_non_form_flags() -> Set[str]:
    return {
        "premium_perception_gap",
        "premium_brand_mismatch",
        "owned_site_undersells_google_proof",
        "cheap_website",
        "site_looks_cheap",
        "outdated_site",
        "lawn_maintenance_positioning",
        "weak_project_gallery",
        "weak_gallery",
        "buried_project_photos",
        "project_photos_buried",
        "portfolio_presentation_pain",
        "weak_services_page",
        "unclear_service_pages",
        "high_ticket_services_unclear",
        "service_clarity_pain",
        "followup_or_missed_call_pain",
        "missed_call_path",
        "followup_gap",
    }


def has_specific_visible_pain(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    if categories & NON_FORM_VISIBLE_PAIN_CATEGORIES:
        return True

    strong_estimate_form = has_strong_estimate_form(row)
    if "estimate_path_pain" in categories:
        if not strong_estimate_form:
            return True
        return has_estimate_path_weakness_beyond_form_existence(row)

    if get_visible_pain_score(row) <= 0:
        return False

    reasons = get_visible_pain_reasons(row)
    if not reasons:
        return False
    if all(_clean(reason).lower() in GENERIC_OPTIMIZATION_REASONS for reason in reasons):
        return False
    if strong_estimate_form and all(is_form_absence_text(reason) for reason in reasons):
        return False
    return True


def has_estimate_path_weakness_beyond_form_existence(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    if categories & {"mobile_usability_pain", "trust_proof_pain"}:
        return True

    reasons_and_trigger = get_visible_pain_reasons(row) + [get_send_trigger(row)]
    for text in reasons_and_trigger:
        cleaned = _clean(text).lower()
        if not cleaned or is_form_absence_text(cleaned):
            continue
        if any(re.search(pattern, cleaned) for pattern in ESTIMATE_PATH_WEAKNESS_PATTERNS):
            return True
        if "estimate_path_pain" in categories and any(
            term in cleaned for term in ("buried", "mobile", "weak", "basic", "broken", "generic", "disconnected")
        ):
            return True

    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)
    flags = set(str(flag).lower() for flag in (website.get("site_issue_flags") or evidence.get("raw_flags") or []))
    if flags & ESTIMATE_PATH_WEAKNESS_FLAGS:
        return True

    if _clean(evidence.get("form_strength")).lower() in {"weak", "basic"}:
        return True

    for item in evidence_items(row):
        field = _clean(item.get("field")).lower()
        value_text = _clean(item.get("value")).lower()
        if field == "site_issue_flags" and any(flag in value_text for flag in ESTIMATE_PATH_WEAKNESS_FLAGS):
            return True
        if field == "form_strength" and value_text in {"weak", "basic"}:
            return True
        if field == "cta_strength" and value_text in {"none", "weak"}:
            return True

    return False


def has_form_absence_claim(row: Dict[str, Any]) -> bool:
    texts: List[str] = [get_send_trigger(row)]
    texts.extend(get_visible_pain_reasons(row))
    texts.extend(_clean(claim.get("claim")) for claim in normalize_safe_outreach_claims(row))
    texts.extend(
        [
            _pick(row, "opener", "Opener"),
            _pick(row, "recommended_action", "Recommended Action"),
            _pick(row, "Specific Observation", "specific_observation"),
        ]
    )
    return any(is_form_absence_text(text) for text in texts)


def is_form_absence_text(text: Any) -> bool:
    cleaned = _clean(text).lower()
    if not cleaned:
        return False
    return any(re.search(pattern, cleaned) for pattern in FORM_ABSENCE_PATTERNS)


def is_generic_optimization_only(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    if categories & CONCRETE_VISIBLE_PAIN_CATEGORIES:
        return False
    reasons = get_visible_pain_reasons(row)
    if not reasons:
        return True
    return all(_clean(reason).lower() in GENERIC_OPTIMIZATION_REASONS for reason in reasons)


def has_strong_estimate_form(row: Dict[str, Any]) -> bool:
    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)

    estimate_value = (
        _pick(row, "Estimate Form Detected", "estimate_form_detected")
        or evidence.get("estimate_form_detected")
        or website.get("estimate_form_detected")
    )
    strength_value = (
        _pick(row, "Form Strength", "form_strength")
        or evidence.get("form_strength")
        or website.get("form_strength")
    )

    return _clean(estimate_value).lower() == "yes" and _clean(strength_value).lower() == "strong"


def get_send_trigger(row: Dict[str, Any]) -> str:
    evidence = _evidence_profile(row)
    raw = _raw_evidence(row)
    raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
    return _pick(row, "Send Trigger", "send_trigger") or _clean(evidence.get("send_trigger")) or _clean(
        raw_evidence.get("send_trigger") if isinstance(raw_evidence, dict) else ""
    )


def get_visible_pain_score(row: Dict[str, Any]) -> int:
    evidence = _evidence_profile(row)
    raw = _raw_evidence(row)
    raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
    value = (
        _pick(row, "Visible Pain Score", "visible_pain_score")
        or evidence.get("visible_pain_score")
        or (raw_evidence.get("visible_pain_score") if isinstance(raw_evidence, dict) else "")
    )
    return _parse_int(value)


def get_visible_pain_reasons(row: Dict[str, Any]) -> List[str]:
    value: Any = row.get("Visible Pain Reasons")
    if value in ("", None, [], {}):
        value = row.get("visible_pain_reasons")
    if value in ("", None, [], {}):
        evidence = _evidence_profile(row)
        value = evidence.get("visible_pain_reasons")
    if value in ("", None, [], {}):
        raw = _raw_evidence(row)
        raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
        if isinstance(raw_evidence, dict):
            value = raw_evidence.get("visible_pain_reasons")

    value = _loads_json_if_possible(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;\n]+", value) if part.strip()]
    return []


def get_visible_pain_categories(row: Dict[str, Any]) -> List[str]:
    value: Any = row.get("Visible Pain Categories")
    if value in ("", None, [], {}):
        value = row.get("visible_pain_categories")
    if value in ("", None, [], {}):
        evidence = _evidence_profile(row)
        value = evidence.get("visible_pain_categories")
    if value in ("", None, [], {}):
        raw = _raw_evidence(row)
        raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
        if isinstance(raw_evidence, dict):
            value = raw_evidence.get("visible_pain_categories")

    value = _loads_json_if_possible(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;\n]+", value) if part.strip()]
    return []


def required_visible_pain_score(reviews: int, *, review_band: str = "", money_signal: bool = False) -> int:
    if review_band == "mature" and not money_signal:
        return 40
    if review_band == "mature":
        return 35
    if reviews >= 75:
        return 30
    if reviews >= 21:
        return 16
    if reviews >= 5:
        return 10
    return 30


def is_exceptional_low_review_fit(row: Dict[str, Any]) -> bool:
    if _review_count(row) >= 5:
        return False
    if _rating(row) < 4.8:
        return False
    return _normalized_site_status(row) in {"missing", "broken", "no website", "no_website", "broken website"}


def _best_angle(row: Dict[str, Any]) -> str:
    return _pick(row, "best_angle", "Offer Fit", "Best Service Angle")


def has_money_signal(row: Dict[str, Any]) -> bool:
    return buyer_has_money_signal(row)


def _review_demand_band(row: Dict[str, Any]) -> str:
    explicit = _clean(_pick(row, "Review Demand Band", "Review Fit Band", "review_demand_band", "review_fit_band")).lower()
    if explicit:
        return explicit
    raw = _raw_evidence(row)
    if isinstance(raw, dict):
        return review_demand_band(raw)
    return ""


def is_bad_site_only_signal(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    status = _normalized_site_status(row)
    bad_site_status = status in {
        "missing",
        "no website",
        "no_website",
        "broken",
        "broken website",
        "broken_website",
        "social only",
        "social_only",
        "jobber only",
        "jobber_only",
    }
    allowed_categories = {"no_site_or_social_only_pain", "broken_site_pain"}
    if bad_site_status:
        allowed_categories.add("estimate_path_pain")
    if categories - allowed_categories:
        return False

    if _clean(_pick(row, "Form Strength", "form_strength")).lower() in {"weak", "basic"}:
        return False

    angle = _best_angle(row).lower()
    if angle and angle not in {"website rebuild", "no-site launch", "social/jobber escape"}:
        return False

    if bad_site_status:
        return True

    trigger = get_send_trigger(row).lower()
    return any(
        phrase in trigger
        for phrase in (
            "no owned website",
            "website appears broken",
            "social-only web presence",
            "jobber-only web presence",
        )
    )


def _review_count(row: Dict[str, Any]) -> int:
    raw = _raw_evidence(row)
    google = raw.get("google", {}) if isinstance(raw, dict) else {}
    return _parse_int(_pick(row, "Reviews", "reviews", "google_review_count") or google.get("reviews"))


def _rating(row: Dict[str, Any]) -> float:
    raw = _raw_evidence(row)
    google = raw.get("google", {}) if isinstance(raw, dict) else {}
    return _parse_float(_pick(row, "Rating", "rating", "google_rating") or google.get("rating"))


def _normalized_site_status(row: Dict[str, Any]) -> str:
    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)
    return _clean(
        _pick(row, "Site Status", "site_status") or evidence.get("site_status") or website.get("site_status")
    ).lower()


def has_demand_signal(row: Dict[str, Any]) -> bool:
    if has_money_signal(row):
        return True

    reviews = _parse_int(_pick(row, "Reviews", "reviews", "google_review_count"))
    rating = _parse_float(_pick(row, "Rating", "rating", "google_rating"))
    if reviews >= 5 or (rating >= 4.5 and reviews > 0):
        return True

    raw = _raw_evidence(row)
    google = raw.get("google", {}) if isinstance(raw, dict) else {}
    if _parse_int(google.get("reviews")) >= 5:
        return True

    for item in evidence_items(row):
        if _clean(item.get("field")).lower() == "google_rating" and _clean(item.get("value")):
            return True

    return False


def has_evidence_backed_opener(row: Dict[str, Any], *, safe_claims: List[Dict[str, Any]] | None = None) -> bool:
    opener = _pick(row, "opener", "Opener")
    if not opener:
        return False
    claims = safe_claims if safe_claims is not None else normalize_safe_outreach_claims(row)
    return any(claim.get("evidence_refs") for claim in claims)


def has_invalid_refs(
    evidence_refs: List[str],
    safe_claims: List[Dict[str, Any]],
    known_refs: Set[str],
) -> bool:
    if not known_refs:
        return False

    refs = list(evidence_refs)
    for claim in safe_claims:
        refs.extend(claim.get("evidence_refs") or [])
    return any(ref not in known_refs for ref in refs)


def known_evidence_ids(row: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for item in evidence_items(row):
        item_id = item.get("id")
        if item_id:
            ids.add(str(item_id))

    ids.update(normalize_evidence_refs(row.get("Known Evidence IDs")))
    ids.update(normalize_evidence_refs(row.get("known_evidence_ids")))
    return ids


def evidence_items(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    direct = _loads_json_if_possible(row.get("evidence_items"))
    if isinstance(direct, list):
        items.extend(item for item in direct if isinstance(item, dict))

    evidence = _evidence_profile(row)
    profile_items = evidence.get("evidence_items") if isinstance(evidence, dict) else []
    if isinstance(profile_items, list):
        items.extend(item for item in profile_items if isinstance(item, dict))

    raw = _raw_evidence(row)
    if isinstance(raw, dict):
        raw_evidence = raw.get("evidence") or {}
        raw_items = raw_evidence.get("evidence_items") if isinstance(raw_evidence, dict) else []
        if isinstance(raw_items, list):
            items.extend(item for item in raw_items if isinstance(item, dict))

    return items


def _evidence_profile(row: Dict[str, Any]) -> Dict[str, Any]:
    evidence = _loads_json_if_possible(row.get("evidence"))
    if isinstance(evidence, dict):
        return evidence

    raw = _raw_evidence(row)
    if isinstance(raw, dict) and isinstance(raw.get("evidence"), dict):
        return raw["evidence"]

    return {}


def _raw_evidence(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = _loads_json_if_possible(row.get("Raw Evidence") or row.get("raw_evidence"))
    return raw if isinstance(raw, dict) else {}


def _split_refs(value: Any) -> List[str]:
    value = _loads_json_if_possible(value)
    if value in ("", None, [], {}):
        return []
    if isinstance(value, str):
        return [part for part in re.split(r"[,;\s]+", value.strip()) if part]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        refs: List[str] = []
        for item in value:
            refs.extend(_split_refs(item))
        return refs
    return [str(value).strip()]


def _parse_flattened_safe_claims(value: str) -> List[Dict[str, Any]]:
    claims: List[Dict[str, Any]] = []
    for part in value.split(";"):
        cleaned = part.strip()
        if not cleaned:
            continue
        match = re.match(r"^(?P<claim>.*?)\s*\[(?P<refs>[^\]]*)\]\s*$", cleaned)
        if match:
            claims.append(
                {
                    "claim": match.group("claim").strip(),
                    "evidence_refs": normalize_evidence_refs(match.group("refs")),
                }
            )
        else:
            claims.append({"claim": cleaned, "evidence_refs": []})
    return claims


def _loads_json_if_possible(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if not cleaned:
        return ""
    if not cleaned.startswith(("[", "{")):
        return value
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return value


def _pick(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in ("", None, [], {}):
            return _clean(value)
    return ""


def _parse_score(row: Dict[str, Any]) -> float:
    return _parse_float(_pick(row, "total_score", "Score Total", "score"))


def _parse_float(value: Any) -> float:
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_int(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _clean(value: Any) -> str:
    return str(value or "").strip()
