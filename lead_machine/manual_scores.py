from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from .schema import normalize_lead
from .sendability import apply_sendability, normalize_evidence_refs, normalize_safe_outreach_claims, normalize_unsafe_claims


SUBSCORE_LIMITS = {
    "icp_fit": 20,
    "reputation_demand_proof": 15,
    "web_conversion_pain": 25,
    "mockup_leverage": 15,
    "reachability": 10,
    "offer_angle_clarity": 10,
    "evidence_confidence": 5,
}

CONFIDENCE_VALUES = {"low", "medium", "high"}


def _require_keys(data: Dict[str, Any], keys: Iterable[str]) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise ValueError(f"Manual score missing required keys: {', '.join(missing)}")


def validate_manual_score(data: Dict[str, Any], *, allowed_evidence_ids: Set[str]) -> Dict[str, Any]:
    _require_keys(
        data,
        [
            "subscores",
            "confidence",
            "hard_disqualifiers",
            "best_angle",
            "recommended_action",
            "why_this_lead",
            "why_this_might_be_wrong",
            "opener",
            "evidence_refs",
            "safe_outreach_claims",
            "unsafe_claims",
            "airtable_fields",
        ],
    )
    subscores = data.get("subscores") or {}
    _require_keys(subscores, SUBSCORE_LIMITS.keys())

    normalized_subscores: Dict[str, int] = {}
    for key, limit in SUBSCORE_LIMITS.items():
        value = subscores.get(key)
        if not isinstance(value, int):
            raise ValueError(f"Subscore {key} must be an integer")
        if value < 0 or value > limit:
            raise ValueError(f"Subscore {key}={value} outside 0-{limit}")
        normalized_subscores[key] = value

    confidence = str(data.get("confidence") or "").lower()
    if confidence not in CONFIDENCE_VALUES:
        raise ValueError("confidence must be low, medium, or high")

    refs = normalize_evidence_refs(data.get("evidence_refs"))
    if not refs:
        raise ValueError("evidence_refs must be a non-empty list")

    unknown_refs = sorted(set(refs) - allowed_evidence_ids)
    if unknown_refs:
        raise ValueError(f"Unknown evidence_refs: {', '.join(unknown_refs)}")

    total = sum(normalized_subscores.values())
    hard_disqualifiers = data.get("hard_disqualifiers") or []
    if hard_disqualifiers or total < 50:
        grade = "Skip"
    elif total >= 80:
        grade = "A"
    elif total >= 65:
        grade = "B"
    else:
        grade = "C"

    validated = dict(data)
    validated["subscores"] = normalized_subscores
    validated["confidence"] = confidence
    validated["evidence_refs"] = refs
    validated["safe_outreach_claims"] = normalize_safe_outreach_claims(data.get("safe_outreach_claims"))
    validated["unsafe_claims"] = normalize_unsafe_claims(data.get("unsafe_claims"))
    validated["total_score"] = total
    validated["grade"] = grade
    validated["send_trigger"] = str(data.get("send_trigger") or "").strip()
    validated["visible_pain_score"] = data.get("visible_pain_score", "")
    visible_pain_reasons = data.get("visible_pain_reasons") or []
    if isinstance(visible_pain_reasons, str):
        visible_pain_reasons = [part.strip() for part in visible_pain_reasons.split(";") if part.strip()]
    if not isinstance(visible_pain_reasons, list):
        visible_pain_reasons = []
    validated["visible_pain_reasons"] = [str(reason).strip() for reason in visible_pain_reasons if str(reason).strip()]
    visible_pain_categories = data.get("visible_pain_categories") or []
    if isinstance(visible_pain_categories, str):
        visible_pain_categories = [
            part.strip() for part in visible_pain_categories.replace(",", ";").split(";") if part.strip()
        ]
    if not isinstance(visible_pain_categories, list):
        visible_pain_categories = []
    validated["visible_pain_categories"] = [
        str(category).strip() for category in visible_pain_categories if str(category).strip()
    ]
    return validated


def load_manual_score(path: Path, *, allowed_evidence_ids: Set[str]) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_manual_score(data, allowed_evidence_ids=allowed_evidence_ids)


def allowed_evidence_ids(enriched: Dict[str, Any]) -> Set[str]:
    evidence = enriched.get("evidence") or {}
    return {str(item.get("id")) for item in evidence.get("evidence_items", []) if item.get("id")}


