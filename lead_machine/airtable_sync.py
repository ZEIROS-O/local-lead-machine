from __future__ import annotations

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


def build_airtable_payload(card: Dict[str, Any]) -> Dict[str, Any]:
    fields = dict(card.get("airtable_fields") or {})
    fields["Place Key"] = card.get("place_id_or_unique_key") or fields.get("Place Key", "")
    fields["Lead Priority"] = card.get("grade", fields.get("Lead Priority", ""))
    fields["AI Confidence"] = card.get("confidence", fields.get("AI Confidence", ""))
    fields["Offer Fit"] = card.get("best_angle", fields.get("Offer Fit", ""))
    fields["Recommended Action"] = card.get("recommended_action", fields.get("Recommended Action", ""))
    fields["Opener"] = card.get("opener", fields.get("Opener", ""))
    allowed = set(AIRTABLE_ALLOWED_FIELDS)
    return {
        "fields": {
            key: value
            for key, value in fields.items()
            if key in allowed and value not in ("", None, [], {})
        }
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
