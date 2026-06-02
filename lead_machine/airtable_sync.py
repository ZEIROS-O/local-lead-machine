from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def build_airtable_payload(card: Dict[str, Any]) -> Dict[str, Any]:
    fields = dict(card.get("airtable_fields") or {})
    fields["Place Key"] = card.get("place_id_or_unique_key", "")
    fields["Lead Priority"] = card.get("grade", fields.get("Lead Priority", ""))
    fields["AI Confidence"] = card.get("confidence", fields.get("AI Confidence", ""))
    fields["Offer Fit"] = card.get("best_angle", fields.get("Offer Fit", ""))
    fields["Recommended Action"] = card.get("recommended_action", fields.get("Recommended Action", ""))
    fields["Opener"] = card.get("opener", fields.get("Opener", ""))
    return {"fields": {key: value for key, value in fields.items() if value not in ("", None, [], {})}}


def plan_airtable_sync(
    cards: Iterable[Dict[str, Any]],
    *,
    existing_records_by_key: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    existing = existing_records_by_key or {}
    planned: List[Dict[str, Any]] = []
    for card in cards:
        key = str(card.get("place_id_or_unique_key") or "")
        record_id = existing.get(key)
        planned.append(
            {
                "method": "PATCH" if record_id else "POST",
                "record_id": record_id or "",
                "place_key": key,
                "payload": build_airtable_payload(card),
            }
        )
    return planned