def _clean_manual_field(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_non_empty_field(fields: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key not in fields:
            continue
        value = _clean_manual_field(fields.get(key))
        if value:
            return value
    return ""


def _sync_manual_airtable_aliases(
    row: Dict[str, Any],
    airtable_fields: Dict[str, Any],
    base_values: Dict[str, Any],
) -> None:
    identity_fields = [
        ("Business Name", "business_name", ("Business Name", "business_name")),
        ("Phone", "phone", ("Phone", "phone")),
        ("City", "city", ("City", "city")),
        ("Vertical", "vertical", ("Vertical", "vertical", "Category", "category")),
        ("Best Service Angle", "best_angle", ("Best Service Angle", "Offer Fit", "Best Angle", "best_angle")),
        ("Recommended Action", "recommended_action", ("Recommended Action", "recommended_action")),
    ]

    for user_key, internal_key, manual_keys in identity_fields:
        value = (
            _first_non_empty_field(airtable_fields, *manual_keys)
            or _clean_manual_field(base_values.get(user_key))
            or _clean_manual_field(row.get(user_key))
        )
        if value:
            row[user_key] = value
            row[internal_key] = value

    if row.get("best_angle"):
        row["Offer Fit"] = row["best_angle"]
        row["Best Angle"] = row["best_angle"]

    category = (
        _first_non_empty_field(airtable_fields, "Category", "category")
        or _clean_manual_field(base_values.get("category"))
        or _clean_manual_field(row.get("category"))
    )
    if category:
        row["category"] = category


def _fallback_visible_pain_from_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    status = str(evidence.get("site_status") or "").strip().lower()
    mapping = {
        "missing": (40, "No owned website found"),
        "no website": (40, "No owned website found"),
        "broken": (40, "Website appears broken"),
        "broken website": (40, "Website appears broken"),
        "social_only": (35, "Social-only web presence"),
        "social only": (35, "Social-only web presence"),
        "jobber_only": (32, "Jobber-only web presence"),
        "jobber only": (32, "Jobber-only web presence"),
    }
    score, trigger = mapping.get(status, (0, ""))
    return {
        "visible_pain_score": score,
        "visible_pain_reasons": [trigger] if trigger else [],
        "visible_pain_categories": [
            "no_site_or_social_only_pain"
            if status in {"missing", "no website", "social_only", "social only", "jobber_only", "jobber only"}
            else "broken_site_pain"
        ]
        if trigger
        else [],
        "send_trigger": trigger,
    }


def merge_manual_score(
    enriched: Dict[str, Any],
    score: Dict[str, Any],
    *,
    fallback_vertical: str,
) -> Dict[str, Any]:
    allowed = allowed_evidence_ids(enriched)
    validated = validate_manual_score(score, allowed_evidence_ids=allowed)
    google = enriched.get("google", {}) or {}
    website = enriched.get("website", {}) or {}
    evidence = enriched.get("evidence", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    subscores = validated["subscores"]

    why = validated.get("why_this_lead") or []
    wrong = validated.get("why_this_might_be_wrong") or []
    observation = "; ".join(str(item) for item in why[:3])
    if wrong:
        observation = f"{observation} Risk: {'; '.join(str(item) for item in wrong[:2])}"

    fallback_pain = _fallback_visible_pain_from_evidence(evidence)
    visible_pain_reasons = (
        validated.get("visible_pain_reasons")
        or evidence.get("visible_pain_reasons")
        or fallback_pain["visible_pain_reasons"]
    )
    if isinstance(visible_pain_reasons, str):
        visible_pain_reasons = [part.strip() for part in visible_pain_reasons.split(";") if part.strip()]
    visible_pain_reasons_text = "; ".join(str(reason) for reason in visible_pain_reasons if str(reason).strip())
    visible_pain_categories = (
        validated.get("visible_pain_categories")
        or evidence.get("visible_pain_categories")
        or fallback_pain["visible_pain_categories"]
    )
    if isinstance(visible_pain_categories, str):
        visible_pain_categories = [
            part.strip() for part in visible_pain_categories.replace(",", ";").split(";") if part.strip()
        ]
    visible_pain_categories_text = "; ".join(
        str(category) for category in visible_pain_categories if str(category).strip()
    )
    visible_pain_score = validated.get("visible_pain_score")
    if visible_pain_score in ("", None):
        visible_pain_score = evidence.get("visible_pain_score")
    if visible_pain_score in ("", None):
        visible_pain_score = fallback_pain["visible_pain_score"]
    send_trigger = validated.get("send_trigger") or evidence.get("send_trigger", "") or fallback_pain["send_trigger"]

    email = ""
    if website.get("emails_found"):
        email = ", ".join(website.get("emails_found") or [])

    packet_eligible = "Yes" if validated["grade"] in {"A", "B"} else ""
    row = {
        "Business Name": lead.business_name,
        "Owner Name": "",
        "Vertical": lead.vertical,
        "City": lead.city,
        "Phone": lead.phone,
        "Email": email,
        "Website URL": lead.website,
        "Google Maps URL": lead.maps_url,
        "Reviews": "" if lead.google_review_count is None else str(lead.google_review_count),
        "Rating": "" if lead.google_rating is None else str(lead.google_rating),
        "Current Website Quality": _website_quality_from_status(evidence.get("site_status", "")),
        "Best Service Angle": validated.get("best_angle", ""),
        "Specific Observation": observation,
        "Mockup URL": "",
        "Video URL": "",
        "Outreach Channel": "Google Maps",
        "Status": "New",
        "Last Contacted": "",
        "Next Follow-Up": "",
        "Lead Priority": validated["grade"],
        "Offer Fit": validated.get("best_angle", ""),
        "Site Status": evidence.get("site_status", ""),
        "Contact Form Detected": evidence.get("contact_form_detected", ""),
        "Estimate Form Detected": evidence.get("estimate_form_detected", ""),
        "Form Strength": evidence.get("form_strength", ""),
        "Form Detection Method": evidence.get("form_detection_method", ""),
        "CTA Strength": evidence.get("cta_strength", ""),
        "Visible Pain Score": visible_pain_score,
        "visible_pain_score": visible_pain_score,
        "Visible Pain Reasons": visible_pain_reasons_text,
        "visible_pain_reasons": visible_pain_reasons,
        "Visible Pain Categories": visible_pain_categories_text,
        "visible_pain_categories": visible_pain_categories,
        "Send Trigger": send_trigger,
        "send_trigger": send_trigger,
        "Evidence Notes": "; ".join(str(ref) for ref in validated.get("evidence_refs", [])),
        "AI Confidence": validated["confidence"],
        "Manual Reviewed": "",
        "Skip Reason": "; ".join(validated.get("hard_disqualifiers") or []) if validated["grade"] == "Skip" else "",
        "Desktop Screenshot Path": evidence.get("screenshots", {}).get("desktop", ""),
        "Mobile Screenshot Path": evidence.get("screenshots", {}).get("mobile", ""),
        "Raw Evidence": json.dumps(enriched, ensure_ascii=False),
        "Score Total": validated["total_score"],
        "Score ICP Fit": subscores["icp_fit"],
        "Score Reputation": subscores["reputation_demand_proof"],
        "Score Web Pain": subscores["web_conversion_pain"],
        "Score Mockup Leverage": subscores["mockup_leverage"],
        "Score Reachability": subscores["reachability"],
        "Score Offer Clarity": subscores["offer_angle_clarity"],
        "Score Evidence Confidence": subscores["evidence_confidence"],
        "Recommended Action": validated.get("recommended_action", ""),
        "Why This Lead": json.dumps(validated.get("why_this_lead") or [], ensure_ascii=False),
        "Why This Might Be Wrong": json.dumps(validated.get("why_this_might_be_wrong") or [], ensure_ascii=False),
        "Opener": validated.get("opener", ""),
        "Evidence Refs": json.dumps(validated.get("evidence_refs") or [], ensure_ascii=False),
        "Safe Outreach Claims": json.dumps(validated.get("safe_outreach_claims") or [], ensure_ascii=False),
        "Unsafe Claims": json.dumps(validated.get("unsafe_claims") or [], ensure_ascii=False),
        "Place Key": lead.place_id_or_unique_key,
        "Packet Eligible": packet_eligible,
        "Packet Eligibility Reason": "Manual grade A/B" if packet_eligible else "Manual grade below B threshold",
    }
    base_alias_values = {
        "Business Name": row.get("Business Name", ""),
        "Phone": row.get("Phone", ""),
        "City": row.get("City", ""),
        "Vertical": row.get("Vertical", ""),
        "Best Service Angle": row.get("Best Service Angle", ""),
        "Recommended Action": row.get("Recommended Action", ""),
        "category": lead.category,
    }
    airtable_fields = validated.get("airtable_fields") or {}
    if not isinstance(airtable_fields, dict):
        airtable_fields = {}
    row.update(airtable_fields)
    _sync_manual_airtable_aliases(row, airtable_fields, base_alias_values)
    row.update(
        {
            # Keep the manual-LLM schema fields next to the existing Airtable/reporting aliases.
            "total_score": validated["total_score"],
            "grade": validated["grade"],
            "best_angle": row.get("best_angle", validated.get("best_angle", "")),
            "confidence": validated["confidence"],
            "Total Score": validated["total_score"],
            "Grade": validated["grade"],
            "Best Angle": row.get("Best Angle", row.get("best_angle", validated.get("best_angle", ""))),
            "Confidence": validated["confidence"],
        }
    )
    return apply_sendability(row)


def _website_quality_from_status(status: str) -> str:
    if status in {"missing"}:
        return "N/A"
    if status in {"broken", "social_only", "jobber_only", "weak"}:
        return "Poor"
    if status == "strong":
        return "Good"
    return "Okay"
