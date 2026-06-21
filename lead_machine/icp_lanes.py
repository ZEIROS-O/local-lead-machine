from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

from .evidence import build_evidence_profile
from .icp import hard_icp_gate
from .schema import normalize_lead
from .verticals import (
    HARDSCAPE_BATCH_TERMS as PROFILE_HARDSCAPE_BATCH_TERMS,
    HARDSCAPE_SERVICE_TERMS as PROFILE_HARDSCAPE_SERVICE_TERMS,
    VerticalProfile,
    resolve_profile,
)


LANES = (
    "no_site_launch",
    "social_jobber_escape",
    "owned_weak_site_rebuild",
    "premium_perception_gap",
    "portfolio_presentation_gap",
    "estimate_funnel",
    "optimization_only",
    "skip",
)

SELECTABLE_LANES = tuple(lane for lane in LANES if lane != "skip")

NON_OWNED_DOMAINS = {
    "facebook.com",
    "fb.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "getjobber.com",
    "clienthub.getjobber.com",
    "angi.com",
    "homeadvisor.com",
    "thumbtack.com",
    "houzz.com",
    "yelp.com",
    "nextdoor.com",
    "porch.com",
    "godaddysites.com",
    "wordpress.com",
    "wixsite.com",
    "square.site",
    "business.site",
    "sites.google.com",
    "linktr.ee",
    "carrd.co",
}

NON_OWNED_URL_TERMS = {
    "facebook.",
    "instagram.",
    "clienthub.getjobber.com",
    "getjobber.com",
    "jobber",
    "angi.com",
    "homeadvisor",
    "thumbtack",
    "houzz.",
    "yelp.",
    "nextdoor.",
    "porch.",
    "godaddysites.com",
    "wordpress.com",
    "wixsite.com",
    "square.site",
    "business.site",
    "sites.google.com",
    "linktr.ee",
    "carrd.co",
}

MICROSITE_URL_TERMS = {
    "godaddysites.com",
    "wordpress.com",
    "wixsite.com",
    "sites.google.com",
    "square.site",
    "linktr.ee",
    "carrd.co",
    "business.site",
}

PREMIUM_FLAGS = {
    "premium_perception_gap",
    "premium_brand_mismatch",
    "owned_site_undersells_google_proof",
    "cheap_website",
    "site_looks_cheap",
    "outdated_site",
    "lawn_maintenance_positioning",
}

PORTFOLIO_FLAGS = {
    "weak_project_gallery",
    "weak_gallery",
    "buried_project_photos",
    "project_photos_buried",
    "portfolio_presentation_pain",
}

ESTIMATE_FLAGS = {
    "no_form_detected",
    "missing_contact_path",
    "missing_estimate_path",
    "weak_basic_form",
    "no_clear_cta_detected",
    "buried_cta",
    "only_phone_email_path",
    "broken_form",
    "form_error",
    "phone_not_visible",
    "buried_phone",
    "weak_mobile_path",
    "bad_mobile_status",
}

WEAK_OPTIMIZATION_FLAGS = {
    "generic_copy_signals",
    "weak_services_page",
    "unclear_service_pages",
    "service_clarity_pain",
    "low_visible_trust",
    "reviews_not_near_cta",
    "trust_proof_pain",
    "followup_or_missed_call_pain",
    "missed_call_path",
    "followup_gap",
}

IRRELEVANT_TERMS = {
    "garden center",
    "nursery",
    "wholesale",
    "equipment rental",
    "landscape supply",
    "stone supply",
    "mulch supplier",
    "materials supplier",
    "snow removal only",
}

TARGET_SERVICE_TERMS = {
    "hardscape",
    "hardscaping",
    "paver",
    "pavers",
    "patio",
    "retaining wall",
    "outdoor living",
    "masonry",
    "brick",
    "concrete",
    "landscape",
    "landscaper",
    "landscaping",
    "contractor",
    "roof",
    "roofing",
    "hvac",
    "heating",
    "air conditioning",
    "plumbing",
    "electric",
    "remodel",
    "renovation",
}

HARDSCAPE_BATCH_TERMS = set(PROFILE_HARDSCAPE_BATCH_TERMS)
HARDSCAPE_SERVICE_TERMS = set(PROFILE_HARDSCAPE_SERVICE_TERMS)

