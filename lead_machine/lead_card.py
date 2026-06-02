from __future__ import annotations

import json
from typing import Any, Dict, List


AIRTABLE_CARD_FIELDS = [
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
    "Outreach Channel",
    "Status",
    "Lead Priority",
    "Offer Fit",
    "Site Status",
    "Evidence Notes",
    "AI Confidence",
    "Skip Reason",
    "Desktop Screenshot Path",
    "Mobile Screenshot Path",
    "Score Total",
    "Recommended Action",
    "Place Key",
]


def _json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def build_lead_card(row: Dict[str, Any]) -> Dict[str, Any]:
    airtable_fields = {field: row.get(field, "") for field in AIRTABLE_CARD_FIELDS if row.get(field, "") != ""}

    return {
        "place_id_or_unique_key": row.get("Place Key") or row.get("Business Name", ""),
        "business_name": row.get("Business Name", ""),
        "grade": row.get("Lead Priority", ""),
        "confidence": row.get("AI Confidence", ""),
        "best_angle": row.get("Offer Fit") or row.get("Best Service Angle", ""),
        "why_this_lead": _json_list(row.get("Why This Lead")),
        "why_this_might_be_wrong": _json_list(row.get("Why This Might Be Wrong")),
        "recommended_action": row.get("Recommended Action", ""),
        "opener": row.get("Opener", ""),
        "evidence_links": _json_list(row.get("Evidence Refs")),
        "screenshots": {
            "desktop": row.get("Desktop Screenshot Path", ""),
            "mobile": row.get("Mobile Screenshot Path", ""),
        },
        "airtable_fields": airtable_fields,
    }

