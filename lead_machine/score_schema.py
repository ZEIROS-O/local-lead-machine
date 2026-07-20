from __future__ import annotations

from typing import Any, Dict


SCORE_SCHEMA_VERSION = 2

COMMERCIAL_DIMENSION_KEYS = (
    "commercial_materiality",
    "recurring_consequence",
    "root_cause_confidence",
    "offer_component",
    "packageable_solution",
    "value_mechanism",
    "beyond_free_fix",
    "self_fix_risk",
    "intentionality",
)

COMMERCIAL_FIELD_TITLES = {
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

COMMERCIAL_FIT_STATES = {
    "commercially-sendable",
    "minor-fix/nurture",
    "needs-broader-system-evidence",
    "intentional-or-ambiguous",
    "not-an-LPM-fit",
    "unverified",
}
CONTACT_ELIGIBILITY_STATES = {"contact-now", "contact-after-asset", "hold-commercial-case"}
ASSET_STATES = {"none", "planned", "verified-existing"}
PROMISE_STATES = {"asset-free", "future-build", "existing-verified-asset"}

V2_SCORE_FIELDS = {
    "score_schema_version",
    *COMMERCIAL_DIMENSION_KEYS,
    "commercial_fit_state",
    "contact_eligibility",
    "asset_state",
    "promise_state",
    "asset_id",
    "asset_verifier_verdict",
}

CSV_SCORE_ALIASES = {
    "score_schema_version": ("Score Schema Version", "score_schema_version"),
    "commercial_fit_state": ("Commercial Fit State", "commercial_fit_state"),
    **{
        key: (title, key)
        for key, title in COMMERCIAL_FIELD_TITLES.items()
    },
    "contact_eligibility": ("Contact Eligibility", "contact_eligibility"),
    "asset_state": ("Asset State", "asset_state"),
    "promise_state": ("Promise State", "promise_state"),
    "asset_id": ("Asset ID", "asset_id"),
    "asset_verifier_verdict": (
        "Asset Verifier Verdict",
        "asset_verifier_verdict",
    ),
}

MANUAL_SCORE_SCHEMA: Dict[str, Any] = {
    "score_schema_version": SCORE_SCHEMA_VERSION,
    "subscores": {
        "icp_fit": 0,
        "reputation_demand_proof": 0,
        "web_conversion_pain": 0,
        "mockup_leverage": 0,
        "reachability": 0,
        "offer_angle_clarity": 0,
        "evidence_confidence": 0,
    },
    "total_score": 0,
    "grade": "A|B|C|Skip",
    "confidence": "low|medium|high",
    "hard_disqualifiers": [],
    "best_angle": "",
    "visible_pain_score": 0,
    "visible_pain_reasons": [],
    "visible_pain_categories": [],
    "send_trigger": "",
    "commercial_fit_state": (
        "commercially-sendable|minor-fix/nurture|needs-broader-system-evidence|"
        "intentional-or-ambiguous|not-an-LPM-fit|unverified"
    ),
    "commercial_materiality": "",
    "recurring_consequence": "",
    "root_cause_confidence": "",
    "offer_component": "Get Found|Get the Lead|Answer It|Close It|Keep|See It All",
    "packageable_solution": "",
    "value_mechanism": "",
    "beyond_free_fix": "",
    "self_fix_risk": "",
    "intentionality": "",
    "contact_eligibility": "contact-now|contact-after-asset|hold-commercial-case",
    "asset_state": "none|planned|verified-existing",
    "promise_state": "asset-free|future-build|existing-verified-asset",
    "asset_id": "",
    "asset_verifier_verdict": "",
    "recommended_action": "",
    "why_this_lead": [],
    "why_this_might_be_wrong": [],
    "opener": "",
    "evidence_refs": [],
    "safe_outreach_claims": [{"claim": "", "evidence_refs": []}],
    "unsafe_claims": [],
    "airtable_fields": {},
}

SHARED_COMMERCIAL_SCORING_RULES = """\
- score_schema_version must be the integer 2.
- A verified issue is not automatically commercially sendable. Classify every lead into exactly one
  commercial_fit_state and fill all nine commercial dimensions when commercially-sendable.
- A broken link, inconsistent phone number, cosmetic issue, basic-form-only issue, or isolated one-step fix is
  minor-fix/nurture unless evidence connects it to a broader recurring conversion or revenue-system failure.
- Use needs-broader-system-evidence when a plausible problem lacks the root-cause, recurrence, or value chain.
- Use intentional-or-ambiguous when the configuration may be intentional or the inference is uncertain.
- The self_fix_risk and beyond_free_fix tests must explain why the case is more than a free one-step correction.
- Model contact_eligibility, asset_state, and promise_state independently. Missing or uncertain contact
  eligibility is hold-commercial-case, never contact-now.
- No asset may still use contact-now with an asset-free opener. Never imply an asset exists unless
  asset_state is verified-existing, asset_id is non-empty, and asset_verifier_verdict is exactly PASS.
- With asset_state none/planned, use honest asset-free or future-build language such as "I can show you what
  I mean" or "I can build the example."""


def _semantic(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _alias_value(row: Dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = row.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def validate_current_scored_row(row: Dict[str, Any]) -> list[str]:
    """Return objective schema-v2 contract errors for one scored CSV row."""
    errors: list[str] = []
    version = _alias_value(row, CSV_SCORE_ALIASES["score_schema_version"])
    if version != str(SCORE_SCHEMA_VERSION):
        errors.append(f"score_schema_version must be {SCORE_SCHEMA_VERSION}")

    for key, aliases in CSV_SCORE_ALIASES.items():
        if not any(alias in row for alias in aliases):
            errors.append(f"missing field {key}")
            continue
        values = {
            _semantic(value)
            for alias in aliases
            if (value := row.get(alias)) not in (None, "")
        }
        if len(values) > 1:
            errors.append(f"conflicting aliases {key}")

    raw_state = _alias_value(
        row, CSV_SCORE_ALIASES["commercial_fit_state"]
    )
    state = next(
        (
            candidate
            for candidate in COMMERCIAL_FIT_STATES
            if candidate.casefold() == raw_state.casefold()
        ),
        raw_state.casefold(),
    )
    contact = _alias_value(
        row, CSV_SCORE_ALIASES["contact_eligibility"]
    ).casefold()
    asset = _alias_value(row, CSV_SCORE_ALIASES["asset_state"]).casefold()
    promise = _alias_value(row, CSV_SCORE_ALIASES["promise_state"]).casefold()
    if state not in COMMERCIAL_FIT_STATES:
        errors.append("invalid commercial_fit_state")
    if contact not in CONTACT_ELIGIBILITY_STATES:
        errors.append("invalid contact_eligibility")
    if asset not in ASSET_STATES:
        errors.append("invalid asset_state")
    if promise not in PROMISE_STATES:
        errors.append("invalid promise_state")
    if state == "commercially-sendable":
        errors.extend(
            f"missing value {key}"
            for key in COMMERCIAL_DIMENSION_KEYS
            if not _alias_value(row, CSV_SCORE_ALIASES[key])
        )
    if contact == "contact-now" and state != "commercially-sendable":
        errors.append("contact-now requires commercially-sendable")
    if contact == "contact-after-asset" and asset != "planned":
        errors.append("contact-after-asset requires planned asset_state")
    if promise == "future-build" and asset != "planned":
        errors.append("future-build requires planned asset_state")
    asset_id = _alias_value(row, CSV_SCORE_ALIASES["asset_id"])
    verdict = _alias_value(
        row, CSV_SCORE_ALIASES["asset_verifier_verdict"]
    )
    if asset == "verified-existing" and (not asset_id or verdict != "PASS"):
        errors.append("verified-existing requires asset_id and PASS verifier")
    if promise == "existing-verified-asset" and asset != "verified-existing":
        errors.append("existing-verified-asset requires verified-existing asset_state")
    return errors
