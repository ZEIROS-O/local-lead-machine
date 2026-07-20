from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from .buyer_signals import buyer_intent_summary, ensure_buyer_intent, has_money_signal, review_demand_band
from .commercial_backfill import backfill_legacy_commercial_fit
from .email_hygiene import emails_text
from .score_schema import (
    ASSET_STATES,
    COMMERCIAL_DIMENSION_KEYS,
    COMMERCIAL_FIT_STATES,
    CONTACT_ELIGIBILITY_STATES,
    PROMISE_STATES,
    SCORE_SCHEMA_VERSION,
    V2_SCORE_FIELDS,
    is_truly_legacy_score,
)
from .schema import normalize_lead
from .sendability import apply_sendability, normalize_evidence_refs, normalize_safe_outreach_claims, normalize_unsafe_claims
from .scoring_v3 import attach_scoring_v3


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
SAFE_AIRTABLE_OVERRIDE_KEYS = {
    "Business Name",
    "business_name",
    "Phone",
    "phone",
    "City",
    "city",
    "Vertical",
    "vertical",
    "Category",
    "category",
    "Best Service Angle",
    "best_angle",
    "Recommended Action",
    "recommended_action",
}


def _require_keys(data: Dict[str, Any], keys: Iterable[str]) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise ValueError(f"Manual score missing required keys: {', '.join(missing)}")


def _canonicalize_evidence_refs(refs: List[str], allowed_evidence_ids: Set[str]) -> List[str]:
    aliases: Dict[str, List[str]] = {}
    for allowed_id in sorted(allowed_evidence_ids):
        alias = re.sub(r"[-_]+", "-", allowed_id.casefold())
        aliases.setdefault(alias, []).append(allowed_id)

    canonical: List[str] = []
    for ref in refs:
        resolved = ref
        if ref not in allowed_evidence_ids:
            matches = aliases.get(re.sub(r"[-_]+", "-", ref.casefold()), [])
            if len(matches) == 1:
                resolved = matches[0]
        if resolved not in canonical:
            canonical.append(resolved)
    return canonical


def validate_manual_score(
    data: Dict[str, Any],
    *,
    allowed_evidence_ids: Set[str],
    require_schema_v2: bool = False,
) -> Dict[str, Any]:
    schema_value = data.get("score_schema_version")
    legacy = schema_value is None
    if legacy:
        if require_schema_v2:
            raise ValueError(f"score_schema_version must be {SCORE_SCHEMA_VERSION}")
        if not is_truly_legacy_score(data):
            partial = sorted(set(data) & V2_SCORE_FIELDS)
            raise ValueError(
                "Unversioned score partially populates the v2 commercial contract: "
                + ", ".join(partial)
            )
    elif type(schema_value) is not int or schema_value != SCORE_SCHEMA_VERSION:
        raise ValueError(f"score_schema_version must be {SCORE_SCHEMA_VERSION}")

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

    refs = _canonicalize_evidence_refs(
        normalize_evidence_refs(data.get("evidence_refs")),
        allowed_evidence_ids,
    )
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
    if legacy:
        validated["_legacy_score_compatibility"] = True
    else:
        _require_keys(data, V2_SCORE_FIELDS)
        validated["score_schema_version"] = SCORE_SCHEMA_VERSION
        raw_commercial_state = str(data.get("commercial_fit_state") or "").strip()
        commercial_state = next(
            (
                state
                for state in COMMERCIAL_FIT_STATES
                if state.casefold() == raw_commercial_state.casefold()
            ),
            raw_commercial_state.lower(),
        )
        contact = str(data.get("contact_eligibility") or "").strip().lower()
        asset = str(data.get("asset_state") or "").strip().lower()
        promise = str(data.get("promise_state") or "").strip().lower()
        if commercial_state not in COMMERCIAL_FIT_STATES:
            raise ValueError(f"invalid commercial_fit_state: {commercial_state or '<missing>'}")
        if contact not in CONTACT_ELIGIBILITY_STATES:
            raise ValueError(f"invalid contact_eligibility: {contact or '<missing>'}")
        if asset not in ASSET_STATES:
            raise ValueError(f"invalid asset_state: {asset or '<missing>'}")
        if promise not in PROMISE_STATES:
            raise ValueError(f"invalid promise_state: {promise or '<missing>'}")
        if contact == "contact-now" and commercial_state != "commercially-sendable":
            raise ValueError("contact-now requires commercially-sendable")
        asset_id = str(data.get("asset_id") or "").strip()
        verifier = str(data.get("asset_verifier_verdict") or "").strip()
        if asset == "verified-existing" and (not asset_id or verifier != "PASS"):
            raise ValueError("verified-existing requires asset_id and PASS asset_verifier_verdict")
        if promise == "existing-verified-asset" and asset != "verified-existing":
            raise ValueError("existing-verified-asset requires verified-existing asset_state")
        if contact == "contact-after-asset" and asset != "planned":
            raise ValueError("contact-after-asset requires planned asset_state")
        if promise == "future-build" and asset != "planned":
            raise ValueError("future-build requires planned asset_state")
        validated["commercial_fit_state"] = commercial_state
        for key in COMMERCIAL_DIMENSION_KEYS:
            validated[key] = str(data.get(key) or "").strip()
        if commercial_state == "commercially-sendable":
            missing_dimensions = [key for key in COMMERCIAL_DIMENSION_KEYS if not validated[key]]
            if missing_dimensions:
                raise ValueError(
                    "commercially-sendable missing dimensions: " + ", ".join(missing_dimensions)
                )
        validated["contact_eligibility"] = contact
        validated["asset_state"] = asset
        validated["promise_state"] = promise
        validated["asset_id"] = asset_id
        validated["asset_verifier_verdict"] = verifier
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


