from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Sequence, Set


MIN_SENDABLE_SCORE = 65
MIN_SENDABLE_CONFIDENCE = 0.75

SCORE_BLOCKER = "Score below B threshold"
CONVERSION_ISSUE_BLOCKER = "Missing concrete conversion issue"
DEMAND_BLOCKER = "Missing proof-of-demand signal"
ANGLE_BLOCKER = "Missing clear outreach angle"
OPENER_BLOCKER = "Missing evidence-backed opener"
EVIDENCE_COUNT_BLOCKER = "Not enough evidence references"
CONFIDENCE_BLOCKER = "Low confidence"
NO_SAFE_CLAIM_BLOCKER = "No safe outreach claim"
SAFE_CLAIM_REF_BLOCKER = "Safe outreach claim missing evidence reference"
INVALID_REF_BLOCKER = "Invalid evidence reference"

EVIDENCE_BLOCKERS = {
    CONVERSION_ISSUE_BLOCKER,
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

CONCRETE_ISSUE_FLAGS = {
    "homepage_fetch_failed",
    "non_standard_site_or_booking_url",
    "non_standard_site_or_booking",
    "no_form_detected",
    "no_clear_cta_detected",
    "missing_contact_path",
    "missing_estimate_path",
    "weak_mobile_path",
    "buried_phone",
    "phone_not_visible",
    "generic_copy_signals",
    "weak_services_page",
}

CONCRETE_ISSUE_FIELDS = {
    "site_status",
    "site_issue_flags",
    "contact_form",
    "cta_terms",
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

    if not has_concrete_conversion_issue(row):
        blockers.append(CONVERSION_ISSUE_BLOCKER)

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

    if updated.get("Packet Eligible") == "Yes" and not result["sendable"]:
        updated["Packet Eligible"] = ""

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
    if status in CONCRETE_SITE_STATUSES or quality in {"n/a", "poor"}:
        return True

    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)

    website_status = _clean(website.get("site_status")).lower()
    profile_status = _clean(evidence.get("site_status")).lower()
    if website_status in CONCRETE_SITE_STATUSES or profile_status in CONCRETE_SITE_STATUSES:
        return True

    flags = set(str(flag).lower() for flag in (website.get("site_issue_flags") or evidence.get("raw_flags") or []))
    if flags & CONCRETE_ISSUE_FLAGS:
        return True

    if evidence.get("has_contact_form") is False:
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
            return True
        if field == "site_issue_flags" and any(flag in value_text for flag in CONCRETE_ISSUE_FLAGS):
            return True
        if field in {"contact_form", "phone_visible"} and value is False:
            return True
        if field == "cta_terms" and value in ("", None, [], {}):
            return True
        if field == "third_party_tools" and any(tool in value_text for tool in ("jobber", "housecall", "leadconnector")):
            return True

    return False


def has_demand_signal(row: Dict[str, Any]) -> bool:
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
