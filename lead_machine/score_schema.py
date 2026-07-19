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


def is_truly_legacy_score(data: Dict[str, Any]) -> bool:
    """Legacy means the score predates the entire v2 commercial contract."""
    return "score_schema_version" not in data and not (set(data) & V2_SCORE_FIELDS)