SITE_WEAKNESS_FLAGS = {
    "generic_copy_signals",
    "weak_services_page",
    "unclear_service_pages",
    "high_ticket_services_unclear",
    "service_clarity_pain",
    "low_visible_trust",
    "reviews_not_near_cta",
    "trust_proof_pain",
}

GENERIC_SERVICE_TERMS = {
    "service",
    "home service",
    "home services",
    "construction",
    "builder",
    "repair",
}


@dataclass(frozen=True)
class ICPLaneResult:
    lane: str
    tier: str
    score: int
    reasons: List[str]
    blockers: List[str]
    evidence_refs: List[str]


@dataclass(frozen=True)
class _LaneContext:
    record: Dict[str, Any]
    google: Dict[str, Any]
    website: Dict[str, Any]
    evidence: Dict[str, Any]
    profile: VerticalProfile
    primary_url: str
    status: str
    rendered_status: str
    flags: set[str]
    categories: set[str]
    visible_pain_score: int
    visible_pain_reasons: List[str]
    send_trigger: str
    phone: str
    rating: float
    reviews: int
    text: str
    service_text: str
    has_owned_site: bool
    is_non_owned_url: bool
    strong_estimate_form: bool
    target_batch: bool
    target_service_evidence: bool


def classify_icp_lane(record: Dict[str, Any]) -> ICPLaneResult:
    ctx = _context(record)
    blockers = _hard_blockers(ctx)

    if blockers:
        lane = "skip"
        reasons = _base_reasons(ctx) + blockers
    else:
        lane, reasons = _choose_lane(ctx)

    score = _score(ctx, lane=lane, blockers=blockers)
    tier = _tier_for_score(score)
    if lane == "skip":
        tier = "Skip"

    refs = _evidence_refs_for_lane(ctx, lane)
    return ICPLaneResult(
        lane=lane,
        tier=tier,
        score=score,
        reasons=_dedupe(reasons),
        blockers=_dedupe(blockers),
        evidence_refs=refs,
    )


def classify_records_by_lane(records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in LANES}
    for record in records:
        grouped[classify_icp_lane(record).lane].append(record)
    return grouped


def lane_sort_key(record: Dict[str, Any], lane: str) -> tuple:
    result = classify_icp_lane(record)
    ctx = _context(record)
    tier_rank = {"A": 0, "B": 1, "C": 2, "Skip": 3}.get(result.tier, 4)
    review_rank = _review_band_rank(ctx.reviews)
    lane_mismatch = 0 if result.lane == lane else 1
    return (
        lane_mismatch,
        tier_rank,
        -result.score,
        -ctx.visible_pain_score,
        review_rank,
        -ctx.reviews,
        _business_name(ctx).lower(),
    )


