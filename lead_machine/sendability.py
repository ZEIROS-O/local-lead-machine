from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Set

from .buyer_signals import (
    buyer_intent_summary,
    demand_signal_state,
    has_money_signal as buyer_has_money_signal,
    money_signal_state,
    review_demand_band,
)
from . import queue_state
from .score_schema import (
    ASSET_STATES,
    COMMERCIAL_FIT_STATES,
    CONTACT_ELIGIBILITY_STATES,
    PROMISE_STATES,
)


MIN_SENDABLE_SCORE = 65
MIN_SENDABLE_CONFIDENCE = 0.75

CONTACT_READINESS_FIELDS = (
    "Sendable",
    "Packet Eligible",
    "Commercial Fit State",
    "Contact Eligibility",
)

SCORE_BLOCKER = "Score below B threshold"
GRADE_BLOCKER = "Lead grade below sendable threshold"
HARD_DISQUALIFIER_BLOCKER = "Hard disqualifier present"
CONVERSION_ISSUE_BLOCKER = "Missing concrete conversion issue"
SEND_TRIGGER_BLOCKER = "Missing concrete send trigger"
DEMAND_BLOCKER = "Missing proof-of-demand signal"
RATING_BLOCKER = "Rating below 4.0 minimum"
REVIEW_COUNT_BLOCKER = "Review count below sendable minimum"
VISIBLE_PAIN_BLOCKER = "Visible conversion pain too weak for business maturity"
STRONG_FORM_NO_PAIN_BLOCKER = "Strong estimate form exists / no concrete visible conversion issue"
ADVANCED_OPTIMIZATION_BLOCKER = "Advanced Optimization without concrete visible pain"
FORM_ABSENCE_CLAIM_BLOCKER = "Strong estimate form conflicts with missing-form claim"
ESTIMATE_FUNNEL_STRONG_FORM_BLOCKER = "Estimate Funnel blocked by strong estimate form without estimate-path weakness"
ANGLE_BLOCKER = "Missing clear outreach angle"
OPENER_BLOCKER = "Missing evidence-backed opener"
EVIDENCE_COUNT_BLOCKER = "Not enough evidence references"
CONFIDENCE_BLOCKER = "Low confidence"
NO_SAFE_CLAIM_BLOCKER = "No safe outreach claim"
SAFE_CLAIM_REF_BLOCKER = "Safe outreach claim missing evidence reference"
INVALID_REF_BLOCKER = "Invalid evidence reference"
COMMERCIAL_FIT_BLOCKER = "Commercial fit gate failed"
CONTACT_ELIGIBILITY_BLOCKER = "Contact eligibility is not contact-now"
HARD_ICP_SKIP_BLOCKER = "Hard ICP gate skipped this lead"
# The two non-scoring paths that used to mint their own verdict, expressed as
# blockers so they go through `admit` like everything else.
SCORER_WITHHELD_BLOCKER = "Scorer did not mark this lead sendable"
MANUAL_CARD_UNVERIFIED_BLOCKER = "Manual lead card not marked sendable by the operator"
PACKET_TIER_NOT_SENDABLE_BLOCKER = "Packet Tier is not the sendable tier"

EVIDENCE_BLOCKERS = {
    HARD_DISQUALIFIER_BLOCKER,
    HARD_ICP_SKIP_BLOCKER,
    CONVERSION_ISSUE_BLOCKER,
    SEND_TRIGGER_BLOCKER,
    VISIBLE_PAIN_BLOCKER,
    STRONG_FORM_NO_PAIN_BLOCKER,
    ADVANCED_OPTIMIZATION_BLOCKER,
    FORM_ABSENCE_CLAIM_BLOCKER,
    ESTIMATE_FUNNEL_STRONG_FORM_BLOCKER,
    DEMAND_BLOCKER,
    OPENER_BLOCKER,
    EVIDENCE_COUNT_BLOCKER,
    NO_SAFE_CLAIM_BLOCKER,
    SAFE_CLAIM_REF_BLOCKER,
    INVALID_REF_BLOCKER,
    COMMERCIAL_FIT_BLOCKER,
}

COMMERCIAL_DIMENSION_FIELDS = {
    "commercial_materiality": ("Commercial Materiality", "commercial_materiality"),
    "recurring_consequence": ("Recurring Consequence", "recurring_consequence"),
    "root_cause_confidence": ("Root Cause Confidence", "root_cause_confidence"),
    "offer_component": ("LPM Offer Component", "offer_component"),
    "packageable_solution": ("Packageable Solution", "packageable_solution"),
    "value_mechanism": ("Value Mechanism", "value_mechanism"),
    "beyond_free_fix": ("Beyond Free Fix", "beyond_free_fix"),
    "self_fix_risk": ("Self Fix Risk", "self_fix_risk"),
    "intentionality": ("Intentionality", "intentionality"),
}
CONFIDENCE_VALUES = {
    "high": 1.0,
    "medium": 0.75,
    "low": 0.5,
}

# --- Packet tiers ---------------------------------------------------------
# Packet creation is split from sendability. A lead can be a valid manual
# prospect (Manual Review Packet) even when it is not safe to cold-send yet.
PACKET_TIER_SENDABLE = "Sendable Packet"
PACKET_TIER_MANUAL = "Manual Review Packet"
PACKET_TIER_WATCHLIST = "Watchlist"
PACKET_TIER_NONE = "No Packet"
PACKET_TIER_CHOICES = frozenset(
    {
        PACKET_TIER_SENDABLE,
        PACKET_TIER_MANUAL,
        PACKET_TIER_WATCHLIST,
        PACKET_TIER_NONE,
    }
)

PACKET_TIERS_WITH_PACKET = {PACKET_TIER_SENDABLE, PACKET_TIER_MANUAL}

