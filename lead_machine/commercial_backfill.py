from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .score_schema import COMMERCIAL_DIMENSION_KEYS, SCORE_SCHEMA_VERSION


COMMERCIAL_FIELD_ALIASES = {
    "commercial_materiality": "Commercial Materiality",
    "recurring_consequence": "Recurring Consequence",
    "root_cause_confidence": "Root Cause Confidence",
    "offer_component": "LPM Offer Component",
    "packageable_solution": "Packageable Solution",
    "value_mechanism": "Value Mechanism",
    "beyond_free_fix": "Beyond Free Fix",
    "self_fix_risk": "Self Fix Risk",
    "intentionality": "Intentionality",
}

HARD_STRUCTURAL_STATUSES = {
    "missing",
    "no website",
    "no_website",
    "social only",
    "social_only",
    "facebook only",
    "facebook_only",
    "jobber only",
    "jobber_only",
}
BROKEN_STATUSES = {"broken", "broken website", "broken_website"}
HARD_FAILURE_FLAGS = {
    "dns_domain_failure",
    "homepage_http_hard_failure",
    "ssl_cert_hard_failure",
    "broken_redirect",
    "rendered_load_failed",
    "parked_dead_domain",
}

PATH_GROUP_FLAGS = {
    "form": {
        "no_form_detected",
        "missing_contact_path",
        "missing_estimate_path",
        "weak_basic_form",
        "only_phone_email_path",
        "broken_form",
        "form_error",
    },
    "cta": {"no_clear_cta_detected", "buried_cta"},
    "mobile": {"weak_mobile_path", "bad_mobile_status", "mobile_usability_pain", "phone_not_visible"},
    "trust": {"low_visible_trust", "reviews_not_near_cta", "trust_proof_pain"},
    "service": {
        "generic_copy_signals",
        "weak_services_page",
        "unclear_service_pages",
        "high_ticket_services_unclear",
        "service_clarity_pain",
    },
    "follow-up": {"followup_or_missed_call_pain", "missed_call_path", "followup_gap"},
}

PATH_GROUP_PATTERNS = {
    "form": (r"\b(no|missing|weak|basic|broken) (contact |estimate )?form\b", r"phone/email[- ]only"),
    "cta": (r"\b(no|missing|buried|weak|unclear) .{0,12}\bcta\b", r"call[- ]to[- ]action"),
}

ISOLATED_PATTERNS = (
    r"\bphone (number )?(mismatch|inconsistent)\b",
    r"\bphone/routing\b.{0,40}\b(differs|mismatch|inconsistent)\b",
    r"\bcosmetic\b",
    r"\b(one|single|isolated) broken link\b",
    r"\bbasic[- ]form[- ]only\b",
)

HIGH_TICKET_NAME_TERMS = {
    "ceramic",
    "coating",
    "epoxy",
    "floor",
    "paint correction",
    "ppf",
    "restoration",
    "tint",
    "wrap",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("_", " ").split())


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif value not in (None, ""):
        yield str(value)


def _evidence_documents(row: dict[str, Any]) -> list[Any]:
    docs: list[Any] = [row]
    for key in ("Raw Evidence", "raw_evidence", "Evidence Snapshot", "evidence_snapshot"):
        parsed = _json_value(row.get(key))
        if parsed is not None:
            docs.append(parsed)
    return docs


def _evidence_blob(row: dict[str, Any]) -> str:
    return " ".join(_clean(value) for doc in _evidence_documents(row) for value in _walk_strings(doc))


def _evidence_flags(row: dict[str, Any]) -> set[str]:
    flags: set[str] = set()
    for doc in _evidence_documents(row):
        if not isinstance(doc, dict):
            continue
        candidates = [doc]
        for key in ("evidence", "website"):
            child = doc.get(key)
            if isinstance(child, dict):
                candidates.append(child)
        for candidate in candidates:
            for key in ("site_issue_flags", "raw_flags", "visible_pain_categories", "visual_pain_reasons"):
                raw = candidate.get(key)
                if isinstance(raw, str):
                    raw = re.split(r"[;,]", raw)
                if isinstance(raw, list):
                    flags.update(str(value).strip().casefold().replace(" ", "_") for value in raw if str(value).strip())
    return flags


def _site_status(row: dict[str, Any]) -> str:
    direct = _clean(_first(row, "Site Status", "site_status"))
    if direct:
        return direct
    for doc in _evidence_documents(row):
        if not isinstance(doc, dict):
            continue
        for candidate in (doc.get("evidence"), doc.get("website")):
            if isinstance(candidate, dict):
                status = _clean(candidate.get("site_status"))
                if status:
                    return status
    return ""


def _path_failure_groups(row: dict[str, Any], flags: set[str], blob: str) -> set[str]:
    groups = {
        group
        for group, group_flags in PATH_GROUP_FLAGS.items()
        if flags & group_flags
    }
    for group, patterns in PATH_GROUP_PATTERNS.items():
        if any(re.search(pattern, blob) for pattern in patterns):
            groups.add(group)

    form_strength = _clean(_first(row, "Form Strength", "form_strength"))
    if form_strength in {"weak", "basic", "broken"}:
        groups.add("form")
    cta_strength = _clean(_first(row, "CTA Strength", "cta_strength"))
    if cta_strength in {"weak", "missing", "none", "buried"}:
        groups.add("cta")
    return groups