def _context(record: Dict[str, Any]) -> _LaneContext:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    evidence = _evidence(record)
    profile = _profile_for_record(record, google)
    primary_url = _primary_url(record, google, website)
    status = _normalize_status(_pick(evidence, "site_status") or _pick(website, "site_status"))
    rendered_status = _normalize_status(_pick(evidence, "rendered_site_status") or _pick(website, "rendered_site_status"))
    flags = _normalized_set(
        _listish(evidence.get("raw_flags"))
        + _listish(evidence.get("site_issue_flags"))
        + _listish(website.get("site_issue_flags"))
    )
    categories = _normalized_set(
        _listish(evidence.get("visible_pain_categories"))
        + _listish(record.get("visible_pain_categories"))
    )
    visible_pain_reasons = _listish(
        evidence.get("visible_pain_reasons") or record.get("visible_pain_reasons") or record.get("Visible Pain Reasons")
    )
    send_trigger = _pick(record, "send_trigger", "Send Trigger") or _pick(evidence, "send_trigger")
    phone = _pick(google, "phone", "Phone") or _pick(record, "phone", "Phone")
    rating = _safe_float(_pick(google, "rating", "Rating", "google_rating") or _pick(record, "rating", "Rating"))
    reviews = _safe_int(
        _pick(google, "reviews", "Reviews", "google_review_count") or _pick(record, "reviews", "Reviews")
    )
    text = _searchable_text(record, google, website, evidence)
    service_text = _service_evidence_text(record, google, website, evidence)
    is_non_owned_url = _is_non_owned_url(primary_url) or status in {"social_only", "jobber_only"}
    has_owned_site = bool(primary_url) and not is_non_owned_url and status not in {
        "",
        "missing",
        "no_website",
        "social_only",
        "jobber_only",
    }
    strong_estimate_form = (
        _normalize_status(
            _pick(record, "estimate_form_detected", "Estimate Form Detected")
            or _pick(evidence, "estimate_form_detected")
            or _pick(website, "estimate_form_detected")
        )
        == "yes"
        and _normalize_status(
            _pick(record, "form_strength", "Form Strength")
            or _pick(evidence, "form_strength")
            or _pick(website, "form_strength")
        )
        == "strong"
    )

    return _LaneContext(
        record=record,
        google=google,
        website=website,
        evidence=evidence,
        profile=profile,
        primary_url=primary_url,
        status=status,
        rendered_status=rendered_status,
        flags=flags,
        categories=categories,
        visible_pain_score=_safe_int(
            _pick(record, "visible_pain_score", "Visible Pain Score") or evidence.get("visible_pain_score")
        ),
        visible_pain_reasons=visible_pain_reasons,
        send_trigger=send_trigger,
        phone=phone,
        rating=rating,
        reviews=reviews,
        text=text,
        service_text=service_text,
        has_owned_site=has_owned_site,
        is_non_owned_url=is_non_owned_url,
        strong_estimate_form=strong_estimate_form,
        target_batch=_is_target_batch(record, google, profile),
        target_service_evidence=_has_any_phrase(service_text, profile.service_terms),
    )


def _evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    evidence = record.get("evidence")
    if isinstance(evidence, dict) and evidence:
        if all(key in evidence for key in ("site_status", "visible_pain_score", "send_trigger")):
            return evidence
        try:
            rebuilt = build_evidence_profile(record)
        except Exception:
            return evidence
        merged = dict(rebuilt)
        merged.update(evidence)
        return merged
    try:
        return build_evidence_profile(record)
    except Exception:
        return {}


def _hard_blockers(ctx: _LaneContext) -> List[str]:
    blockers: List[str] = []
    lead = normalize_lead(
        ctx.google or ctx.record,
        fallback_vertical=_pick(ctx.google, "vertical", "Vertical")
        or _pick(ctx.record, "vertical", "Vertical")
        or ctx.profile.key,
    )
    gate = hard_icp_gate(lead)
    if not ctx.phone:
        blockers.append("missing phone")
    if ctx.rating and ctx.rating < 4.3:
        blockers.append("rating below 4.3")
    for reason in gate.reasons:
        if reason == "weak_reputation":
            continue
        blockers.append(reason.replace("_", " "))
    if any(term in ctx.text for term in IRRELEVANT_TERMS):
        blockers.append("irrelevant or supply-side business")
    return blockers


def _profile_for_record(record: Dict[str, Any], google: Dict[str, Any]) -> VerticalProfile:
    vertical = _pick(record, "vertical", "Vertical") or _pick(google, "vertical", "Vertical")
    source_query = _pick(record, "source_query", "Source Query") or _pick(google, "source_query", "Source Query")
    return resolve_profile(vertical, source_query=source_query)