# --- Verdict --------------------------------------------------------------
# The one machine-owned verdict, declared beside the tiers because it is a
# rename of them rather than a second decision. `lead_machine.verdict` imports
# this and owns the evaluation API; the vocabulary lives here so there is one
# declaration, and so `readiness_flags` below can read a base row that now
# carries `Verdict` where it used to carry `Packet Tier` without importing the
# module that imports this one.
VERDICT_SEND = "send"
VERDICT_REVIEW = "review"
VERDICT_WATCH = "watch"
VERDICT_REJECT = "reject"

TIER_VERDICTS = {
    PACKET_TIER_SENDABLE: VERDICT_SEND,
    PACKET_TIER_MANUAL: VERDICT_REVIEW,
    PACKET_TIER_WATCHLIST: VERDICT_WATCH,
    PACKET_TIER_NONE: VERDICT_REJECT,
}
VERDICT_CHOICES = frozenset(TIER_VERDICTS.values())
# The inverse. It exists only because the mapping is a bijection, which
# tests/test_verdict.py pins — a lossy mapping would make this silently wrong.
VERDICT_PACKET_TIERS = {verdict: tier for tier, verdict in TIER_VERDICTS.items()}

MANUAL_REVIEW_MIN_RATING = 4.5
MANUAL_REVIEW_MIN_REVIEWS = 10
MANUAL_REVIEW_MIN_PAIN_SCORE = 35
MANUAL_REVIEW_BANDS = {"early_proof", "ideal_growth"}
MANUAL_REVIEW_OFFERS = {"no-site launch", "website rebuild", "estimate funnel"}
MANUAL_REVIEW_SITE_STATUSES = {
    "missing",
    "no website",
    "no_website",
    "weak",
    "broken",
    "broken website",
    "broken_website",
    "social only",
    "social_only",
}
MANUAL_REVIEW_QUALITY = {"poor", "missing", "n/a"}
MANUAL_REVIEW_PAIN_PATTERNS = [
    "social-only",
    "social only",
    "facebook-only",
    "facebook only",
    "facebook profile",
    "no standalone website",
    "standalone website",
    "no clear estimate cta",
    "no estimate cta",
    "no estimate form",
    "no estimate funnel",
    "estimate cta or form",
    "phone hard to find",
    "phone number hard to find",
    "phone is hard to find",
    "phone number is hard to find",
]

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

HARD_CONCRETE_SITE_STATUSES = CONCRETE_SITE_STATUSES - {"weak"}

CONCRETE_ISSUE_FLAGS = {
    "dns_domain_failure",
    "homepage_http_hard_failure",
    "ssl_cert_hard_failure",
    "broken_redirect",
    "rendered_load_failed",
    "parked_dead_domain",
    "non_standard_site_or_booking_url",
    "non_standard_site_or_booking",
    "premium_perception_gap",
    "premium_brand_mismatch",
    "owned_site_undersells_google_proof",
    "cheap_website",
    "site_looks_cheap",
    "outdated_site",
    "lawn_maintenance_positioning",
    "weak_project_gallery",
    "weak_gallery",
    "buried_project_photos",
    "project_photos_buried",
    "portfolio_presentation_pain",
    "no_form_detected",
    "no_clear_cta_detected",
    "missing_contact_path",
    "missing_estimate_path",
    "weak_mobile_path",
    "buried_phone",
    "phone_not_visible",
    "generic_copy_signals",
    "weak_services_page",
    "weak_basic_form",
    "only_phone_email_path",
    "broken_form",
    "form_error",
    "bad_mobile_status",
    "buried_cta",
    "low_visible_trust",
    "reviews_not_near_cta",
    "unclear_service_pages",
    "high_ticket_services_unclear",
    "service_clarity_pain",
    "mobile_usability_pain",
    "trust_proof_pain",
    "followup_or_missed_call_pain",
    "missed_call_path",
    "followup_gap",
}

FORM_ABSENCE_FLAGS = {
    "no_form_detected",
    "missing_contact_path",
    "missing_estimate_path",
}

ESTIMATE_PATH_WEAKNESS_FLAGS = {
    "no_clear_cta_detected",
    "weak_mobile_path",
    "bad_mobile_status",
    "buried_cta",
    "only_phone_email_path",
    "broken_form",
    "form_error",
    "low_visible_trust",
    "reviews_not_near_cta",
    "weak_basic_form",
    "mobile_usability_pain",
    "trust_proof_pain",
}

NON_FORM_VISIBLE_PAIN_CATEGORIES = {
    "premium_perception_gap",
    "portfolio_presentation_pain",
    "trust_proof_pain",
    "mobile_usability_pain",
    "service_clarity_pain",
    "no_site_or_social_only_pain",
    "broken_site_pain",
    "followup_or_missed_call_pain",
}

CONCRETE_VISIBLE_PAIN_CATEGORIES = NON_FORM_VISIBLE_PAIN_CATEGORIES | {"estimate_path_pain"}

FORM_ABSENCE_PATTERNS = [
    r"\bno estimate form\b",
    r"\bmissing estimate form\b",
    r"\bno contact form\b",
    r"\bmissing contact form\b",
    r"\bno contact path\b",
    r"\bmissing contact path\b",
    r"\bno estimate path\b",
    r"\bmissing estimate path\b",
    r"\bno estimate funnel\b",
    r"\bno way to request (a )?(quote|estimate)\b",
    r"\bneed(s)? an estimate path\b",
]

ESTIMATE_PATH_WEAKNESS_PATTERNS = [
    r"\bweak estimate form\b",
    r"\bbasic estimate form\b",
    r"\bestimate form appears broken\b",
    r"\bbroken form\b",
    r"\bform error\b",
    r"\bno clear estimate cta\b",
    r"\bestimate cta appears buried\b",
    r"\bburied cta\b",
    r"\bonly phone/email estimate path\b",
    r"\bweak mobile (quote|estimate) path\b",
    r"\bbad mobile\b",
    r"\bmobile quote path\b",
    r"\btrust proof near cta\b",
    r"\breviews? (are )?not used near\b",
]

