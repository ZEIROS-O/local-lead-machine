from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

from .schema import normalize_phone


AIRTABLE_ALLOWED_FIELDS = [
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
    "Current Website Quality",
    "Best Service Angle",
    "Specific Observation",
    "Mockup URL",
    "Video URL",
    "Outreach Channel",
    "Status",
    "Last Contacted",
    "Next Follow-Up",
    "Assigned To",
    "Lead Priority",
    "Offer Fit",
    "Site Status",
    "Contact Form Detected",
    "Estimate Form Detected",
    "Form Strength",
    "Form Detection Method",
    "CTA Strength",
    "Visible Pain Score",
    "Visible Pain Reasons",
    "Send Trigger",
    "Money Signal",
    "Buyer Intent Signals",
    "AI Confidence",
    "Skip Reason",
    "Place Key",
    "Score Total",
    "Sendable",
    "Packet Eligible",
    "Opener",
    "Recommended Action",
    "Send Blockers",
    "Packet Path",
    "Next Action",
]

INTEGER_FIELDS = {"Reviews", "Score Total", "Visible Pain Score"}
FLOAT_FIELDS = {"Rating"}
AI_CONFIDENCE_FIELD = "AI Confidence"
AI_CONFIDENCE_LABELS = {"low", "medium", "high"}
SITE_STATUS_FIELD = "Site Status"
SITE_STATUS_LABELS = {
    "missing": "Missing",
    "broken": "Broken",
    "unknown": "Unknown",
    "no website": "No Website",
    "loaded": "Loaded",
    "loads": "Loaded",
    "redirect issue": "Redirect Issue",
    "social only": "Social Only",
    "facebook only": "Social Only",
}
OFFER_FIT_FIELD = "Offer Fit"
BEST_SERVICE_ANGLE_FIELD = "Best Service Angle"
SERVICE_ANGLE_FIELDS = {OFFER_FIT_FIELD, BEST_SERVICE_ANGLE_FIELD}
SERVICE_ANGLE_LABELS = {
    "social/jobber escape": "No-Site Launch",
    "no-site launch": "No-Site Launch",
    "no site launch": "No-Site Launch",
    "social jobber escape": "No-Site Launch",
    "owned weak site rebuild": "Website Rebuild",
    "premium perception gap": "Website Rebuild",
    "portfolio presentation gap": "Website Rebuild",
    "website rebuild": "Website Rebuild",
    "estimate funnel": "Estimate Funnel",
    "optimization only": "Automation / Follow-Up",
    "automation / follow up": "Automation / Follow-Up",
    "automation/follow up": "Automation / Follow-Up",
    "automation / follow-up": "Automation / Follow-Up",
    "automation/follow-up": "Automation / Follow-Up",
}
TYPED_AIRTABLE_FIELDS = (
    INTEGER_FIELDS | FLOAT_FIELDS | {AI_CONFIDENCE_FIELD, SITE_STATUS_FIELD} | SERVICE_ANGLE_FIELDS
)


def _parse_finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = str(value).strip()
        if cleaned == "":
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    if not math.isfinite(number):
        return None
    return number


def normalize_airtable_value(field: str, value: Any) -> Any:
    if field == AI_CONFIDENCE_FIELD:
        label = str(value or "").strip().casefold()
        if label in AI_CONFIDENCE_LABELS:
            return label
        number = _parse_finite_float(value)
        if number is None:
            return None
        if number >= 0.80:
            return "high"
        if number >= 0.55:
            return "medium"
        return "low"

    if field == SITE_STATUS_FIELD:
        label = " ".join(str(value or "").strip().casefold().replace("_", " ").split())
        return SITE_STATUS_LABELS.get(label)

    if field in SERVICE_ANGLE_FIELDS:
        label = " ".join(str(value or "").strip().casefold().replace("_", " ").split())
        return SERVICE_ANGLE_LABELS.get(label)

    if field in INTEGER_FIELDS:
        number = _parse_finite_float(value)
        if number is None:
            return None
        return int(number)

    if field in FLOAT_FIELDS:
        return _parse_finite_float(value)

    return value


def _first_non_blank(*values: Any) -> Any:
    for value in values:
        if value not in ("", None, [], {}):
            return value
    return ""


def build_airtable_payload(card: Dict[str, Any]) -> Dict[str, Any]:
    fields = dict(card.get("airtable_fields") or {})
    fields["Place Key"] = card.get("place_id_or_unique_key") or fields.get("Place Key", "")
    fields["Lead Priority"] = card.get("grade", fields.get("Lead Priority", ""))
    fields["AI Confidence"] = _first_non_blank(card.get("confidence"), fields.get("AI Confidence", ""))
    fields["Offer Fit"] = card.get("best_angle", fields.get("Offer Fit", ""))
    fields["Recommended Action"] = card.get("recommended_action", fields.get("Recommended Action", ""))
    fields["Opener"] = card.get("opener", fields.get("Opener", ""))
    allowed = set(AIRTABLE_ALLOWED_FIELDS)
    payload_fields: Dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        normalized_value = normalize_airtable_value(key, value)
        if normalized_value in ("", None, [], {}):
            continue
        payload_fields[key] = normalized_value
    return {
        "fields": payload_fields
    }


def card_place_key(card: Dict[str, Any]) -> str:
    fields = card.get("airtable_fields") or {}
    return str(card.get("place_id_or_unique_key") or fields.get("Place Key") or "").strip()


def card_upsert_key(card: Dict[str, Any]) -> str:
    explicit = str(card.get("upsert_key") or "").strip()
    if explicit:
        return explicit

    place_key = card_place_key(card)
    if place_key:
        return f"place:{place_key}"

    fields = card.get("airtable_fields") or {}
    phone = normalize_phone(str(card.get("phone") or fields.get("Phone") or ""))
    if phone:
        return f"phone:{phone}"

    return ""


def plan_airtable_sync(
    cards: Iterable[Dict[str, Any]],
    *,
    existing_records_by_key: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    existing = existing_records_by_key or {}
    planned: List[Dict[str, Any]] = []
    seen_input_keys: set[str] = set()
    for card in cards:
        place_key = card_place_key(card)
        upsert_key = card_upsert_key(card)
        if not upsert_key:
            raise ValueError("Airtable record missing upsert key")
        if upsert_key in seen_input_keys:
            raise ValueError(f"Duplicate Airtable upsert key in input: {upsert_key}")
        seen_input_keys.add(upsert_key)
        record_id = existing.get(upsert_key) or existing.get(place_key)
        payload = build_airtable_payload(card)
        if record_id:
            fields = payload.get("fields", {})
            for field in card.get("defaulted_airtable_fields") or []:
                fields.pop(str(field), None)
        planned.append(
            {
                "method": "PATCH" if record_id else "POST",
                "record_id": record_id or "",
                "place_key": place_key,
                "upsert_key": upsert_key,
                "payload": payload,
            }
        )
    return planned
