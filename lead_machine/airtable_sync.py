from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .schema import GENERIC_DOMAINS, normalize_domain, normalize_phone
from .verticals import VERTICAL_PROFILES, clean_vertical_label


AIRTABLE_ALLOWED_FIELDS = [
    "Business Name",
    "Owner Name",
    "Vertical",
    "City",
    "Metro Context",
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
    "Consent Posture",
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
    "Commercial Fit State",
    "Commercial Materiality",
    "Recurring Consequence",
    "Root Cause Confidence",
    "LPM Offer Component",
    "Packageable Solution",
    "Value Mechanism",
    "Beyond Free Fix",
    "Self Fix Risk",
    "Intentionality",
    "Contact Eligibility",
    "Asset State",
    "Promise State",
    "Asset ID",
    "Asset Verifier Verdict",
    "Evidence Refs",
    "Safe Outreach Claims",
    "Evidence Snapshot",
    "Review Demand Band",
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
VERTICAL_FIELD = "Vertical"
IDENTITY_VERTICAL_SELECT_ALIASES = {
    "epoxy": ("epoxy", "epoxy flooring", "garage floor coating"),
    "landscaping": ("landscaping", "landscape"),
    "pressure_washing": ("pressure washing", "pressure_washing"),
    "roofing": ("roofing", "roof replacement", "roof repair"),
}
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
    INTEGER_FIELDS | FLOAT_FIELDS | {AI_CONFIDENCE_FIELD, VERTICAL_FIELD, SITE_STATUS_FIELD} | SERVICE_ANGLE_FIELDS
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


def _clean_label(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("_", " ").split())


def _vertical_label_lookup() -> Dict[str, str]:
    # Keyed with the same cleaner verticals.resolve_profile uses so the write
    # path and profile resolution never disagree on a spelling.
    labels: Dict[str, str] = {}
    for profile in VERTICAL_PROFILES.values():
        aliases = {clean_vertical_label(profile.key), clean_vertical_label(profile.label), *profile.aliases}
        for alias in aliases:
            cleaned = clean_vertical_label(alias)
            if cleaned:
                labels[cleaned] = profile.key
    for key, aliases in IDENTITY_VERTICAL_SELECT_ALIASES.items():
        labels[clean_vertical_label(key)] = key
        for alias in aliases:
            cleaned = clean_vertical_label(alias)
            if cleaned:
                labels[cleaned] = key
    return labels


VERTICAL_LABELS = _vertical_label_lookup()


def resolve_vertical_label(value: str) -> str:
    """Resolve any human phrase or slug to the Airtable Vertical slug.

    Used by the read side (query_ready_to_send.py) so `--vertical "water restoration"`
    and `--vertical water_restoration` return the same filter. Unlike the write-path
    lookup in normalize_airtable_value, this never returns empty on an unrecognized
    niche — it falls back to a slugified guess so a brand-new vertical (one added to
    VerticalProfile/VERTICAL_LABELS after this was written, or simply not aliased yet)
    still produces a usable filter value instead of matching nothing silently.
    """
    label = clean_vertical_label(value)
    if not label:
        return ""
    resolved = VERTICAL_LABELS.get(label)
    if resolved:
        return resolved
    return label.replace(" ", "_")


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

    if field == VERTICAL_FIELD:
        return VERTICAL_LABELS.get(clean_vertical_label(value))

    if field == SITE_STATUS_FIELD:
        return SITE_STATUS_LABELS.get(_clean_label(value))

    if field in SERVICE_ANGLE_FIELDS:
        return SERVICE_ANGLE_LABELS.get(_clean_label(value))

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


def build_airtable_payload_with_drops(card: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    fields = dict(card.get("airtable_fields") or {})
    fields["Place Key"] = card.get("place_id_or_unique_key") or fields.get("Place Key", "")
    fields["Lead Priority"] = card.get("grade", fields.get("Lead Priority", ""))
    fields["AI Confidence"] = _first_non_blank(card.get("confidence"), fields.get("AI Confidence", ""))
    fields["Offer Fit"] = card.get("best_angle", fields.get("Offer Fit", ""))
    fields["Recommended Action"] = card.get("recommended_action", fields.get("Recommended Action", ""))
    fields["Opener"] = card.get("opener", fields.get("Opener", ""))
    allowed = set(AIRTABLE_ALLOWED_FIELDS)
    payload_fields: Dict[str, Any] = {}
    dropped_fields: List[Dict[str, Any]] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        normalized_value = normalize_airtable_value(key, value)
        if normalized_value in ("", None, [], {}):
            if key in TYPED_AIRTABLE_FIELDS and value not in ("", None, [], {}):
                dropped_fields.append({"field": key, "value": value})
            continue
        payload_fields[key] = normalized_value
    return {"fields": payload_fields}, dropped_fields


def build_airtable_payload(card: Dict[str, Any]) -> Dict[str, Any]:
    payload, _dropped_fields = build_airtable_payload_with_drops(card)
    return payload


def card_place_key(card: Dict[str, Any]) -> str:
    fields = card.get("airtable_fields") or {}
    return str(card.get("place_id_or_unique_key") or fields.get("Place Key") or "").strip()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_business_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).casefold())