def _choose_lane(ctx: _LaneContext) -> tuple[str, List[str]]:
    reasons = _base_reasons(ctx)

    if ctx.is_non_owned_url:
        return "social_jobber_escape", reasons + [_non_owned_reason(ctx.primary_url)]

    if _is_missing_or_broken_site(ctx):
        if _has_demand_proof(ctx):
            return "no_site_launch", reasons + [_structural_site_reason(ctx)]
        return "skip", reasons + ["missing demand proof for no-site lane"]

    premium = _has_premium_gap(ctx)
    portfolio = _has_portfolio_gap(ctx)
    estimate = _has_estimate_issue(ctx)
    weak_owned = _has_owned_weak_site(ctx)
    weak_signal = _has_weak_signal_only(ctx)

    if premium:
        return "premium_perception_gap", reasons + ["premium perception gap evidence"]

    if portfolio:
        return "portfolio_presentation_gap", reasons + ["portfolio presentation gap evidence"]

    if estimate:
        if _reviews_require_very_strong_pain(ctx) and not _very_strong_pain(ctx):
            return "optimization_only", reasons + ["151+ reviews requires stronger pain than this estimate signal"]
        if _reviews_require_clear_pain(ctx) and not _clear_concrete_pain(ctx):
            return "optimization_only", reasons + ["76+ reviews requires clear concrete pain"]
        estimate_reasons = ["estimate-path issue"]
        if _target_service_fit_missing(ctx):
            estimate_reasons.append(f"{ctx.profile.key} service evidence is weak/generic")
        if _weak_estimate_signal_only(ctx):
            estimate_reasons.append("quote-path signal is basic or weak-form-only")
        return "estimate_funnel", reasons + estimate_reasons

    if weak_owned:
        if _reviews_require_very_strong_pain(ctx) and not _very_strong_pain(ctx):
            return "optimization_only", reasons + ["151+ reviews with weak signal only"]
        if _reviews_require_clear_pain(ctx) and not _clear_concrete_pain(ctx):
            return "optimization_only", reasons + ["76+ reviews requires clear concrete pain"]
        return "owned_weak_site_rebuild", reasons + ["owned loaded site is weak with visible pain"]

    if _is_loaded_or_okay(ctx) and (weak_signal or ctx.visible_pain_score > 0):
        return "optimization_only", reasons + ["loaded site has only weaker optimization/routing issues"]

    return "skip", reasons + ["strong or okay site with no concrete pain"]


def _score(ctx: _LaneContext, *, lane: str, blockers: List[str]) -> int:
    demand = _demand_points(ctx)
    service = _service_fit_points(ctx)
    pain = _pain_points(ctx, lane)
    mockup = _mockup_points(ctx, lane)
    reachability = 10 if ctx.phone else 0
    confidence = _evidence_confidence_points(ctx)

    if _reviews_require_clear_pain(ctx) and not _clear_concrete_pain(ctx):
        pain = min(pain, 12)
        mockup = min(mockup, 8)
    if _reviews_require_very_strong_pain(ctx) and not _very_strong_pain(ctx):
        pain = min(pain, 10)
        mockup = min(mockup, 7)

    score = demand + service + pain + mockup + reachability + confidence

    if lane == "optimization_only":
        score = min(score, 79)
        if _has_weak_signal_only(ctx) and ctx.reviews >= 151:
            score = min(score, 64)
        if _weak_estimate_signal_only(ctx) and ctx.reviews >= 151:
            score = min(score, 64)

    if lane == "estimate_funnel":
        if _target_service_fit_missing(ctx):
            score = min(score, 64)
        if _weak_estimate_signal_only(ctx) and not _strong_project_value_fit(ctx):
            score = min(score, 79)
        if _no_estimate_with_visible_contact_path(ctx):
            score = min(score, 79)

    if lane == "skip" or blockers:
        score = min(score, 49)

    return max(0, min(100, int(score)))


def _demand_points(ctx: _LaneContext) -> int:
    if ctx.rating and ctx.rating < 4.3:
        return 0
    rating_points = 0
    if ctx.rating >= 4.8:
        rating_points = 8
    elif ctx.rating >= 4.5:
        rating_points = 6
    elif ctx.rating >= 4.3:
        rating_points = 4

    if 5 <= ctx.reviews <= 20:
        review_points = 15
    elif 21 <= ctx.reviews <= 75:
        review_points = 17
    elif 76 <= ctx.reviews <= 150:
        review_points = 14
    elif ctx.reviews >= 151:
        review_points = 12
    elif 1 <= ctx.reviews <= 4:
        review_points = 4
    else:
        review_points = 0

    return min(25, rating_points + review_points)


def _service_fit_points(ctx: _LaneContext) -> int:
    if any(term in ctx.text for term in IRRELEVANT_TERMS):
        return 0
    if ctx.target_batch:
        if ctx.target_service_evidence:
            return 20
        if any(term in ctx.service_text for term in GENERIC_SERVICE_TERMS):
            return 10
        return 8
    if any(term in ctx.service_text for term in TARGET_SERVICE_TERMS):
        return 20
    if any(term in ctx.service_text for term in GENERIC_SERVICE_TERMS):
        return 14
    return 10


