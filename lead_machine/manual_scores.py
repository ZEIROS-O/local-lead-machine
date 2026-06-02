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
    return validated


def load_manual_score(path: Path, *, allowed_evidence_ids: Set[str]) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_manual_score(data, allowed_evidence_ids=allowed_evidence_ids)


def allowed_evidence_ids(enriched: Dict[str, Any]) -> Set[str]:
    evidence = enriched.get("evidence") or {}
    return {str(item.get("id")) for item in evidence.get("evidence_items", []) if item.get("id")}


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
    row.update(validated.get("airtable_fields") or {})
    return apply_sendability(row)


def _website_quality_from_status(status: str) -> str:
    if status in {"missing"}:
        return "N/A"
    if status in {"broken", "social_only", "jobber_only", "weak"}:
        return "Poor"
    if status == "strong":
        return "Good"
    return "Okay"