def normalize_city(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).casefold())


def card_phone_key(card: Dict[str, Any]) -> str:
    fields = card.get("airtable_fields") or {}
    phone = normalize_phone(str(card.get("phone") or fields.get("Phone") or ""))
    return f"phone:{phone}" if phone else ""


def card_domain_key(card: Dict[str, Any]) -> str:
    fields = card.get("airtable_fields") or {}
    domain = normalize_domain(str(card.get("website") or fields.get("Website URL") or ""))
    if domain in GENERIC_DOMAINS:
        return ""
    return f"domain:{domain}" if domain else ""


def card_business_city_key(card: Dict[str, Any]) -> str:
    fields = card.get("airtable_fields") or {}
    name = normalize_business_name(
        card.get("business_name") or fields.get("Business Name") or card.get("name") or ""
    )
    city = normalize_city(card.get("city") or fields.get("City") or "")
    if not name or not city:
        return ""
    return f"business_city:{name}|{city}"


def card_upsert_key(card: Dict[str, Any]) -> str:
    explicit = str(card.get("upsert_key") or "").strip()
    if explicit:
        return explicit

    place_key = card_place_key(card)
    if place_key:
        return f"place:{place_key}"

    phone_key = card_phone_key(card)
    if phone_key:
        return phone_key

    return ""


def _record_ids_for_key(existing: Mapping[str, Any], key: str) -> List[str]:
    if not key:
        return []
    raw = existing.get(key)
    if raw in ("", None):
        return []
    if isinstance(raw, (list, tuple, set)):
        return sorted({str(item) for item in raw if str(item)})
    return [str(raw)]


def _resolve_existing_record(
    card: Dict[str, Any],
    existing: Mapping[str, Any],
    upsert_key: str,
    place_key: str,
) -> tuple[str, str, str]:
    candidates = [
        ("exact", upsert_key),
        ("exact", f"place:{place_key}" if place_key else ""),
        ("exact", place_key),
        ("phone", card_phone_key(card)),
        ("domain", card_domain_key(card)),
        ("business_city", card_business_city_key(card)),
    ]
    seen_keys: set[str] = set()
    for reason, key in candidates:
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        record_ids = _record_ids_for_key(existing, key)
        if not record_ids:
            continue
        if len(record_ids) > 1:
            return "", reason, (
                f"Ambiguous Airtable existing match for {upsert_key}: "
                f"{key} matched {', '.join(record_ids)}"
            )
        return record_ids[0], "" if reason == "exact" else reason, ""
    return "", "", ""


def plan_airtable_sync(
    cards: Iterable[Dict[str, Any]],
    *,
    existing_records_by_key: Optional[Mapping[str, Any]] = None,
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
        record_id, match_reason, warning = _resolve_existing_record(card, existing, upsert_key, place_key)
        payload, dropped_fields = build_airtable_payload_with_drops(card)
        if warning:
            planned.append(
                {
                    "method": "SKIP",
                    "record_id": "",
                    "place_key": place_key,
                    "upsert_key": upsert_key,
                    "match_reason": match_reason,
                    "warning": warning,
                    "payload": payload,
                }
            )
            if dropped_fields:
                planned[-1]["dropped_fields"] = dropped_fields
            continue
        if record_id:
            fields = payload.get("fields", {})
            for field in card.get("defaulted_airtable_fields") or []:
                fields.pop(str(field), None)
        item = {
            "method": "PATCH" if record_id else "POST",
            "record_id": record_id or "",
            "place_key": place_key,
            "upsert_key": upsert_key,
            "payload": payload,
        }
        if match_reason:
            item["match_reason"] = match_reason
        if dropped_fields:
            item["dropped_fields"] = dropped_fields
        planned.append(item)
    return planned