def _metro_context(enriched: Dict[str, Any]) -> str:
    google = enriched.get("google") or {}
    source = " ".join(str(google.get(key) or "").lower().replace("_", " ") for key in ("source_batch", "Source Batch", "source_geo", "Source Geo"))
    profile_dir = Path(__file__).resolve().parents[1] / "market_profiles"
    matches = [path.stem for path in profile_dir.glob("*.json") if re.search(rf"(?<![a-z0-9]){re.escape(path.stem.replace('_', ' '))}(?![a-z0-9])", source)]
    return matches[0].replace("_", "-") if len(set(matches)) == 1 else ""


def _first_non_empty_field(fields: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key not in fields:
            continue
        value = _clean_manual_field(fields.get(key))
        if value:
            return value
    return ""


def _safe_airtable_overrides(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in fields.items() if key in SAFE_AIRTABLE_OVERRIDE_KEYS}


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
        ("Best Service Angle", "best_angle", ("Best Service Angle", "best_angle")),
        ("Recommended Action", "recommended_action", ("Recommended Action", "recommended_action")),
    ]

    for user_key, internal_key, manual_keys in identity_fields:
        value = (
            _first_non_empty_field(airtable_fields, *manual_keys)
            or _clean_manual_field(base_values.get(user_key))
            or _clean_manual_field(row.get(user_key))
        )
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
    allowed_extra_evidence_ids: Set[str] | None = None,
) -> Dict[str, Any]:
    allowed = allowed_evidence_ids(enriched)
    if allowed_extra_evidence_ids:
        allowed.update(str(ref) for ref in allowed_extra_evidence_ids if str(ref).strip())
    validated = validate_manual_score(score, allowed_evidence_ids=allowed)
    google = enriched.get("google", {}) or {}
    website = enriched.get("website", {}) or {}
    evidence = enriched.get("evidence", {}) or {}
    ensure_buyer_intent(enriched)
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
    visual_pain_reasons = evidence.get("visual_pain_reasons") or []
    if isinstance(visual_pain_reasons, str):
        visual_pain_reasons = [part.strip() for part in visual_pain_reasons.split(";") if part.strip()]
    visual_pain_reasons_text = "; ".join(str(reason) for reason in visual_pain_reasons if str(reason).strip())

    email = ""
    if website.get("emails_found"):
        email = emails_text(
            website.get("emails_found") or [],
            business_domain=website.get("domain") or website.get("final_url") or lead.website,
        )

    packet_eligible = "Yes" if validated["grade"] in {"A", "B"} else ""
    buyer_intent_signals = buyer_intent_summary(enriched)
    demand_band = review_demand_band(enriched, vertical=lead.vertical)
    row = {
        "Business Name": lead.business_name,
        "Owner Name": "",
        "Vertical": lead.vertical,
        "City": lead.city,
        "Metro Context": _metro_context(enriched),
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
        "Outreach Channel": "Phone" if lead.phone else "Email" if email else "",
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
        "Rendered Site Status": evidence.get("rendered_site_status", ""),
        "rendered_site_status": evidence.get("rendered_site_status", ""),
        "Rendered Status Reason": evidence.get("rendered_status_reason", ""),
        "rendered_status_reason": evidence.get("rendered_status_reason", ""),
        "Desktop Screenshot Path": evidence.get("desktop_screenshot_path", "")
        or evidence.get("screenshots", {}).get("desktop", ""),
        "desktop_screenshot_path": evidence.get("desktop_screenshot_path", "")
        or evidence.get("screenshots", {}).get("desktop", ""),
        "Mobile Screenshot Path": evidence.get("mobile_screenshot_path", "")
        or evidence.get("screenshots", {}).get("mobile", ""),
        "mobile_screenshot_path": evidence.get("mobile_screenshot_path", "")
        or evidence.get("screenshots", {}).get("mobile", ""),
        "Visual Review Needed": evidence.get("visual_review_needed", ""),
        "visual_review_needed": evidence.get("visual_review_needed", ""),
        "Visual Quality Score": evidence.get("visual_quality_score", ""),
        "visual_quality_score": evidence.get("visual_quality_score", ""),
        "Premium Perception Score": evidence.get("premium_perception_score", ""),
        "premium_perception_score": evidence.get("premium_perception_score", ""),
        "Mobile Visual Score": evidence.get("mobile_visual_score", ""),
        "mobile_visual_score": evidence.get("mobile_visual_score", ""),
        "Hero Clarity Score": evidence.get("hero_clarity_score", ""),
        "hero_clarity_score": evidence.get("hero_clarity_score", ""),
        "CTA Visibility Score": evidence.get("cta_visibility_score", ""),
        "cta_visibility_score": evidence.get("cta_visibility_score", ""),
        "Gallery Visibility Score": evidence.get("gallery_visibility_score", ""),
        "gallery_visibility_score": evidence.get("gallery_visibility_score", ""),
        "Trust Proof Visibility Score": evidence.get("trust_proof_visibility_score", ""),
        "trust_proof_visibility_score": evidence.get("trust_proof_visibility_score", ""),
        "Visual Pain Reasons": visual_pain_reasons_text,
        "visual_pain_reasons": visual_pain_reasons,
        "Visible Pain Score": visible_pain_score,
        "visible_pain_score": visible_pain_score,
        "Visible Pain Reasons": visible_pain_reasons_text,
        "visible_pain_reasons": visible_pain_reasons,
        "Visible Pain Categories": visible_pain_categories_text,
        "visible_pain_categories": visible_pain_categories,
        "Send Trigger": send_trigger,
        "send_trigger": send_trigger,
        "Money Signal": "Yes" if has_money_signal(enriched) else "No",
        "money_signal": "Yes" if has_money_signal(enriched) else "No",
        "Buyer Intent Signals": buyer_intent_signals,
        "buyer_intent_signals": buyer_intent_signals,
        "Review Fit Band": demand_band,
        "Review Demand Band": demand_band,
        "Evidence Notes": "; ".join(str(ref) for ref in validated.get("evidence_refs", [])),
        "AI Confidence": validated["confidence"],
        "Manual Reviewed": "",
        "Skip Reason": "; ".join(validated.get("hard_disqualifiers") or []) if validated["grade"] == "Skip" else "",
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
        "Evidence Snapshot": json.dumps(enriched.get("evidence") or {}, ensure_ascii=False),
        "Safe Outreach Claims": json.dumps(validated.get("safe_outreach_claims") or [], ensure_ascii=False),
        "Unsafe Claims": json.dumps(validated.get("unsafe_claims") or [], ensure_ascii=False),
        "Score Schema Version": validated.get("score_schema_version", ""),
        "score_schema_version": validated.get("score_schema_version", ""),
        "Commercial Fit Source": "llm-schema-v2" if not validated.get("_legacy_score_compatibility") else "",
        "commercial_fit_source": "llm-schema-v2" if not validated.get("_legacy_score_compatibility") else "",
        "Commercial Fit State": validated.get("commercial_fit_state", "unverified"),
        "commercial_fit_state": validated.get("commercial_fit_state", "unverified"),
        "Commercial Materiality": validated.get("commercial_materiality", ""),
        "Recurring Consequence": validated.get("recurring_consequence", ""),
        "Root Cause Confidence": validated.get("root_cause_confidence", ""),
        "LPM Offer Component": validated.get("offer_component", ""),
        "Packageable Solution": validated.get("packageable_solution", ""),
        "Value Mechanism": validated.get("value_mechanism", ""),
        "Beyond Free Fix": validated.get("beyond_free_fix", ""),
        "Self Fix Risk": validated.get("self_fix_risk", ""),
        "Intentionality": validated.get("intentionality", ""),
        "Contact Eligibility": validated.get("contact_eligibility", ""),
        "Asset State": validated.get("asset_state", ""),
        "Promise State": validated.get("promise_state", ""),
        "Asset ID": validated.get("asset_id", ""),
        "asset_id": validated.get("asset_id", ""),
        "Asset Verifier Verdict": validated.get("asset_verifier_verdict", ""),
        "asset_verifier_verdict": validated.get("asset_verifier_verdict", ""),
        "Place Key": lead.place_id_or_unique_key,
        "Packet Eligible": packet_eligible,
        "Packet Eligibility Reason": "Manual grade A/B" if packet_eligible else "Manual grade below B threshold",
    }
    commercial_aliases = {
        "Commercial Materiality": "commercial_materiality",
        "Recurring Consequence": "recurring_consequence",
        "Root Cause Confidence": "root_cause_confidence",
        "LPM Offer Component": "offer_component",
        "Packageable Solution": "packageable_solution",
        "Value Mechanism": "value_mechanism",
        "Beyond Free Fix": "beyond_free_fix",
        "Self Fix Risk": "self_fix_risk",
        "Intentionality": "intentionality",
        "Contact Eligibility": "contact_eligibility",
        "Asset State": "asset_state",
        "Promise State": "promise_state",
        "Asset ID": "asset_id",
        "Asset Verifier Verdict": "asset_verifier_verdict",
    }
    for title, snake in commercial_aliases.items():
        value = row.get(title, validated.get(snake, ""))
        row[title] = value
        row[snake] = value
    extra_refs_used = set(validated.get("evidence_refs") or []) - allowed_evidence_ids(enriched)
    for claim in validated.get("safe_outreach_claims") or []:
        extra_refs_used.update(set(claim.get("evidence_refs") or []) - allowed_evidence_ids(enriched))
    if extra_refs_used:
        sorted_allowed = sorted(allowed)
        row["Known Evidence IDs"] = json.dumps(sorted_allowed, ensure_ascii=False)
        row["known_evidence_ids"] = sorted_allowed
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
    airtable_overrides = _safe_airtable_overrides(airtable_fields)
    _sync_manual_airtable_aliases(row, airtable_overrides, base_alias_values)
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
    if validated.get("_legacy_score_compatibility"):
        row = backfill_legacy_commercial_fit(row)
    governed = apply_sendability(row)
    return attach_scoring_v3(enriched, governed, fallback_vertical=fallback_vertical)


def _website_quality_from_status(status: str) -> str:
    if status in {"missing"}:
        return "N/A"
    if status in {"broken", "social_only", "jobber_only", "weak"}:
        return "Poor"
    if status == "strong":
        return "Good"
    return "Okay"