def _offer_component(row: dict[str, Any]) -> str:
    angle = _clean(_first(row, "Best Service Angle", "Offer Fit", "best_angle"))
    if any(value in angle for value in ("no site", "rebuild", "premium", "portfolio", "service")):
        return "Get Found"
    if any(value in angle for value in ("estimate", "quote", "lead", "mobile")):
        return "Get the Lead"
    if any(value in angle for value in ("follow", "missed call", "automation")):
        return "Answer It"
    return "Get the Lead"


def _set_commercial_fields(
    row: dict[str, Any],
    *,
    state: str,
    reason: str,
    groups: set[str],
) -> dict[str, Any]:
    updated = dict(row)
    trigger = str(_first(row, "Send Trigger", "send_trigger", "Specific Observation") or reason).strip()
    angle = str(_first(row, "Best Service Angle", "Offer Fit", "best_angle") or "conversion path repair").strip()
    group_text = ", ".join(sorted(groups)) if groups else "structural website"
    eligible = state == "commercially-sendable"
    dimensions = {
        "commercial_materiality": f"Verified {reason} affects the prospect's owned conversion path.",
        "recurring_consequence": f"The {group_text} gap can recur for each prospective customer using that path.",
        "root_cause_confidence": f"Deterministic v2 backfill based on {trigger or reason}; no LLM inference.",
        "offer_component": _offer_component(row),
        "packageable_solution": angle,
        "value_mechanism": "Repair the verified path between visible demand and a completed inquiry.",
        "beyond_free_fix": (
            "The evidence spans a structural absence/failure or at least two independent path groups."
            if eligible
            else "Evidence does not prove work beyond an isolated correction."
        ),
        "self_fix_risk": (
            "Low: the proven case requires a packaged conversion-path repair."
            if eligible
            else "High or unknown: the visible issue may be self-fixable."
        ),
        "intentionality": "No evidence the verified structural/path failure is an intentional conversion design.",
    }
    updated["Score Schema Version"] = SCORE_SCHEMA_VERSION
    updated["score_schema_version"] = SCORE_SCHEMA_VERSION
    updated["Commercial Fit Source"] = "deterministic-backfill-v2"
    updated["commercial_fit_source"] = "deterministic-backfill-v2"
    updated["Commercial Fit State"] = state
    updated["commercial_fit_state"] = state
    for key in COMMERCIAL_DIMENSION_KEYS:
        value = dimensions[key]
        updated[key] = value
        updated[COMMERCIAL_FIELD_ALIASES[key]] = value
    updated["Contact Eligibility"] = "contact-now" if eligible else "hold-commercial-case"
    updated["contact_eligibility"] = updated["Contact Eligibility"]
    updated["Asset State"] = "none"
    updated["asset_state"] = "none"
    updated["Promise State"] = "asset-free"
    updated["promise_state"] = "asset-free"
    updated.setdefault("Asset ID", str(_first(row, "asset_id") or ""))
    updated.setdefault("asset_id", str(_first(row, "Asset ID") or ""))
    updated.setdefault("Asset Verifier Verdict", str(_first(row, "asset_verifier_verdict") or ""))
    updated.setdefault("asset_verifier_verdict", str(_first(row, "Asset Verifier Verdict") or ""))
    return updated


def backfill_legacy_commercial_fit(row: dict[str, Any]) -> dict[str, Any]:
    """Migrate one legacy score monotonically, conservatively, and without an LLM."""
    status = _site_status(row)
    flags = _evidence_flags(row)
    blob = _evidence_blob(row)
    groups = _path_failure_groups(row, flags, blob)
    prior_sendable = _clean(_first(row, "Sendable", "sendable"))
    explicit_non_sendable = bool(prior_sendable) and prior_sendable not in {"yes", "true", "1"}

    structural = status in HARD_STRUCTURAL_STATUSES
    broken_with_proof = status in BROKEN_STATUSES and bool(flags & HARD_FAILURE_FLAGS)
    multi_path = status in {"loaded", "weak", "loads"} and len(groups) >= 2
    business_name = _clean(_first(row, "Business Name", "business_name"))
    weak_offer_fit = (
        status in {"loaded", "weak", "loads"}
        and "no_high_ticket_terms_detected" in flags
        and not any(term in business_name for term in HIGH_TICKET_NAME_TERMS)
    )

    if explicit_non_sendable:
        state = "needs-broader-system-evidence"
        reason = "legacy row was previously non-sendable; monotonic backfill cannot upgrade it"
    elif structural:
        state = "commercially-sendable"
        reason = f"structural site state {status}"
    elif broken_with_proof:
        state = "commercially-sendable"
        reason = "broken site corroborated by DNS/SSL/HTTP/rendered hard-failure evidence"
    elif weak_offer_fit:
        state = "not-an-LPM-fit"
        reason = "loaded/weak site lacks verified high-ticket offer evidence"
    elif any(re.search(pattern, blob) for pattern in ISOLATED_PATTERNS) or groups == {"form"}:
        state = "minor-fix/nurture"
        reason = "isolated or basic-form-only issue"
    elif multi_path:
        state = "commercially-sendable"
        reason = f"{len(groups)} independent path-level failure groups"
    elif status in BROKEN_STATUSES or "static fetch" in blob or "fetch failure" in blob:
        state = "needs-broader-system-evidence"
        reason = "unconfirmed broken/static-fetch evidence"
    elif status in {"loaded", "loads", "weak"}:
        state = "needs-broader-system-evidence"
        reason = "fewer than two independent path-level failure groups"
    else:
        state = "intentional-or-ambiguous"
        reason = "site and commercial mechanism remain ambiguous"

    return _set_commercial_fields(row, state=state, reason=reason, groups=groups)