GENERIC_OPTIMIZATION_REASONS = {
    "generic/template website copy",
    "minor optimization angle only",
}

CONCRETE_ISSUE_FIELDS = {
    "site_status",
    "site_issue_flags",
    "rendered_site_status",
    "contact_form",
    "contact_form_detected",
    "estimate_form_detected",
    "form_strength",
    "cta_terms",
    "cta_strength",
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
        parts.append(f"{_escape_claim_text(str(claim.get('claim', '')))} [{refs}]")
    return "; ".join(parts)


def normalize_unsafe_claims(value: Any) -> List[str]:
    """Unsafe claims keep the raw `;` split, deliberately — with a caveat.

    They carry no citations, so the misattribution that forced escaping on the
    safe side cannot happen here: a split yields two warnings rather than one,
    and never moves evidence between assertions.

    But the reason it is harmless is narrower than it looks, and it is *not*
    "both fragments still block". No blocker reads unsafe claims at all — they
    are display-only. The safety rests on them sitting outside the gate
    entirely. Wire them into `evaluate_sendability` and this unescaped split
    becomes live; escape it then, the way `_escape_claim_text` does.

    Known, accepted loss: a claim of only `;` or whitespace round-trips to
    nothing, dropping a "do not say this" warning from the rep's card.
    """
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


def hard_icp_skip_blockers(row: Dict[str, Any]) -> List[str]:
    """The enricher's ICP verdict, read off wherever a scored row still carries it.

    `hard_icp_skip` is not a column. `enrich_leads` writes it to
    `derived.hard_icp_skip` on the enriched record, and both scorers dump that
    whole record into `Raw Evidence` before calling `apply_sendability` — so the
    verdict is already on every scored row and needs no new field, only a reader.

    Fails closed on an unparseable verdict. `make_llm_batch` raises on one because
    a malformed batch is worth stopping; here a raise would take down a whole
    scoring run over one hand-edited row, and refusing to send is the same answer
    at lower cost. Absent or explicitly false is the only clearance.
    """
    derived = row.get("derived")
    if not isinstance(derived, dict):
        derived = _raw_evidence(row).get("derived")
    if not isinstance(derived, dict):
        return []

    skip = derived.get("hard_icp_skip")
    if skip is None or skip is False or skip == "":
        return []

    reasons = derived.get("hard_icp_reasons")
    if not isinstance(reasons, (list, tuple)):
        reasons = []
    named = ", ".join(str(reason).strip() for reason in reasons if str(reason).strip())
    return [f"{HARD_ICP_SKIP_BLOCKER}: {named}" if named else HARD_ICP_SKIP_BLOCKER]


@dataclass(frozen=True)
class Admission:
    """A lead's permission to be contacted, and the only thing that can grant it.

    Sendability used to be a bare bool that each caller turned into its own
    `Sendable` string, so the enricher's `hard_icp_skip` verdict was only as
    binding as whichever path happened to remember it — and `apply_sendability`
    never did. 31 rows across 14 businesses shipped `Sendable=Yes` on top of a
    hard ICP skip. Routing every verdict through one object is what makes that
    combination unreachable rather than merely absent.
    """

    blockers: tuple[str, ...]

    @property
    def admitted(self) -> bool:
        return not self.blockers

    @property
    def sendable_flag(self) -> str:
        return "Yes" if self.admitted else "No"


def admit(row: Dict[str, Any], blockers: Iterable[str] = ()) -> Admission:
    """The one place a lead may be declared sendable.

    The ICP verdict is re-read from *row* rather than taken from *blockers* on
    purpose: a caller that computed its own clean blocker list still cannot get
    an admission for a skipped lead, because the check belongs to the choke point
    and not to the caller. Any new sendable path has to come through here.
    """
    return Admission(tuple(blockers) + tuple(hard_icp_skip_blockers(row)))


def evaluate_sendability(row: Dict[str, Any]) -> Dict[str, Any]:
    blockers: List[str] = []
    total_score = _parse_score(row)
    confidence = normalize_confidence(_pick(row, "confidence", "AI Confidence"))
    evidence_refs = normalize_evidence_refs(row)
    safe_claims = normalize_safe_outreach_claims(row)
    known_refs = known_evidence_ids(row)
    commercial_fit = evaluate_commercial_fit(row)

    if total_score < MIN_SENDABLE_SCORE:
        blockers.append(SCORE_BLOCKER)

    grade = _pick(row, "Lead Priority", "grade", "Grade").strip().upper()
    if grade not in {"A", "B"}:
        blockers.append(GRADE_BLOCKER)

    if has_hard_disqualifier(row):
        blockers.append(HARD_DISQUALIFIER_BLOCKER)

    if commercial_fit["commercial_fit_state"] != "commercially-sendable":
        blockers.append(f"{COMMERCIAL_FIT_BLOCKER}: {commercial_fit['commercial_fit_state']}")
    if commercial_fit["contact_eligibility"] != "contact-now":
        blockers.append(CONTACT_ELIGIBILITY_BLOCKER)
    if commercial_fit["state_errors"]:
        blockers.extend(f"{COMMERCIAL_FIT_BLOCKER}: {error}" for error in commercial_fit["state_errors"])

    concrete_issue = has_concrete_conversion_issue(row)
    strong_estimate_form = has_strong_estimate_form(row)
    send_trigger = get_send_trigger(row)
    visible_pain_score = get_visible_pain_score(row)
    visible_pain_reasons = get_visible_pain_reasons(row)
    rating = _rating(row)
    reviews = _review_count(row)

    if not concrete_issue:
        blockers.append(CONVERSION_ISSUE_BLOCKER)

    if not send_trigger:
        blockers.append(SEND_TRIGGER_BLOCKER)

    if strong_estimate_form and has_form_absence_claim(row):
        blockers.append(FORM_ABSENCE_CLAIM_BLOCKER)

    if (
        strong_estimate_form
        and _best_angle(row).lower() == "estimate funnel"
        and not has_estimate_path_weakness_beyond_form_existence(row)
    ):
        blockers.append(ESTIMATE_FUNNEL_STRONG_FORM_BLOCKER)

    if strong_estimate_form and not concrete_issue:
        blockers.append(STRONG_FORM_NO_PAIN_BLOCKER)

    if _best_angle(row).lower() == "advanced optimization" and (
        not concrete_issue or is_generic_optimization_only(row)
    ):
        blockers.append(ADVANCED_OPTIMIZATION_BLOCKER)

    if rating and rating < 4.0:
        blockers.append(RATING_BLOCKER)

    if reviews < 5 and not is_exceptional_low_review_fit(row):
        blockers.append(REVIEW_COUNT_BLOCKER)

    # Only a confirmed "yes" relaxes the mature-band pain threshold below. An
    # "unknown" must behave exactly as False did, or the gate loosens on the
    # leads we have the least evidence about.
    money_signal = has_money_signal(row)
    review_band = _review_demand_band(row)
    required_pain = required_visible_pain_score(reviews, review_band=review_band, money_signal=money_signal)
    if not is_exceptional_low_review_fit(row) and visible_pain_score < required_pain:
        blockers.append(VISIBLE_PAIN_BLOCKER)

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

    admission = admit(row, blockers)

    minimum_evidence_passed = not any(
        blocker in EVIDENCE_BLOCKERS
        or blocker.startswith(f"{COMMERCIAL_FIT_BLOCKER}:")
        or blocker.startswith(f"{HARD_ICP_SKIP_BLOCKER}:")
        for blocker in admission.blockers
    )

    return {
        "sendable": admission.admitted,
        "sendable_flag": admission.sendable_flag,
        "send_blockers": list(admission.blockers),
        "minimum_evidence_passed": minimum_evidence_passed,
        "safe_outreach_claims": safe_claims,
        "unsafe_claims": normalize_unsafe_claims(row),
        "evidence_refs": evidence_refs,
        "confidence_score": confidence,
        "send_trigger": send_trigger,
        "visible_pain_score": visible_pain_score,
        "visible_pain_reasons": visible_pain_reasons,
        "money_signal": money_signal,
        "money_signal_state": money_signal_state(row),
        "demand_signal_state": demand_signal_state(row),
        "buyer_intent_signals": buyer_intent_summary(row),
        **commercial_fit,
    }


def readiness_flags(row: Dict[str, Any]) -> tuple[str, str]:
    """Return (Sendable, Packet Eligible) for *row*, deriving them if absent.

    Airtable stores Packet Tier instead of the two booleans — it separates
    Watchlist from No Packet, which the booleans could not. Rows read back from
    the base therefore carry only the tier, while internal pipeline rows still
    carry all three.

    Explicit values win in the TIGHTENING direction only. A hand-edited "No" is
    honoured, because a human withdrawing a lead never needs the gate's
    permission. A stored "Yes" does not win: it is a snapshot taken before the
    ICP verdict was known, and honouring it is what let a hard-skipped lead ride
    a stale cell all the way into the send queue.
    """
    tier = _pick(row, "Packet Tier", "packet_tier").strip()
    sendable = _pick(row, "Sendable", "sendable").strip()
    packet_eligible = _pick(row, "Packet Eligible", "packet_eligible").strip()
    verdict = _pick(row, "Verdict", "verdict").strip()
    if not tier and verdict:
        # A base row now carries `Verdict` where it used to carry `Packet Tier`.
        # Reconstruct the tier so the rules below stay written against one
        # concept: `Verdict` is a rename of the tier, so this loses nothing.
        tier = VERDICT_PACKET_TIERS.get(verdict, "")
    if not sendable and tier:
        # Reconstructing the flag from the tier is still a way to declare a lead
        # sendable, so it goes through `admit` like every other one.
        sendable = admit(
            row, () if tier == PACKET_TIER_SENDABLE else (PACKET_TIER_NOT_SENDABLE_BLOCKER,)
        ).sendable_flag
    elif sendable.casefold() == "yes":
        # Re-run the admission over a stored Yes so the ICP verdict can withdraw
        # it. Never upgrades a No.
        sendable = admit(row, ()).sendable_flag
    if not packet_eligible and tier:
        packet_eligible = "Yes" if tier in PACKET_TIERS_WITH_PACKET else "No"
    return sendable, packet_eligible


def contact_readiness_failures(row: Dict[str, Any]) -> List[str]:
    """Return authoritative contact-readiness contract failures.

    Packet eligibility by itself is intentionally insufficient: manual-review
    packets may be uploadable while remaining unsafe to contact.

    The commercially-sendable and contact-now clauses are gone from
    READINESS_CLAUSES because `Verdict` folds them in — `send` is unreachable
    unless both hold. What does not go away is re-deriving the stored value:
    `Verdict` in the base is still a snapshot, and a snapshot is exactly what let
    a hard-skipped lead ride a stale cell into the send queue. `readiness_flags`
    re-runs the admission over it, in the tightening direction only.
    """
    failures: List[str] = []
    sendable, packet_eligible = readiness_flags(row)
    if sendable.casefold() != "yes":
        failures.append(queue_state.READINESS_CLAUSES[0].failure)
    if packet_eligible.casefold() != "yes":
        failures.append("Packet Eligible must be Yes")
    # A row carrying a Verdict is a base row read back after the cutover, where
    # these two columns no longer exist. Anything else — a scored CSV, a legacy
    # row — is expected to carry them, and their absence is the scorer never
    # having established commercial fit. Blank must keep meaning "failed" there,
    # or a legacy row asserting Sendable=Yes with no evidence at all uploads as
    # a sendable packet.
    carries_verdict = bool(_pick(row, "Verdict", "verdict").strip())
    for clause in queue_state.READINESS_CROSS_CHECKS:
        snake_case = clause.field.casefold().replace(" ", "_")
        declared = _pick(row, clause.field, snake_case).strip()
        if not declared and carries_verdict:
            continue
        if declared.casefold() != clause.expected.casefold():
            failures.append(clause.failure)
    return failures


def is_contact_ready(row: Dict[str, Any]) -> bool:
    return not contact_readiness_failures(row)


def evaluate_commercial_fit(row: Dict[str, Any]) -> Dict[str, Any]:
    raw_state = _pick(row, "Commercial Fit State", "commercial_fit_state").strip()
    state = next(
        (candidate for candidate in COMMERCIAL_FIT_STATES if candidate.casefold() == raw_state.casefold()),
        "unverified",
    )
    dimensions = {
        name: _pick(row, *fields).strip()
        for name, fields in COMMERCIAL_DIMENSION_FIELDS.items()
    }
    state_errors: List[str] = []
    if state == "unverified" and raw_state.casefold() != "unverified":
        state_errors.append(f"invalid commercial_fit_state {raw_state or '<missing>'}")
    if state == "commercially-sendable":
        missing = [name for name, value in dimensions.items() if not value]
        if missing:
            state = "unverified"
            state_errors.append("missing dimensions " + ",".join(missing))

    raw_contact = _pick(row, "Contact Eligibility", "contact_eligibility").strip().lower()
    raw_asset = _pick(row, "Asset State", "asset_state").strip().lower()
    raw_promise = _pick(row, "Promise State", "promise_state").strip().lower()
    contact = raw_contact if raw_contact in CONTACT_ELIGIBILITY_STATES else "hold-commercial-case"
    asset = raw_asset if raw_asset in ASSET_STATES else "none"
    promise = raw_promise if raw_promise in PROMISE_STATES else "asset-free"
    if raw_contact not in CONTACT_ELIGIBILITY_STATES:
        state_errors.append(f"invalid contact_eligibility {raw_contact or '<missing>'}; held")
    if raw_asset not in ASSET_STATES:
        state_errors.append(f"invalid asset_state {raw_asset or '<missing>'}")
    if raw_promise not in PROMISE_STATES:
        state_errors.append(f"invalid promise_state {raw_promise or '<missing>'}")
    if contact == "contact-now" and state != "commercially-sendable":
        state_errors.append("contact-now requires commercially-sendable")
    asset_id = _pick(row, "Asset ID", "asset_id").strip()
    asset_verifier_verdict = _pick(row, "Asset Verifier Verdict", "asset_verifier_verdict").strip()
    if asset == "verified-existing" and (not asset_id or asset_verifier_verdict != "PASS"):
        state_errors.append("verified-existing asset lacks a PASS artifact receipt")
    if promise == "existing-verified-asset":
        if asset != "verified-existing":
            state_errors.append("existing asset promise conflicts with asset_state")
        if not asset_id or asset_verifier_verdict != "PASS":
            state_errors.append("existing asset promise lacks a PASS artifact receipt")
    if contact == "contact-after-asset" and asset == "verified-existing":
        state_errors.append("contact-after-asset conflicts with verified-existing asset")
    return {
        "commercial_fit_state": state,
        "commercial_dimensions": dimensions,
        "contact_eligibility": contact,
        "asset_state": asset,
        "promise_state": promise,
        "asset_id": asset_id,
        "asset_verifier_verdict": asset_verifier_verdict,
        "state_errors": state_errors,
    }


def has_hard_disqualifier(row: Dict[str, Any]) -> bool:
    for key in ("hard_disqualifiers", "Hard Disqualifiers", "Skip Reason"):
        value = _loads_json_if_possible(row.get(key))
        if value in ("", None, [], {}):
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if any(str(item).strip() for item in value):
                return True
            continue
        if str(value).strip():
            return True
    return False


def manual_review_issue_indicators(row: Dict[str, Any]) -> List[str]:
    """Concrete-enough issues that justify a human taking a manual look.

    These are softer than the strict sendability gate: any one of them is enough
    to make a valid business worth a manual review packet.
    """
    indicators: List[str] = []

    status = _normalized_site_status(row)
    if status in MANUAL_REVIEW_SITE_STATUSES:
        indicators.append(f"site status {status}")

    quality = _clean(_pick(row, "Current Website Quality")).lower()
    if quality in MANUAL_REVIEW_QUALITY:
        indicators.append(f"website quality {quality or 'missing'}")

    pain_score = get_visible_pain_score(row)
    if pain_score >= MANUAL_REVIEW_MIN_PAIN_SCORE:
        indicators.append(f"visible pain score {pain_score}")

    for reason in get_visible_pain_reasons(row):
        cleaned = _clean(reason).lower()
        if any(pattern in cleaned for pattern in MANUAL_REVIEW_PAIN_PATTERNS):
            indicators.append(reason)

    return indicators


def manual_review_reason(row: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Return a non-empty reason string if the lead qualifies for a manual packet."""
    if not result.get("minimum_evidence_passed"):
        return ""
    if _pick(row, "Lead Priority", "grade") not in {"B", "C"}:
        return ""
    if _rating(row) < MANUAL_REVIEW_MIN_RATING:
        return ""
    if _review_count(row) < MANUAL_REVIEW_MIN_REVIEWS:
        return ""
    if _review_demand_band(row) not in MANUAL_REVIEW_BANDS:
        return ""
    if _best_angle(row).lower() not in MANUAL_REVIEW_OFFERS:
        return ""

    # Do not rescue strong-form / no-concrete-issue leads unless a real issue exists.
    if has_strong_estimate_form(row) and not has_concrete_conversion_issue(row):
        return ""

    indicators = manual_review_issue_indicators(row)
    if not indicators:
        return ""
    return "; ".join(indicators)


def qualifies_for_watchlist(row: Dict[str, Any], result: Dict[str, Any]) -> bool:
    """A valid business worth keeping an eye on, but with no concrete issue yet."""
    if not result.get("minimum_evidence_passed"):
        return False
    if _rating(row) < MANUAL_REVIEW_MIN_RATING:
        return False
    if _review_count(row) < MANUAL_REVIEW_MIN_REVIEWS:
        return False
    if _review_demand_band(row) not in MANUAL_REVIEW_BANDS:
        return False
    if _best_angle(row).lower() not in MANUAL_REVIEW_OFFERS:
        return False
    return True


def evaluate_packet_tier(row: Dict[str, Any], result: Dict[str, Any]) -> tuple[str, str]:
    """Classify a scored lead into a packet tier, independent of sendability.

    Returns (tier, manual_review_reason).
    """
    if result.get("sendable"):
        return PACKET_TIER_SENDABLE, ""

    reason = manual_review_reason(row, result)
    if reason:
        return PACKET_TIER_MANUAL, reason

    if qualifies_for_watchlist(row, result):
        return PACKET_TIER_WATCHLIST, ""

    return PACKET_TIER_NONE, ""


def apply_sendability(row: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(row)
    result = evaluate_sendability(updated)
    blockers = "; ".join(result["send_blockers"])

    # The literal comes from the Admission, not from here: `admit` is the only
    # thing allowed to spell the verdict, so a new sendable path cannot mint one.
    updated["Sendable"] = result["sendable_flag"]
    updated["Minimum Evidence Passed"] = "Yes" if result["minimum_evidence_passed"] else "No"
    updated["Send Blockers"] = blockers
    updated["Sendability Blockers"] = blockers
    updated["Evidence Refs"] = json.dumps(result["evidence_refs"], ensure_ascii=False)
    updated["Safe Outreach Claims"] = flatten_safe_outreach_claims(result["safe_outreach_claims"])
    updated["Unsafe Claims"] = flatten_unsafe_claims(result["unsafe_claims"])
    updated["Send Trigger"] = result["send_trigger"]
    updated["send_trigger"] = result["send_trigger"]
    updated["Visible Pain Score"] = result["visible_pain_score"]
    updated["visible_pain_score"] = result["visible_pain_score"]
    updated["Visible Pain Reasons"] = "; ".join(result["visible_pain_reasons"])
    updated["visible_pain_reasons"] = result["visible_pain_reasons"]
    updated["Money Signal"] = result["money_signal_state"].capitalize()
    updated["money_signal"] = updated["Money Signal"]
    # Demand is reported separately from spend on purpose: only Money Signal
    # feeds required_visible_pain_score. Both are CSV/packet columns, not
    # Airtable fields.
    updated["Demand Signal"] = result["demand_signal_state"].capitalize()
    updated["demand_signal"] = updated["Demand Signal"]
    updated["Buyer Intent Signals"] = result["buyer_intent_signals"]
    updated["buyer_intent_signals"] = result["buyer_intent_signals"]
    updated["Commercial Fit State"] = result["commercial_fit_state"]
    updated["commercial_fit_state"] = result["commercial_fit_state"]
    for name, value in result["commercial_dimensions"].items():
        updated[name] = value
    updated["Contact Eligibility"] = result["contact_eligibility"]
    updated["contact_eligibility"] = result["contact_eligibility"]
    updated["Asset State"] = result["asset_state"]
    updated["asset_state"] = result["asset_state"]
    updated["Promise State"] = result["promise_state"]
    updated["promise_state"] = result["promise_state"]
    updated["Asset ID"] = result["asset_id"]
    updated["asset_id"] = result["asset_id"]
    updated["Asset Verifier Verdict"] = result["asset_verifier_verdict"]
    updated["asset_verifier_verdict"] = result["asset_verifier_verdict"]

    tier, review_reason = evaluate_packet_tier(updated, result)
    updated["Packet Tier"] = tier
    updated["Manual Review Reason"] = review_reason
    # Written from the tier that was just computed, in the one function that
    # computes it. `lead_machine.verdict.evaluate_verdict` is the same
    # projection for callers holding only a row; both read TIER_VERDICTS, so a
    # scored CSV cannot carry a verdict that disagrees with its own tier.
    updated["Verdict"] = TIER_VERDICTS[tier]
    updated["Verdict Reasons"] = blockers

    if tier == PACKET_TIER_SENDABLE:
        updated["Packet Eligible"] = "Yes"
    elif tier == PACKET_TIER_MANUAL:
        updated["Packet Eligible"] = "Yes"
        updated["Packet Eligibility Reason"] = (
            f"Manual Review Packet (not sendable yet): {review_reason}"
        )
    else:
        if updated.get("Packet Eligible") == "Yes":
            updated["Packet Eligibility Reason"] = f"Blocked by sendability: {blockers}"
        updated["Packet Eligible"] = ""
        if tier == PACKET_TIER_WATCHLIST and not updated.get("Packet Eligibility Reason"):
            updated["Packet Eligibility Reason"] = (
                "Watchlist: valid business, needs visual review before outreach"
            )

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
    strong_estimate_form = has_strong_estimate_form(row)

    if status in HARD_CONCRETE_SITE_STATUSES or quality in {"n/a"}:
        return True
    if has_specific_visible_pain(row):
        return True
    if not strong_estimate_form and (status in CONCRETE_SITE_STATUSES or quality == "poor"):
        return True

    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)

    website_status = _clean(website.get("site_status")).lower()
    profile_status = _clean(evidence.get("site_status")).lower()
    if website_status in HARD_CONCRETE_SITE_STATUSES or profile_status in HARD_CONCRETE_SITE_STATUSES:
        return True
    if not strong_estimate_form and (
        website_status in CONCRETE_SITE_STATUSES or profile_status in CONCRETE_SITE_STATUSES
    ):
        return True

    flags = set(str(flag).lower() for flag in (website.get("site_issue_flags") or evidence.get("raw_flags") or []))
    if strong_estimate_form:
        flags = flags - FORM_ABSENCE_FLAGS
    if flags & CONCRETE_ISSUE_FLAGS:
        if strong_estimate_form and not (
            flags & (ESTIMATE_PATH_WEAKNESS_FLAGS | concrete_non_form_flags())
        ):
            pass
        else:
            return True

    if not strong_estimate_form and evidence.get("has_contact_form") is False:
        return True
    if not strong_estimate_form and _clean(evidence.get("contact_form_detected")).lower() == "no":
        return True
    if not strong_estimate_form and _clean(evidence.get("estimate_form_detected")).lower() == "no":
        return True
    if _clean(evidence.get("form_strength")).lower() in {"weak", "basic"}:
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
            if strong_estimate_form and value_text == "weak":
                continue
            return True
        if field == "rendered_site_status" and value_text == "broken":
            return True
        if field == "site_issue_flags" and any(flag in value_text for flag in CONCRETE_ISSUE_FLAGS):
            if strong_estimate_form and not any(
                flag in value_text for flag in (ESTIMATE_PATH_WEAKNESS_FLAGS | concrete_non_form_flags())
            ):
                continue
            return True
        if field == "contact_form" and value is False and not strong_estimate_form:
            return True
        if field in {"contact_form_detected", "estimate_form_detected"} and value_text == "no" and not strong_estimate_form:
            return True
        if field == "form_strength" and value_text in {"weak", "basic"}:
            return True
        if field == "phone_visible" and value is False:
            return True
        if field == "cta_terms" and value in ("", None, [], {}):
            return True
        if field == "cta_strength" and value_text in {"none", "weak"} and not strong_estimate_form:
            return True
        if field == "third_party_tools" and any(tool in value_text for tool in ("jobber", "housecall", "leadconnector")):
            return True

    return False


def concrete_non_form_flags() -> Set[str]:
    return {
        "premium_perception_gap",
        "premium_brand_mismatch",
        "owned_site_undersells_google_proof",
        "cheap_website",
        "site_looks_cheap",
        "outdated_site",
        "lawn_maintenance_positioning",
        "weak_project_gallery",
        "weak_gallery",
        "buried_project_photos",
        "project_photos_buried",
        "portfolio_presentation_pain",
        "weak_services_page",
        "unclear_service_pages",
        "high_ticket_services_unclear",
        "service_clarity_pain",
        "followup_or_missed_call_pain",
        "missed_call_path",
        "followup_gap",
    }


def has_specific_visible_pain(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    if categories & NON_FORM_VISIBLE_PAIN_CATEGORIES:
        return True

    strong_estimate_form = has_strong_estimate_form(row)
    if "estimate_path_pain" in categories:
        if not strong_estimate_form:
            return True
        return has_estimate_path_weakness_beyond_form_existence(row)

    if get_visible_pain_score(row) <= 0:
        return False

    reasons = get_visible_pain_reasons(row)
    if not reasons:
        return False
    if all(_clean(reason).lower() in GENERIC_OPTIMIZATION_REASONS for reason in reasons):
        return False
    if strong_estimate_form and all(is_form_absence_text(reason) for reason in reasons):
        return False
    return True


def has_estimate_path_weakness_beyond_form_existence(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    if categories & {"mobile_usability_pain", "trust_proof_pain"}:
        return True

    reasons_and_trigger = get_visible_pain_reasons(row) + [get_send_trigger(row)]
    for text in reasons_and_trigger:
        cleaned = _clean(text).lower()
        if not cleaned or is_form_absence_text(cleaned):
            continue
        if any(re.search(pattern, cleaned) for pattern in ESTIMATE_PATH_WEAKNESS_PATTERNS):
            return True
        if "estimate_path_pain" in categories and any(
            term in cleaned for term in ("buried", "mobile", "weak", "basic", "broken", "generic", "disconnected")
        ):
            return True

    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)
    flags = set(str(flag).lower() for flag in (website.get("site_issue_flags") or evidence.get("raw_flags") or []))
    if flags & ESTIMATE_PATH_WEAKNESS_FLAGS:
        return True

    if _clean(evidence.get("form_strength")).lower() in {"weak", "basic"}:
        return True

    for item in evidence_items(row):
        field = _clean(item.get("field")).lower()
        value_text = _clean(item.get("value")).lower()
        if field == "site_issue_flags" and any(flag in value_text for flag in ESTIMATE_PATH_WEAKNESS_FLAGS):
            return True
        if field == "form_strength" and value_text in {"weak", "basic"}:
            return True
        if field == "cta_strength" and value_text in {"none", "weak"}:
            return True

    return False


def has_form_absence_claim(row: Dict[str, Any]) -> bool:
    texts: List[str] = [get_send_trigger(row)]
    texts.extend(get_visible_pain_reasons(row))
    texts.extend(_clean(claim.get("claim")) for claim in normalize_safe_outreach_claims(row))
    texts.extend(
        [
            _pick(row, "opener", "Opener"),
            _pick(row, "recommended_action", "Recommended Action"),
            _pick(row, "Specific Observation", "specific_observation"),
        ]
    )
    return any(is_form_absence_text(text) for text in texts)


def is_form_absence_text(text: Any) -> bool:
    cleaned = _clean(text).lower()
    if not cleaned:
        return False
    return any(re.search(pattern, cleaned) for pattern in FORM_ABSENCE_PATTERNS)


def is_generic_optimization_only(row: Dict[str, Any]) -> bool:
    categories = set(get_visible_pain_categories(row))
    if categories & CONCRETE_VISIBLE_PAIN_CATEGORIES:
        return False
    reasons = get_visible_pain_reasons(row)
    if not reasons:
        return True
    return all(_clean(reason).lower() in GENERIC_OPTIMIZATION_REASONS for reason in reasons)


def has_strong_estimate_form(row: Dict[str, Any]) -> bool:
    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)

    estimate_value = (
        _pick(row, "Estimate Form Detected", "estimate_form_detected")
        or evidence.get("estimate_form_detected")
        or website.get("estimate_form_detected")
    )
    strength_value = (
        _pick(row, "Form Strength", "form_strength")
        or evidence.get("form_strength")
        or website.get("form_strength")
    )

    return _clean(estimate_value).lower() == "yes" and _clean(strength_value).lower() == "strong"


def get_send_trigger(row: Dict[str, Any]) -> str:
    evidence = _evidence_profile(row)
    raw = _raw_evidence(row)
    raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
    return _pick(row, "Send Trigger", "send_trigger") or _clean(evidence.get("send_trigger")) or _clean(
        raw_evidence.get("send_trigger") if isinstance(raw_evidence, dict) else ""
    )


def get_visible_pain_score(row: Dict[str, Any]) -> int:
    evidence = _evidence_profile(row)
    raw = _raw_evidence(row)
    raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
    value = (
        _pick(row, "Visible Pain Score", "visible_pain_score")
        or evidence.get("visible_pain_score")
        or (raw_evidence.get("visible_pain_score") if isinstance(raw_evidence, dict) else "")
    )
    return _parse_int(value)


def get_visible_pain_reasons(row: Dict[str, Any]) -> List[str]:
    value: Any = row.get("Visible Pain Reasons")
    if value in ("", None, [], {}):
        value = row.get("visible_pain_reasons")
    if value in ("", None, [], {}):
        evidence = _evidence_profile(row)
        value = evidence.get("visible_pain_reasons")
    if value in ("", None, [], {}):
        raw = _raw_evidence(row)
        raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
        if isinstance(raw_evidence, dict):
            value = raw_evidence.get("visible_pain_reasons")

    value = _loads_json_if_possible(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;\n]+", value) if part.strip()]
    return []


def get_visible_pain_categories(row: Dict[str, Any]) -> List[str]:
    value: Any = row.get("Visible Pain Categories")
    if value in ("", None, [], {}):
        value = row.get("visible_pain_categories")
    if value in ("", None, [], {}):
        evidence = _evidence_profile(row)
        value = evidence.get("visible_pain_categories")
    if value in ("", None, [], {}):
        raw = _raw_evidence(row)
        raw_evidence = raw.get("evidence", {}) if isinstance(raw, dict) else {}
        if isinstance(raw_evidence, dict):
            value = raw_evidence.get("visible_pain_categories")

    value = _loads_json_if_possible(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;\n]+", value) if part.strip()]
    return []


def required_visible_pain_score(reviews: int, *, review_band: str = "", money_signal: bool = False) -> int:
    if review_band == "mature" and not money_signal:
        return 40
    if review_band == "mature":
        return 35
    if reviews >= 75:
        return 30
    if reviews >= 21:
        return 16
    if reviews >= 5:
        return 10
    return 30


def is_exceptional_low_review_fit(row: Dict[str, Any]) -> bool:
    if _review_count(row) >= 5:
        return False
    if _rating(row) < 4.8:
        return False
    return _normalized_site_status(row) in {"missing", "broken", "no website", "no_website", "broken website"}


def _best_angle(row: Dict[str, Any]) -> str:
    return _pick(row, "best_angle", "Offer Fit", "Best Service Angle")


def has_money_signal(row: Dict[str, Any]) -> bool:
    return buyer_has_money_signal(row)


def _review_demand_band(row: Dict[str, Any]) -> str:
    explicit = _clean(_pick(row, "Review Demand Band", "Review Fit Band", "review_demand_band", "review_fit_band")).lower()
    if explicit:
        return explicit
    raw = _raw_evidence(row)
    if isinstance(raw, dict):
        return review_demand_band(raw)
    return ""


def _review_count(row: Dict[str, Any]) -> int:
    raw = _raw_evidence(row)
    google = raw.get("google", {}) if isinstance(raw, dict) else {}
    return _parse_int(_pick(row, "Reviews", "reviews", "google_review_count") or google.get("reviews"))


def _rating(row: Dict[str, Any]) -> float:
    raw = _raw_evidence(row)
    google = raw.get("google", {}) if isinstance(raw, dict) else {}
    return _parse_float(_pick(row, "Rating", "rating", "google_rating") or google.get("rating"))


def _normalized_site_status(row: Dict[str, Any]) -> str:
    raw = _raw_evidence(row)
    website = raw.get("website", {}) if isinstance(raw, dict) else {}
    evidence = _evidence_profile(row)
    return _clean(
        _pick(row, "Site Status", "site_status") or evidence.get("site_status") or website.get("site_status")
    ).lower()


def has_demand_signal(row: Dict[str, Any]) -> bool:
    if has_money_signal(row):
        return True

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


def _escape_claim_text(text: str) -> str:
    """Make a claim safe to sit inside the `"; "`-joined flat string.

    The flat form is what a rep reads in Airtable, so it stays flat rather than
    becoming JSON. Escaping the backslash first is what keeps the two
    substitutions from colliding: a claim already containing `\\;` must survive
    the round trip as `\\;`, not turn into a delimiter.
    """
    return text.replace("\\", "\\\\").replace(";", "\\;")


def _split_escaped_claims(value: str) -> List[str]:
    """Split on unescaped `;` and unescape each fragment, in one left-to-right pass.

    Splitting and unescaping have to share a scan: doing them separately makes
    the answer depend on how many backslashes precede a delimiter. Only `\\\\`
    and `\\;` are treated as escapes — a lone backslash in a legacy row (a
    Windows path, say) is left exactly as written, and a legacy raw `;` splits
    precisely as it always has.
    """
    parts: List[str] = []
    buffer: List[str] = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length and value[index + 1] in ("\\", ";"):
            buffer.append(value[index + 1])
            index += 2
            continue
        if char == ";":
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def _parse_flattened_safe_claims(value: str) -> List[Dict[str, Any]]:
    claims: List[Dict[str, Any]] = []
    for part in _split_escaped_claims(value):
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