def _pain_points(ctx: _LaneContext, lane: str) -> int:
    if lane == "no_site_launch":
        return 25
    if lane == "social_jobber_escape":
        return 23
    if lane in {"premium_perception_gap", "portfolio_presentation_gap"}:
        return min(25, 21 + max(1, ctx.visible_pain_score // 10))
    if lane == "estimate_funnel":
        base = 18
        if ctx.flags & {"broken_form", "form_error"}:
            base = 24
        elif ctx.flags & {"no_form_detected", "missing_estimate_path", "missing_contact_path"}:
            base = 22
        elif _form_strength(ctx) in {"weak", "basic"}:
            base = 19
        return min(25, max(base, ctx.visible_pain_score))
    if lane == "owned_weak_site_rebuild":
        return min(25, max(16, ctx.visible_pain_score))
    if lane == "optimization_only":
        return min(14, max(6, ctx.visible_pain_score))
    return min(8, ctx.visible_pain_score)


def _mockup_points(ctx: _LaneContext, lane: str) -> int:
    if lane == "no_site_launch":
        return 15
    if lane == "social_jobber_escape":
        return 14
    if lane in {"premium_perception_gap", "portfolio_presentation_gap", "owned_weak_site_rebuild"}:
        return 13
    if lane == "estimate_funnel":
        return 11 if _is_loaded_or_okay(ctx) else 13
    if lane == "optimization_only":
        return 5
    return 0


def _evidence_confidence_points(ctx: _LaneContext) -> int:
    items = ctx.evidence.get("evidence_items")
    has_items = isinstance(items, list) and bool(items)
    has_rendered = ctx.rendered_status in {"loaded", "loads", "okay"}
    has_pain = bool(ctx.categories or ctx.flags or ctx.visible_pain_reasons or ctx.send_trigger)
    if has_items and (has_rendered or has_pain):
        return 5
    if has_items or has_pain:
        return 4
    if ctx.status or ctx.primary_url:
        return 3
    return 2


def _tier_for_score(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "Skip"


def _base_reasons(ctx: _LaneContext) -> List[str]:
    reasons: List[str] = []
    if ctx.rating or ctx.reviews:
        reasons.append(f"{ctx.rating:g} rating / {ctx.reviews} reviews")
    if ctx.phone:
        reasons.append("phone present")
    if ctx.visible_pain_score:
        reasons.append(f"visible pain score {ctx.visible_pain_score}")
    if ctx.send_trigger:
        reasons.append(ctx.send_trigger)
    return reasons


def _evidence_refs_for_lane(ctx: _LaneContext, lane: str) -> List[str]:
    fields = {
        "google_rating",
        "site_status",
        "site_issue_flags",
        "visible_pain_categories",
        "visible_pain_reasons",
        "send_trigger",
    }
    if lane in {"owned_weak_site_rebuild", "premium_perception_gap", "portfolio_presentation_gap", "estimate_funnel"}:
        fields.update({"rendered_site_status", "estimate_form_detected", "contact_form_detected", "form_strength", "cta_strength"})
    items = ctx.evidence.get("evidence_items")
    if not isinstance(items, list):
        return []
    refs: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip().lower()
        item_id = str(item.get("id") or "").strip()
        if item_id and field in fields and item_id not in refs:
            refs.append(item_id)
    return refs


def _has_demand_proof(ctx: _LaneContext) -> bool:
    return ctx.reviews >= 5 and ctx.rating >= 4.3


def _is_missing_or_broken_site(ctx: _LaneContext) -> bool:
    if not ctx.primary_url:
        return True
    return ctx.status in {"missing", "no_website", "broken", "broken_website"}


def _structural_site_reason(ctx: _LaneContext) -> str:
    if ctx.status in {"broken", "broken_website"}:
        return "website appears broken"
    return "no owned website found"


def _has_owned_weak_site(ctx: _LaneContext) -> bool:
    return (
        ctx.has_owned_site
        and ctx.status == "weak"
        and ctx.visible_pain_score >= 16
        and _has_site_itself_weak_pain(ctx)
        and not _estimate_signal_only(ctx)
    )


def _has_premium_gap(ctx: _LaneContext) -> bool:
    return ctx.has_owned_site and (
        "premium_perception_gap" in ctx.categories
        or bool(ctx.flags & PREMIUM_FLAGS)
    )


def _has_portfolio_gap(ctx: _LaneContext) -> bool:
    return ctx.has_owned_site and (
        "portfolio_presentation_pain" in ctx.categories
        or bool(ctx.flags & PORTFOLIO_FLAGS)
    )


def _has_estimate_issue(ctx: _LaneContext) -> bool:
    if not ctx.has_owned_site:
        return False
    if ctx.strong_estimate_form and not _estimate_issue_beyond_strong_form(ctx):
        return False
    if "estimate_path_pain" in ctx.categories:
        return True
    if ctx.flags & ESTIMATE_FLAGS:
        return True
    if _form_strength(ctx) in {"weak", "basic"}:
        return True
    estimate_form = _normalize_status(
        _pick(ctx.evidence, "estimate_form_detected") or _pick(ctx.website, "estimate_form_detected")
    )
    contact_form = _normalize_status(
        _pick(ctx.evidence, "contact_form_detected") or _pick(ctx.website, "contact_form_detected")
    )
    cta_strength = _normalize_status(_pick(ctx.evidence, "cta_strength") or _pick(ctx.website, "cta_strength"))
    if estimate_form == "no" or contact_form == "no":
        return True
    if cta_strength in {"none", "weak"}:
        return True
    if ctx.evidence.get("has_estimate_cta") is False:
        return True
    if _normalize_status(_pick(ctx.evidence, "phone_consistency") or _pick(ctx.record, "phone_consistency")) == "different":
        return True
    return False


def _estimate_issue_beyond_strong_form(ctx: _LaneContext) -> bool:
    if ctx.flags & (ESTIMATE_FLAGS - {"no_form_detected", "missing_contact_path", "missing_estimate_path"}):
        return True
    if ctx.categories & {"mobile_usability_pain", "trust_proof_pain", "followup_or_missed_call_pain"}:
        return True
    texts = " ".join(ctx.visible_pain_reasons + [ctx.send_trigger]).lower()
    return any(term in texts for term in ("buried", "weak", "basic", "broken", "mobile", "routing", "phone"))


def _has_weak_signal_only(ctx: _LaneContext) -> bool:
    if ctx.categories & {"premium_perception_gap", "portfolio_presentation_pain", "estimate_path_pain", "no_site_or_social_only_pain", "broken_site_pain"}:
        return False
    if ctx.flags & (PREMIUM_FLAGS | PORTFOLIO_FLAGS | ESTIMATE_FLAGS):
        return False
    if ctx.flags & WEAK_OPTIMIZATION_FLAGS:
        return True
    if ctx.visible_pain_score and ctx.visible_pain_score < 16:
        return True
    lowered_reasons = {reason.lower() for reason in ctx.visible_pain_reasons}
    return bool(lowered_reasons & {"generic/template website copy", "minor optimization angle only"})


def _clear_concrete_pain(ctx: _LaneContext) -> bool:
    if _is_missing_or_broken_site(ctx) or ctx.is_non_owned_url:
        return True
    if _weak_estimate_signal_only(ctx):
        return False
    if ctx.categories & {"premium_perception_gap", "portfolio_presentation_pain", "no_site_or_social_only_pain", "broken_site_pain"}:
        return True
    if ctx.flags & (PREMIUM_FLAGS | PORTFOLIO_FLAGS):
        return True
    if _has_estimate_issue(ctx) and ctx.visible_pain_score >= 16 and not _weak_estimate_signal_only(ctx):
        return True
    return bool(ctx.send_trigger and ctx.visible_pain_score >= 20)


def _very_strong_pain(ctx: _LaneContext) -> bool:
    if _is_missing_or_broken_site(ctx) or ctx.is_non_owned_url:
        return True
    if ctx.flags & {"broken_form", "form_error"}:
        return True
    if ctx.categories & {"premium_perception_gap", "portfolio_presentation_pain"} and ctx.visible_pain_score >= 22:
        return True
    if ctx.flags & (PREMIUM_FLAGS | PORTFOLIO_FLAGS) and ctx.visible_pain_score >= 22:
        return True
    if "estimate_path_pain" in ctx.categories and ctx.visible_pain_score >= 28 and not _weak_estimate_signal_only(ctx):
        return True
    return bool(ctx.send_trigger and ctx.visible_pain_score >= 30 and not _weak_estimate_signal_only(ctx))


def _reviews_require_clear_pain(ctx: _LaneContext) -> bool:
    return ctx.reviews >= 76


def _reviews_require_very_strong_pain(ctx: _LaneContext) -> bool:
    return ctx.reviews >= 151


def _is_loaded_or_okay(ctx: _LaneContext) -> bool:
    return ctx.rendered_status in {"loaded", "loads", "okay"} or ctx.status in {"okay", "loaded", "loads", "strong"}


def _review_band_rank(reviews: int) -> int:
    if 5 <= reviews <= 75:
        return 0
    if 76 <= reviews <= 150:
        return 1
    if reviews >= 151:
        return 2
    return 3


def _form_strength(ctx: _LaneContext) -> str:
    return _normalize_status(
        _pick(ctx.record, "form_strength", "Form Strength")
        or _pick(ctx.evidence, "form_strength")
        or _pick(ctx.website, "form_strength")
    )


def _primary_url(record: Dict[str, Any], google: Dict[str, Any], website: Dict[str, Any]) -> str:
    homepage = website.get("homepage") if isinstance(website.get("homepage"), dict) else {}
    return (
        _pick(google, "website", "website_url", "Website URL")
        or _pick(record, "website_url", "Website URL")
        or _pick(website, "final_url")
        or _pick(homepage, "url")
    )


def _is_non_owned_url(url: str) -> bool:
    lowered = url.lower()
    if any(term in lowered for term in NON_OWNED_URL_TERMS):
        return True
    host = _domain(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in NON_OWNED_DOMAINS)


def _non_owned_reason(url: str) -> str:
    lowered = url.lower()
    if "jobber" in lowered or "clienthub" in lowered:
        return "primary URL is Jobber/clienthub"
    if "facebook" in lowered or "instagram" in lowered:
        return "primary URL is social media"
    if any(term in lowered for term in MICROSITE_URL_TERMS):
        return "primary URL is microsite/non-owned site"
    if any(term in lowered for term in ("angi", "homeadvisor", "thumbtack", "houzz", "yelp", "nextdoor", "porch")):
        return "primary URL is third-party directory"
    return "primary URL is non-owned social/booking/directory URL"


def _business_name(ctx: _LaneContext) -> str:
    return _pick(ctx.google, "business_name", "Business Name", "name") or _pick(ctx.record, "business_name", "Business Name", "name")


def _searchable_text(
    record: Dict[str, Any],
    google: Dict[str, Any],
    website: Dict[str, Any],
    evidence: Dict[str, Any],
) -> str:
    parts = [
        _pick(google, "business_name", "Business Name", "name"),
        _pick(google, "category", "Category", "Google category"),
        _pick(google, "source_query", "Source Query"),
        _pick(google, "vertical", "Vertical"),
        _pick(record, "business_name", "Business Name", "name"),
        _pick(record, "category", "Category", "Google category"),
        _pick(record, "vertical", "Vertical"),
        _pick(website, "combined_text"),
        " ".join(_listish(website.get("high_ticket_terms"))),
        " ".join(_listish(evidence.get("services_detected"))),
    ]
    return " ".join(part for part in parts if part).lower()


def _service_evidence_text(
    record: Dict[str, Any],
    google: Dict[str, Any],
    website: Dict[str, Any],
    evidence: Dict[str, Any],
) -> str:
    parts = [
        _pick(google, "business_name", "Business Name", "name"),
        _pick(google, "category", "Category", "Google category"),
        _pick(record, "business_name", "Business Name", "name"),
        _pick(record, "category", "Category", "Google category"),
        _pick(website, "combined_text"),
        " ".join(_listish(website.get("high_ticket_terms"))),
        " ".join(_listish(evidence.get("services_detected"))),
    ]
    return " ".join(part for part in parts if part).lower()


def _is_target_batch(record: Dict[str, Any], google: Dict[str, Any], profile: VerticalProfile) -> bool:
    text = " ".join(
        [
            _pick(google, "source_query", "Source Query"),
            _pick(google, "vertical", "Vertical"),
            _pick(record, "source_query", "Source Query"),
            _pick(record, "vertical", "Vertical"),
        ]
    ).lower()
    return _has_any_phrase(text, profile.batch_terms)


def _target_service_fit_missing(ctx: _LaneContext) -> bool:
    return ctx.target_batch and not ctx.target_service_evidence


def _has_site_itself_weak_pain(ctx: _LaneContext) -> bool:
    if ctx.flags & SITE_WEAKNESS_FLAGS:
        return True
    if ctx.categories & {"service_clarity_pain", "trust_proof_pain"}:
        return True
    text = " ".join(ctx.visible_pain_reasons + [ctx.send_trigger]).lower()
    return any(
        term in text
        for term in (
            "outdated",
            "cheap",
            "template",
            "generic",
            "unclear service",
            "weak service",
            "underbuilt",
            "low visible trust",
        )
    )


def _estimate_signal_only(ctx: _LaneContext) -> bool:
    estimate_flags = bool(ctx.flags & ESTIMATE_FLAGS)
    estimate_categories = bool(ctx.categories & {"estimate_path_pain", "mobile_usability_pain"})
    non_estimate_flags = ctx.flags - ESTIMATE_FLAGS
    non_estimate_categories = ctx.categories - {"estimate_path_pain", "mobile_usability_pain"}
    return (estimate_flags or estimate_categories) and not non_estimate_flags and not non_estimate_categories


def _weak_estimate_signal_only(ctx: _LaneContext) -> bool:
    if ctx.flags & {"broken_form", "form_error", "missing_contact_path", "no_form_detected"}:
        return False
    if _normalize_status(_pick(ctx.evidence, "contact_form_detected") or _pick(ctx.website, "contact_form_detected")) == "no":
        return False
    if _form_strength(ctx) in {"weak", "basic"} or "weak_basic_form" in ctx.flags:
        return True
    return _no_estimate_with_visible_contact_path(ctx)


def _no_estimate_with_visible_contact_path(ctx: _LaneContext) -> bool:
    estimate_form = _normalize_status(
        _pick(ctx.evidence, "estimate_form_detected") or _pick(ctx.website, "estimate_form_detected")
    )
    contact_form = _normalize_status(
        _pick(ctx.evidence, "contact_form_detected") or _pick(ctx.website, "contact_form_detected")
    )
    cta_strength = _normalize_status(_pick(ctx.evidence, "cta_strength") or _pick(ctx.website, "cta_strength"))
    return (
        estimate_form == "no"
        and contact_form != "no"
        and cta_strength not in {"", "none", "weak"}
        and bool(ctx.phone or _listish(ctx.website.get("emails_found")))
    )


def _strong_project_value_fit(ctx: _LaneContext) -> bool:
    if not ctx.target_batch or not ctx.target_service_evidence:
        return False
    return _phrase_match_count(ctx.service_text, ctx.profile.service_terms) >= 3 and ctx.visible_pain_score >= 28


def _has_any_phrase(text: str, terms: Iterable[str]) -> bool:
    return any(_has_phrase(text, term) for term in terms)


def _phrase_match_count(text: str, terms: Iterable[str]) -> int:
    return sum(1 for term in terms if _has_phrase(text, term))


def _has_phrase(text: str, term: str) -> bool:
    cleaned_text = str(text or "").lower()
    cleaned_term = str(term or "").lower().strip()
    if not cleaned_text or not cleaned_term:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(cleaned_term)}s?(?![a-z0-9])"
    return re.search(pattern, cleaned_text) is not None


def _domain(url: str) -> str:
    cleaned = url.strip().lower()
    if not cleaned:
        return ""
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    host = parsed.netloc or parsed.path.split("/")[0]
    return re.sub(r"^www\.", "", host)


def _pick(values: Dict[str, Any], *keys: str) -> str:
    if not isinstance(values, dict):
        return ""
    for key in keys:
        value = values.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, (dict, list, tuple, set)):
                continue
            return str(value).strip()
    return ""


def _listish(value: Any) -> List[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple) or isinstance(value, set):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;\n]+", value) if part.strip()]
    return [str(value).strip()]


def _normalized_set(values: List[str]) -> set[str]:
    return {_normalize_status(value) for value in values if _normalize_status(value)}


def _normalize_status(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(value or "").strip().lower())


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: List[str]) -> List[str]:
    deduped: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            deduped.append(cleaned)
            seen.add(cleaned)
    return deduped
