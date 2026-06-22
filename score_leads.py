#!/usr/bin/env python3
"""
Phase 7: score enriched leads.

This version is delta-first, not raw-score-first.

Core logic:
- Reviews are an ICP-fit band, not monotonic "more = better".
- Priority is driven by visual/conversion delta, then capped by ICP fit.
- Whales/franchises/commercial/restoration-heavy roofing are downgraded.
- Missing form/CTA is de-weighted unless corroborated by other weakness.
- Packet Eligible = only A/B + a concrete rebuild, funnel, portfolio, premium-perception, mobile, service-page, or trust-proof angle.

Examples:
    python score_leads.py --input output/enriched_hardscaping.jsonl --output output/scored_hardscaping.csv --vertical hardscaping
    python score_leads.py --input output/enriched_roofing.jsonl --output output/scored_roofing.csv --vertical roofing
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from import_llm_score_bundle import import_score_bundle, validate_bundle_scores
from lead_machine.buyer_signals import (
    buyer_intent_summary,
    ensure_buyer_intent,
    has_money_signal as buyer_has_money_signal,
)
from lead_machine.email_hygiene import emails_text, sanitize_record_emails
from lead_machine.evidence import build_evidence_profile
from lead_machine.lead_card import build_lead_card
from lead_machine import llm_client
from lead_machine.llm_queue import safe_slug
from lead_machine.manual_scores import allowed_evidence_ids as original_evidence_ids
from lead_machine.manual_scores import load_manual_score, merge_manual_score
from lead_machine.schema import normalize_lead
from lead_machine.sendability import apply_sendability, normalize_evidence_refs
from lead_machine.verticals import VerticalProfile, resolve_profile
from make_llm_batch import MANUAL_SCORE_SCHEMA, score_file_for_record


PRIORITY_RANK = {"A": 0, "B": 1, "C": 2, "Skip": 3}
DELTA_RANK = {
    "Website Rebuild": 0,
    "Estimate Funnel": 1,
    "Premium Perception Gap": 1,
    "Portfolio Rebuild": 1,
    "Mobile Quote Path Cleanup": 1,
    "Service Page Cleanup": 2,
    "Review/Trust Proof Upgrade": 2,
    "Website Cleanup": 2,
    "Tracking / Missed Call": 2,
    "GBP / Reviews": 2,
    "SEO / Service Pages": 2,
    "Retargeting": 3,
    "Advanced Optimization": 3,
    "No-Site Launch": 0,
    "Social/Jobber Escape": 0,
    "Tracking": 2,
    "Skip": 9,
}
SITE_RANK = {"N/A": 0, "Poor": 0, "Okay": 1, "Good": 2, "Unknown": 3}

FRANCHISE_DENYLIST = [
    "storm guard",
    "mr. roof",
    "mr roof",
    "erie home",
    "erie construction",
    "power home",
    "leaffilter",
    "leaf filter",
    "renewal by andersen",
    "west shore",
    "long roofing",
    "groundworks",
    "beldon",
    "da bella",
    "window world",
]

CORPORATE_HINTS = [
    " of cleveland",
    " of akron",
    " of columbus",
    " of brecksville",
    " of northeast ohio",
]

COMMERCIAL_TERMS = [
    "commercial roofing",
    "commercial flat roof",
    "industrial roofing",
    "tpo",
    "epdm",
    "facility maintenance",
    "flat roof systems",
    "commercial roof repair",
]

RESTORATION_TERMS = [
    "restoration",
    "insurance claim",
    "insurance claims",
    "storm damage",
    "hail damage",
    "wind damage",
    "claims network",
    "mitigation",
]

HIGH_TICKET_TERMS = [
    "hardscape",
    "hardscaping",
    "patio",
    "paver",
    "retaining wall",
    "drainage",
    "outdoor living",
    "outdoor kitchen",
    "fire pit",
    "fire feature",
    "pergola",
    "pool",
    "landscape design",
    "design build",
    "hydroseeding",
    "roof replacement",
    "roof repair",
    "siding",
    "gutter",
    "masonry",
    "concrete",
    "brick",
]

BAD_REVIEW_THRESHOLD = 4.0
IDEAL_RATING_THRESHOLD = 4.4

PREMIUM_PERCEPTION_FLAGS = {
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

MOBILE_FLAGS = {"weak_mobile_path", "bad_mobile_status", "mobile_usability_pain"}
TRUST_FLAGS = {"low_visible_trust", "reviews_not_near_cta", "trust_proof_pain"}
SERVICE_CLARITY_FLAGS = {"weak_services_page", "unclear_service_pages", "high_ticket_services_unclear", "service_clarity_pain"}
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
}

CONCRETE_PACKET_OFFERS = {
    "Website Rebuild",
    "Estimate Funnel",
    "Premium Perception Gap",
    "Portfolio Rebuild",
    "Mobile Quote Path Cleanup",
    "Service Page Cleanup",
    "Review/Trust Proof Upgrade",
}


@dataclass(frozen=True)
class MissingManualScore:
    lead_name: str
    expected_paths: List[Path]


@dataclass(frozen=True)
class ManualScoreRun:
    rows: List[Dict[str, Any]]
    missing_scores: List[MissingManualScore]


@dataclass(frozen=True)
class ApiLlmSkip:
    lead_name: str
    score_file: str
    reason: str


@dataclass(frozen=True)
class ApiLlmRun:
    manual_run: ManualScoreRun
    calls: int
    input_tokens: int
    output_tokens: int
    tokens: int
    usd: float
    caps: Dict[str, int | float]
    stopped_by_cap: bool
    stop_reason: str = ""
    skipped_records: List[ApiLlmSkip] | None = None
    debug_notes: List[str] | None = None


DEFAULT_LLM_SCORING_MODEL = "claude-sonnet-4-6"
DEFAULT_LLM_MAX_TOKENS = 4096
DEFAULT_MAX_LLM_CALLS_PER_RUN = 5
DEFAULT_MAX_LLM_TOKENS_PER_RUN = 100_000
DEFAULT_MAX_LLM_USD_PER_RUN = 5.0
SONNET_4_6_INPUT_USD_PER_MILLION_TOKENS = 3.0
SONNET_4_6_OUTPUT_USD_PER_MILLION_TOKENS = 15.0
MAX_LLM_SCORING_STRING_CHARS = 1500
MAX_LLM_SCORING_SNIPPET_CHARS = 300
MAX_LLM_SCORING_SNIPPETS = 8

LLM_SCORING_SNIPPET_TERMS = (
    "ceramic coating",
    "paint protection film",
    "paint correction",
    "mobile detailing",
    "window tint",
    "vehicle wrap",
    "booking",
    "estimate",
    "quote",
    "appointment",
    "contact",
    "call",
    "text",
    "trust",
    "review",
    "testimonial",
    "no clear",
    "weak",
    "missing",
    "buried",
    "cta",
    "ppf",
)

LLM_SCORING_EVIDENCE_FIELDS = {
    "site_status",
    "site_issue_flags",
    "third_party_tools",
    "contact_form",
    "contact_form_detected",
    "estimate_form_detected",
    "form_strength",
    "form_detection_method",
    "cta_terms",
    "cta_strength",
    "services_detected",
    "trust_signals",
    "rendered_site_status",
    "rendered_status_reason",
    "desktop_screenshot_path",
    "mobile_screenshot_path",
    "visual_review_needed",
    "visual_pain_reasons",
    "visible_pain_score",
    "visible_pain_reasons",
    "visible_pain_categories",
    "send_trigger",
    "screenshots",
}

SCORE_WEIGHTS = {
    "business_strength": 30,
    "online_weakness": 35,
    "offer_fit": 20,
    "buyer_intent": 15,
    "raw_business_strength": 40,
    "raw_online_weakness": 40,
    "raw_offer_fit": 20,
    "priority_thresholds": {
        "cap_to_c_below": 35,
        "skip_below": 25,
    },
}


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def getenv_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def text_blob(record: Dict[str, Any]) -> str:
    w = record.get("website", {})
    g = record.get("google", {})
    parts = [
        g.get("business_name", ""),
        g.get("category", ""),
        w.get("homepage", {}).get("title", ""),
        w.get("homepage", {}).get("meta", ""),
        w.get("combined_text", ""),
    ]
    return " ".join(str(p or "") for p in parts).lower()


def _record_source_query(record: Dict[str, Any]) -> str:
    g = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    return str(record.get("source_query") or record.get("Source Query") or g.get("source_query") or g.get("Source Query") or "")


def _record_vertical(record: Dict[str, Any]) -> str:
    g = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    return str(record.get("vertical") or record.get("Vertical") or g.get("vertical") or g.get("Vertical") or "")


def has_any(text: str, terms: List[str] | tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def more_restrictive(a: str, b: str) -> str:
    return a if PRIORITY_RANK.get(a, 9) >= PRIORITY_RANK.get(b, 9) else b


def weighted_score(raw_score: int, raw_max: int, weight: int) -> int:
    if raw_max <= 0:
        return 0
    return min(weight, round(max(0, raw_score) * weight / raw_max))


def is_no_or_broken(record: Dict[str, Any]) -> bool:
    status = record.get("website", {}).get("site_status", "Unknown")
    return status in {"No Website", "Broken Website", "Social Only"}


def has_money_signal(record: Dict[str, Any]) -> bool:
    return buyer_has_money_signal(record)


def form_status(record: Dict[str, Any], key: str, default: str = "") -> str:
    website = record.get("website", {}) or {}
    evidence = record.get("evidence", {}) or {}
    value = website.get(key)
    if value in ("", None):
        value = evidence.get(key)
    return str(value or default)


def has_strong_estimate_form(record: Dict[str, Any]) -> bool:
    return (
        form_status(record, "estimate_form_detected").lower() == "yes"
        and form_status(record, "form_strength").lower() == "strong"
    )


def form_strength(record: Dict[str, Any]) -> str:
    return form_status(record, "form_strength", "None")


def site_issue_flags(record: Dict[str, Any]) -> set[str]:
    website = record.get("website", {}) or {}
    evidence = record.get("evidence", {}) or {}
    flags = website.get("site_issue_flags") or evidence.get("raw_flags") or []
    return {str(flag).strip().lower() for flag in flags if str(flag).strip()}


def visible_pain_categories(record: Dict[str, Any]) -> set[str]:
    evidence = record.get("evidence", {}) or {}
    value = evidence.get("visible_pain_categories") or []
    if isinstance(value, str):
        value = value.replace(",", ";").split(";")
    if not isinstance(value, list):
        value = []
    return {str(category).strip() for category in value if str(category).strip()}


def has_estimate_path_weakness(record: Dict[str, Any]) -> bool:
    categories = visible_pain_categories(record)
    flags = site_issue_flags(record)
    current_form_strength = form_strength(record).lower()
    if categories & {"estimate_path_pain", "mobile_usability_pain", "trust_proof_pain"}:
        return True
    if flags & ESTIMATE_PATH_WEAKNESS_FLAGS:
        return True
    return current_form_strength in {"weak", "basic"}


def infer_vertical(record: Dict[str, Any], fallback: str) -> str:
    blob = text_blob(record)
    name = record.get("google", {}).get("business_name", "").lower()

    if "roof" in blob or "roof" in name:
        return "Roofing"

    if any(t in blob for t in ["hardscape", "paver", "patio", "retaining wall", "outdoor living"]):
        return "Hardscaping"

    if any(t in blob for t in ["masonry", "brick", "concrete"]):
        return "Masonry / Hardscaping"

    if "landscape" in blob or "landscap" in name:
        return "Landscaping / Hardscaping"

    return fallback.title()


def vertical_family(vertical: str) -> str:
    profile = resolve_profile(vertical)
    if profile.key != "hardscaping" or "hard" in vertical.lower() or "landscap" in vertical.lower() or "masonry" in vertical.lower():
        return profile.family
    v = vertical.lower()
    if "roof" in v:
        return "roofing"
    return "other"


def fit_ceiling(
    record: Dict[str, Any],
    vertical: str,
    *,
    profile: VerticalProfile | None = None,
) -> Tuple[str, List[str]]:
    """
    Caps priority based on ICP closeability.

    A = eligible for top priority.
    B/C = keep, but do not treat as immediate top target.
    Skip = not worth current motion.
    """
    g = record.get("google", {})
    name = (g.get("business_name") or "").lower()
    blob = text_blob(record)
    rating = safe_float(g.get("rating"))
    reviews = safe_int(g.get("reviews"))
    no_site = is_no_or_broken(record)
    money_signal = has_money_signal(record)
    profile = profile or resolve_profile(_record_vertical(record) or vertical, source_query=_record_source_query(record))
    family = profile.family

    flags: List[str] = []

    if any(f in name for f in profile.franchise_denylist):
        flags.append("franchise/brand denylist")
        return "Skip", flags

    # "X of City" is not always franchise, but in roofing/home services it often indicates a vendorized location page.
    if family == "roofing" and any(h in name for h in CORPORATE_HINTS):
        flags.append("possible corporate/location-brand naming")
        return "C", flags

    if any(t in blob for t in COMMERCIAL_TERMS) or "commercial" in name:
        flags.append("commercial/bid-based focus")
        return "C", flags

    if rating and rating < BAD_REVIEW_THRESHOLD:
        flags.append(f"rating below {BAD_REVIEW_THRESHOLD}")
        return "Skip", flags

    if rating and rating < IDEAL_RATING_THRESHOLD:
        flags.append("rating below preferred 4.4")
        return "C", flags

    # Roofing restoration/insurance is not always bad, but it is often a slower/insurance-driven sale.
    if family == "roofing" and has_any(blob, RESTORATION_TERMS):
        flags.append("restoration/insurance-heavy roofing signal")
        if no_site:
            return "B", flags
        return "C", flags

    if no_site and not money_signal:
        flags.append("bad site without buyer-intent money signal")
        return "C", flags

    # Inverted-U review logic.
    # 5-20 reviews = best first-wave owner-led band. More reviews need clearer visible pain.
    if reviews >= 75:
        flags.append("75+ reviews requires very clear visible pain")
        if money_signal and no_site:
            flags.append("buyer-intent money signal supports high-review structural gap")
            return "A", flags
        return ("A" if no_site else "B"), flags

    if reviews >= 21:
        flags.append("21-75 reviews requires clear visible pain")
        return "A", flags

    if reviews < 5:
        flags.append("very thin review proof")
        if family == "hardscaping" and no_site and rating >= 4.8:
            return "C", flags
        return "C", flags

    flags.append("inside 5-20 sweet spot review/rating band")
    return "A", flags


def score_business_strength(
    record: Dict[str, Any],
    vertical: str,
    *,
    profile: VerticalProfile | None = None,
) -> Tuple[int, List[str]]:
    """
    Strength score is still useful, but it no longer directly controls priority.
    """
    g = record.get("google", {})
    w = record.get("website", {})
    d = record.get("derived", {})
    blob = text_blob(record)
    profile = profile or resolve_profile(_record_vertical(record) or vertical, source_query=_record_source_query(record))
    family = profile.family

    rating = safe_float(g.get("rating"))
    reviews = safe_int(g.get("reviews"))

    score = 0
    evidence: List[str] = []

    if rating >= 4.8:
        score += 8
        evidence.append(f"strong rating {rating}")
    elif rating >= 4.5:
        score += 6
        evidence.append(f"solid rating {rating}")
    elif rating >= 4.3:
        score += 3
        evidence.append(f"acceptable rating {rating}")

    # Inverted-U: reward enough proof, not infinite proof.
    if family == "hardscaping":
        if 5 <= reviews <= 20:
            score += 10
            evidence.append(f"{reviews} reviews inside 5-20 hardscaping sweet spot")
        elif 21 <= reviews <= 75:
            score += 7
            evidence.append(f"{reviews} reviews, workable proof but needs visible pain")
        elif reviews > 75:
            score += 4
            evidence.append(f"{reviews} reviews, higher-review business needs stronger visible pain")
    else:
        if 5 <= reviews <= 20:
            score += 10
            evidence.append(f"{reviews} reviews inside 5-20 sweet spot")
        elif 21 <= reviews <= 75:
            score += 7
            evidence.append(f"{reviews} reviews, workable proof but needs visible pain")
        elif reviews > 75:
            score += 4
            evidence.append(f"{reviews} reviews, higher-review business needs stronger visible pain")

    high_ticket_terms = profile.high_ticket_terms
    if profile.key == "hardscaping":
        high_ticket_terms = tuple(dict.fromkeys([*profile.high_ticket_terms, *HIGH_TICKET_TERMS]))
    if has_any(blob, high_ticket_terms) or d.get("has_high_ticket_terms"):
        score += 10
        evidence.append("high-ticket services detected")

    if re.search(r"\b(7|10|15|20|25|30|35|40|50)\+?\s+years\b", blob) or "years of experience" in blob:
        score += 5
        evidence.append("years/experience credibility mentioned")

    if d.get("has_social_links"):
        score += 4
        evidence.append("social links found")

    if any(t in blob for t in ["gallery", "portfolio", "project pictures", "projects", "before and after", "our work"]):
        score += 4
        evidence.append("portfolio/project proof detected")

    return min(score, 40), evidence


def score_online_weakness(record: Dict[str, Any]) -> Tuple[int, List[str]]:
    w = record.get("website", {})
    d = record.get("derived", {})
    flags = site_issue_flags(record)
    categories = visible_pain_categories(record)
    status = w.get("site_status", "Unknown")
    blob = text_blob(record)
    strong_estimate_form = has_strong_estimate_form(record)

    score = 0
    evidence: List[str] = []

    if status == "No Website":
        score += 25
        evidence.append("no website")
    elif status == "Broken Website":
        score += 25
        evidence.append("broken website")
    elif status == "Social Only":
        score += 18
        evidence.append("social-only web presence")

    if d.get("non_standard_site_or_booking"):
        score += 18
        evidence.append("non-standard website/booking URL")

    if categories & {"premium_perception_gap"} or flags & PREMIUM_PERCEPTION_FLAGS:
        score += 16
        evidence.append("premium work is undersold by owned site")

    if categories & {"portfolio_presentation_pain"} or flags & PORTFOLIO_FLAGS:
        score += 16
        evidence.append("weak/buried project gallery")

    if categories & {"mobile_usability_pain"} or flags & MOBILE_FLAGS:
        score += 14
        evidence.append("mobile quote path weakness")

    if categories & {"trust_proof_pain"} or flags & TRUST_FLAGS:
        score += 10
        evidence.append("weak trust proof near conversion point")

    if categories & {"service_clarity_pain"} or flags & SERVICE_CLARITY_FLAGS:
        score += 10
        evidence.append("unclear high-ticket service presentation")

    if "no_email_detected" in flags:
        score += 5
        evidence.append("no visible email")

    # Missing form/CTA is low-confidence unless corroborated.
    pages = w.get("pages", {})
    services_ok = bool(pages.get("services", {}).get("ok"))
    generic = bool(w.get("generic_copy_terms"))
    no_email = "no_email_detected" in flags
    structural_status = status in {"Broken Website", "Social Only", "No Website"}
    nonstandard = d.get("non_standard_site_or_booking")
    weak_services = not services_ok
    corroborated = bool(structural_status or nonstandard or generic or no_email or weak_services)

    if "no_form_detected" in flags and not strong_estimate_form:
        points = 8 if corroborated else 2
        score += points
        evidence.append("no form detected" + ("" if corroborated else " (low-confidence)"))

    current_form_strength = form_strength(record).lower()
    if current_form_strength == "weak":
        score += 8
        evidence.append("weak form detected")
    elif current_form_strength == "basic":
        score += 5
        evidence.append("basic form only")

    if "no_clear_cta_detected" in flags and not strong_estimate_form:
        points = 8 if corroborated else 2
        score += points
        evidence.append("weak/no clear CTA" + ("" if corroborated else " (low-confidence)"))

    if weak_services:
        score += 5
        evidence.append("services page weak/missing")

    if not any(t in blob for t in ["service area", "areas we serve", "serving", "county", "cleveland", "akron", "chardon", "northeast ohio"]):
        score += 4
        evidence.append("weak location/service-area clarity")

    if d.get("phone_consistency") == "different":
        score += 8
        evidence.append("phone mismatch across Google/site")

    if w.get("generic_copy_terms"):
        score += 7
        evidence.append("generic/template copy signals")

    homepage_text = w.get("homepage", {}).get("text", "")
    if homepage_text.count("Home") >= 4 or homepage_text.count("More") >= 4:
        score += 4
        evidence.append("clutter/repetitive site copy")

    return min(score, 40), evidence


def score_buyer_intent(record: Dict[str, Any]) -> Tuple[int, List[str]]:
    buyer_intent = ensure_buyer_intent(record)
    stack = buyer_intent.get("crm_booking_stack") or {}
    review_velocity = buyer_intent.get("review_velocity") or {}
    ads = buyer_intent.get("ads") or {}
    detected_tools = stack.get("detected_tools") or []
    if isinstance(detected_tools, str):
        detected_tools = [part.strip() for part in detected_tools.replace(",", ";").split(";") if part.strip()]

    score = 0
    evidence: List[str] = []

    if stack.get("has_paid_crm"):
        score += 8
        evidence.append(f"paid CRM/booking stack detected ({', '.join(str(tool) for tool in detected_tools)})")
    elif detected_tools:
        score += 3
        evidence.append(f"booking/form tools detected ({', '.join(str(tool) for tool in detected_tools)})")

    velocity_band = str(review_velocity.get("velocity_band") or "").strip().lower()
    if velocity_band == "live":
        score += 5
        evidence.append("live review velocity by count/rating")
    elif velocity_band == "steady":
        score += 2
        evidence.append("steady review proof by count/rating")

    yes_ads = [
        field
        for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge")
        if ads.get(field) == "yes"
    ]
    if yes_ads:
        score += 4
        evidence.append(f"ad signal detected ({', '.join(yes_ads)})")

    if buyer_intent.get("money_signal"):
        score += 2
        evidence.append("buyer-intent money signal present")
    else:
        evidence.append("no buyer-intent money signal")

    return min(score, SCORE_WEIGHTS["buyer_intent"]), evidence


def choose_offer_fit(record: Dict[str, Any], strength: int, weakness: int) -> Tuple[str, int, List[str]]:
    w = record.get("website", {})
    d = record.get("derived", {})
    flags = site_issue_flags(record)
    categories = visible_pain_categories(record)
    status = w.get("site_status", "Unknown")

    evidence: List[str] = []

    if status in {"No Website", "Broken Website", "Social Only"}:
        evidence.append("site missing/broken/social-only")
        return "Website Rebuild", 20, evidence

    if d.get("non_standard_site_or_booking"):
        evidence.append("uses booking/social URL instead of full owned site")
        return "Website Rebuild", 20, evidence

    strong_estimate_form = has_strong_estimate_form(record)
    current_form_strength = form_strength(record).lower()
    no_form = "no_form_detected" in flags
    no_cta = "no_clear_cta_detected" in flags
    no_email = "no_email_detected" in flags
    phone_diff = d.get("phone_consistency") == "different"
    estimate_path_weakness = has_estimate_path_weakness(record)

    if categories & {"portfolio_presentation_pain"} or flags & PORTFOLIO_FLAGS:
        evidence.append("project gallery/portfolio presentation gap")
        return "Portfolio Rebuild", 16, evidence

    if categories & {"premium_perception_gap"} or flags & PREMIUM_PERCEPTION_FLAGS:
        evidence.append("premium work is undersold by owned site")
        return "Premium Perception Gap", 16, evidence

    if categories & {"mobile_usability_pain"} or flags & MOBILE_FLAGS:
        evidence.append("mobile quote path needs cleanup")
        return "Mobile Quote Path Cleanup", 15, evidence

    if categories & {"trust_proof_pain"} or flags & TRUST_FLAGS:
        evidence.append("review/trust proof is weak near conversion point")
        return "Review/Trust Proof Upgrade", 12, evidence

    if categories & {"service_clarity_pain"} or flags & SERVICE_CLARITY_FLAGS:
        evidence.append("high-ticket service pages are unclear")
        return "Service Page Cleanup", 12, evidence

    if strong_estimate_form and not (estimate_path_weakness or no_email or phone_diff):
        if weakness >= 18:
            evidence.append("site has cleanup gaps, but a strong estimate form is already present")
            return "Website Cleanup", 10, evidence
        evidence.append("strong estimate form already present")
        return "Advanced Optimization", 4, evidence

    if current_form_strength in {"weak", "basic"} and weakness >= 12:
        evidence.append(f"{current_form_strength} estimate/contact form with other weakness")
        return "Estimate Funnel", 14, evidence

    # Only treat form/CTA as funnel issue if there are multiple signals.
    if not strong_estimate_form and no_form and no_cta and (no_email or weakness >= 18):
        evidence.append("no form, weak CTA, and corroborating weakness")
        return "Estimate Funnel", 18, evidence

    if not strong_estimate_form and no_form and weakness >= 16:
        evidence.append("form/intake gap with other weakness")
        return "Estimate Funnel", 16, evidence

    if strong_estimate_form and estimate_path_weakness and weakness >= 16:
        evidence.append("estimate path has a specific weakness beyond form existence")
        return "Mobile Quote Path Cleanup", 14, evidence

    if phone_diff:
        evidence.append("phone mismatch")
        return "Tracking / Missed Call", 12, evidence

    if weakness >= 18:
        evidence.append("site has multiple conversion/clarity gaps")
        return "Website Cleanup", 14, evidence

    if no_email and weakness >= 10:
        evidence.append("no visible email plus mild site weakness")
        return "Website Cleanup", 10, evidence

    if strength >= 28 and weakness <= 10:
        evidence.append("strong business with mostly solid site")
        return "Advanced Optimization", 6, evidence

    return "Advanced Optimization", 4, ["minor optimization angle only"]


def infer_website_quality(record: Dict[str, Any], weakness: int, offer_fit: str) -> str:
    w = record.get("website", {})
    d = record.get("derived", {})
    status = w.get("site_status", "Unknown")

    if status == "No Website":
        return "N/A"

    if status == "Broken Website" or d.get("non_standard_site_or_booking"):
        return "Poor"

    if offer_fit == "Website Rebuild":
        return "Poor"

    if weakness >= 24:
        return "Poor"

    if offer_fit == "Estimate Funnel" and weakness >= 18:
        return "Poor"

    if weakness >= 12:
        return "Okay"

    if offer_fit == "Advanced Optimization":
        return "Good"

    return "Okay"


def base_priority_from_delta(record: Dict[str, Any], offer_fit: str) -> str:
    status = record.get("website", {}).get("site_status", "Unknown")

    if status in {"No Website", "Broken Website", "Social Only"}:
        return "A"

    if offer_fit == "Website Rebuild":
        return "A"

    if offer_fit == "Estimate Funnel":
        return "B"

    if offer_fit in {"Premium Perception Gap", "Portfolio Rebuild", "Mobile Quote Path Cleanup"}:
        return "B"

    if offer_fit in {
        "Website Cleanup",
        "Tracking / Missed Call",
        "GBP / Reviews",
        "SEO / Service Pages",
        "Service Page Cleanup",
        "Review/Trust Proof Upgrade",
    }:
        return "C"

    if offer_fit == "Advanced Optimization":
        return "C"

    return "Skip"


def classify_priority(
    total: int,
    record: Dict[str, Any],
    offer_fit: str,
    vertical: str,
    *,
    profile: VerticalProfile | None = None,
) -> Tuple[str, str, List[str]]:
    base = base_priority_from_delta(record, offer_fit)
    ceiling, fit_flags = fit_ceiling(record, vertical, profile=profile)

    priority = more_restrictive(base, ceiling)

    # If it scores absurdly low, don't keep it high.
    if total < SCORE_WEIGHTS["priority_thresholds"]["cap_to_c_below"]:
        priority = more_restrictive(priority, "C")

    if total < SCORE_WEIGHTS["priority_thresholds"]["skip_below"]:
        priority = "Skip"

    return priority, ceiling, fit_flags


def best_service_angle(record: Dict[str, Any], vertical: str, offer_fit: str) -> str:
    blob = text_blob(record)
    family = vertical_family(vertical)

    if family == "roofing":
        if has_any(blob, RESTORATION_TERMS):
            return "Residential roof replacement, storm-damage lead capture, quote routing, and owned estimate requests"
        return "Roof replacement, roof repair, high-intent estimate requests, and faster call-to-booking flow"

    if "masonry" in vertical.lower():
        return "Masonry, retaining walls, patios, concrete, brickwork, and high-value exterior project leads"

    if family == "hardscaping":
        if any(t in blob for t in ["outdoor kitchen", "fire pit", "pergola", "outdoor living"]):
            return "High-ticket outdoor living, patios, retaining walls, landscape design, and project estimate requests"
        return "Hardscaping, patios, retaining walls, drainage, landscape design, and estimate requests"

    return "High-intent local service leads, estimate requests, and missed-call recovery"


def make_observation(
    record: Dict[str, Any],
    priority: str,
    offer_fit: str,
    website_quality: str,
    strength_evidence: List[str],
    weakness_evidence: List[str],
    fit_flags: List[str],
    vertical: str,
) -> str:
    g = record.get("google", {})
    w = record.get("website", {})

    name = g.get("business_name", "This business")
    rating = g.get("rating", "")
    reviews = g.get("reviews", "")
    status = w.get("site_status", "Unknown")

    strength = ", ".join(strength_evidence[:4]) or "some local proof"
    weakness = ", ".join(weakness_evidence[:5]) or "limited visible conversion gaps"
    fit = ", ".join(fit_flags[:3]) or "fits basic ICP"

    if priority == "Skip":
        return (
            f"{name} is not a strong current outreach target from the automated evidence. "
            f"Signals: {rating} rating, {reviews} reviews, site status {status}, fit flags: {fit}, and weakness signals: {weakness}. "
            f"Keep only for market mapping unless manual screenshot review shows a much stronger visual gap."
        )

    if offer_fit == "Website Rebuild":
        return (
            f"{name} has credible local demand/proof ({rating} rating, {reviews} reviews; {strength}) but the owned web presence appears materially weaker than the business. "
            f"The key weakness is {weakness}. Fit context: {fit}. This is a strong mockup-first prospect because the visual/conversion delta is obvious: show them a cleaner site and estimate path instead of arguing abstract SEO."
        )

    if offer_fit == "Estimate Funnel":
        return (
            f"{name} looks like a credible {vertical.lower()} company ({rating} rating, {reviews} reviews; {strength}), but the site appears to have intake/conversion gaps. "
            f"The main issues are {weakness}. Fit context: {fit}. This is not just a prettier-site angle; the pitch should be a cleaner estimate funnel with stronger service-specific project intake, trust proof, and faster lead routing."
        )

    if offer_fit == "Tracking / Missed Call":
        return (
            f"{name} has reputation proof ({rating} rating, {reviews} reviews; {strength}), but the strongest automated signal is tracking/routing inconsistency rather than a dramatic website rebuild. "
            f"Detected issues: {weakness}. Fit context: {fit}. This should be treated as a later-stage tracking/missed-call opportunity unless manual review reveals a weak visual funnel."
        )

    if offer_fit == "Website Cleanup":
        return (
            f"{name} has solid proof ({rating} rating, {reviews} reviews; {strength}), but the site shows cleanup or conversion gaps: {weakness}. "
            f"Fit context: {fit}. This is likely a secondary prospect unless screenshots show the site is visibly outdated enough for a strong mockup delta."
        )

    if offer_fit in {
        "Premium Perception Gap",
        "Portfolio Rebuild",
        "Mobile Quote Path Cleanup",
        "Service Page Cleanup",
        "Review/Trust Proof Upgrade",
    }:
        return (
            f"{name} has credible local demand/proof ({rating} rating, {reviews} reviews; {strength}), but the visible web presentation undersells the business: {weakness}. "
            f"Fit context: {fit}. The outreach angle should stay tied to that observable gap, not to a missing-form claim."
        )

    if offer_fit == "Advanced Optimization":
        return (
            f"{name} appears more mature than a basic rebuild prospect: {rating} rating, {reviews} reviews, and {strength}. "
            f"Detected weaknesses are limited or less visually dramatic ({weakness}). Fit context: {fit}. Keep as a later optimization target, not a first-wave mockup prospect."
        )

    return (
        f"{name} has {rating} rating with {reviews} reviews. Strength signals: {strength}. "
        f"Weakness signals: {weakness}. Fit context: {fit}. Manual review needed."
    )


def skip_reason(record: Dict[str, Any], priority: str, total: int, offer_fit: str, fit_flags: List[str]) -> str:
    if priority != "Skip":
        return ""

    if fit_flags:
        return "; ".join(fit_flags)

    if offer_fit == "Advanced Optimization":
        return "Advanced optimization only; no strong mockup-first angle."

    if total < 25:
        return "Low total score; no clear enough offer fit."

    return "Skipped by scoring rules."


def packet_eligible(priority: str, offer_fit: str) -> str:
    if priority in {"A", "B"} and offer_fit in CONCRETE_PACKET_OFFERS:
        return "Yes"
    return ""


def packet_eligibility_reason(priority: str, offer_fit: str) -> str:
    if packet_eligible(priority, offer_fit):
        return "A/B lead with concrete visible-pain offer fit"
    if priority not in {"A", "B"}:
        return "Lead priority below B threshold"
    return "Offer fit not Website Rebuild or Estimate Funnel"


def evidence_id_by_field(record: Dict[str, Any], field: str) -> str:
    evidence = record.get("evidence") or {}
    for item in evidence.get("evidence_items", []) or []:
        if item.get("field") == field and item.get("id"):
            return str(item["id"])
    return ""


def first_issue_evidence_id(record: Dict[str, Any]) -> str:
    evidence = record.get("evidence") or {}
    issue_fields = {
        "site_status",
        "site_issue_flags",
        "contact_form",
        "cta_terms",
        "phone_visible",
        "third_party_tools",
    }
    for item in evidence.get("evidence_items", []) or []:
        if item.get("field") in issue_fields and item.get("id"):
            return str(item["id"])
    return ""


def deterministic_safe_claims(record: Dict[str, Any], offer_fit: str) -> List[Dict[str, Any]]:
    g = record.get("google", {}) or {}
    w = record.get("website", {}) or {}
    evidence = record.get("evidence", {}) or {}
    rating = g.get("rating", "")
    reviews = g.get("reviews", "")
    status = w.get("site_status", "")
    send_trigger = str(evidence.get("send_trigger") or "").strip()

    claims: List[Dict[str, Any]] = []
    demand_ref = evidence_id_by_field(record, "google_rating")
    issue_ref = evidence_id_by_field(record, "send_trigger") or first_issue_evidence_id(record)

    if demand_ref:
        claims.append(
            {
                "claim": f"Your Google profile shows {rating} stars and {reviews} reviews.",
                "evidence_refs": [demand_ref],
            }
        )

    if issue_ref:
        if send_trigger == "No owned website found":
            claim = "There is no owned website or conversion asset visible."
        elif send_trigger == "Website appears broken":
            claim = "The current website appears broken from the collected evidence."
        elif send_trigger == "Social-only web presence":
            claim = "The visible web presence appears to rely on social pages instead of an owned conversion asset."
        elif send_trigger:
            claim = send_trigger
        elif status == "No Website":
            claim = "There is no owned website or conversion asset visible."
        elif status == "Broken Website":
            claim = "The current website appears broken from the collected evidence."
        elif status == "Social Only":
            claim = "The visible web presence appears to rely on social pages instead of an owned conversion asset."
        elif offer_fit == "Estimate Funnel":
            claim = "The estimate path appears harder to find than it should be."
        else:
            claim = "The owned conversion path appears weaker than the business proof."
        claims.append({"claim": claim, "evidence_refs": [issue_ref]})

    return claims


def deterministic_opener(record: Dict[str, Any], offer_fit: str) -> str:
    g = record.get("google", {}) or {}
    business = g.get("business_name", "your business")
    rating = g.get("rating", "")
    reviews = g.get("reviews", "")
    status = (record.get("website", {}) or {}).get("site_status", "")
    send_trigger = str((record.get("evidence", {}) or {}).get("send_trigger") or "").strip()

    if status == "No Website":
        return (
            f"Your Google profile looks strong ({rating} stars / {reviews} reviews), "
            "but I couldn't find an owned website or estimate path. Want me to send a cleaner direction?"
        )
    if send_trigger:
        return (
            f"{business} looks credible from the outside ({rating} stars / {reviews} reviews), "
            f"but I noticed this specific web issue: {send_trigger.lower()}. Want me to send a cleaner version?"
        )
    if offer_fit == "Estimate Funnel":
        return (
            f"{business} looks credible from the outside ({rating} stars / {reviews} reviews), "
            "but the estimate path looks harder to find than it should be. Want me to send a cleaner version?"
        )
    return (
        f"{business} looks credible from the outside ({rating} stars / {reviews} reviews), "
        "but the online conversion path looks weaker than the business proof. Want me to send what I noticed?"
    )


def review_fit_band(record: Dict[str, Any], vertical: str) -> str:
    reviews = safe_int(record.get("google", {}).get("reviews"))

    if reviews >= 75:
        return "High 75+ needs very clear pain"
    if 21 <= reviews <= 75:
        return "Allowed 21-75 needs clear pain"
    if 5 <= reviews <= 20:
        return "Ideal 5-20"
    if reviews < 5:
        return "Very thin 0-4"
    return "Out of band"


def score_record(record: Dict[str, Any], fallback_vertical: str) -> Dict[str, Any]:
    record = sanitize_record_emails(record)
    record = ensure_evidence(record)
    record = sanitize_record_emails(record)
    vertical = infer_vertical(record, fallback_vertical)
    profile = resolve_profile(_record_vertical(record) or vertical, source_query=_record_source_query(record))

    raw_strength, strength_evidence = score_business_strength(record, vertical, profile=profile)
    raw_weakness, weakness_evidence = score_online_weakness(record)
    buyer_intent_score, buyer_intent_evidence = score_buyer_intent(record)
    offer_fit, raw_offer_score, offer_evidence = choose_offer_fit(record, raw_strength, raw_weakness)

    strength = weighted_score(
        raw_strength,
        SCORE_WEIGHTS["raw_business_strength"],
        SCORE_WEIGHTS["business_strength"],
    )
    weakness = weighted_score(
        raw_weakness,
        SCORE_WEIGHTS["raw_online_weakness"],
        SCORE_WEIGHTS["online_weakness"],
    )
    offer_score = weighted_score(
        raw_offer_score,
        SCORE_WEIGHTS["raw_offer_fit"],
        SCORE_WEIGHTS["offer_fit"],
    )
    total = min(100, strength + weakness + offer_score + buyer_intent_score)
    priority, fit_cap, fit_flags = classify_priority(total, record, offer_fit, vertical, profile=profile)
    website_quality = infer_website_quality(record, raw_weakness, offer_fit)
    angle = best_service_angle(record, vertical, offer_fit)

    observation = make_observation(
        record,
        priority,
        offer_fit,
        website_quality,
        strength_evidence,
        weakness_evidence + offer_evidence,
        fit_flags,
        vertical,
    )

    g = record.get("google", {})
    w = record.get("website", {})
    screenshots = record.get("screenshots", {})

    evidence_profile = record.get("evidence", {}) or {}
    buyer_summary = buyer_intent_summary(record)
    money_signal = "Yes" if has_money_signal(record) else "No"
    visible_pain_reasons = evidence_profile.get("visible_pain_reasons") or []
    visible_pain_reasons_text = "; ".join(str(reason) for reason in visible_pain_reasons if str(reason).strip())
    send_trigger = str(evidence_profile.get("send_trigger") or "").strip()
    visible_pain_score = evidence_profile.get("visible_pain_score", 0)
    visual_pain_reasons = evidence_profile.get("visual_pain_reasons") or []
    visual_pain_reasons_text = "; ".join(str(reason) for reason in visual_pain_reasons if str(reason).strip())
    rendered_site_status = str(evidence_profile.get("rendered_site_status") or "").strip()
    rendered_status_reason = str(evidence_profile.get("rendered_status_reason") or "").strip()
    desktop_screenshot_path = (
        str(evidence_profile.get("desktop_screenshot_path") or "").strip() or screenshots.get("desktop", "")
    )
    mobile_screenshot_path = (
        str(evidence_profile.get("mobile_screenshot_path") or "").strip() or screenshots.get("mobile", "")
    )

    evidence_notes = "; ".join(
        [
            f"Strength {strength}/{SCORE_WEIGHTS['business_strength']}: {', '.join(strength_evidence) or 'none'}",
            f"Weakness {weakness}/{SCORE_WEIGHTS['online_weakness']}: {', '.join(weakness_evidence) or 'none'}",
            f"Offer {offer_score}/20: {offer_fit} ({', '.join(offer_evidence) or 'none'})",
            f"Buyer intent {buyer_intent_score}/{SCORE_WEIGHTS['buyer_intent']}: {', '.join(buyer_intent_evidence) or 'none'}",
            f"Fit cap: {fit_cap} ({', '.join(fit_flags) or 'none'})",
            f"Visible pain {visible_pain_score}: {visible_pain_reasons_text or 'none'}",
        ]
    )

    safe_claims = deterministic_safe_claims(record, offer_fit)
    evidence_refs: List[str] = []
    for claim in safe_claims:
        for ref in claim.get("evidence_refs") or []:
            if ref not in evidence_refs:
                evidence_refs.append(ref)

    row = {
        "Business Name": g.get("business_name", ""),
        "Owner Name": "",
        "Vertical": vertical,
        "City": g.get("city", ""),
        "Phone": g.get("phone", ""),
        "Email": emails_text(
            w.get("emails_found", []),
            business_domain=w.get("domain") or w.get("final_url") or g.get("website_url", ""),
        ),
        "Website URL": g.get("website_url", ""),
        "Google Maps URL": g.get("google_maps_url", ""),
        "Reviews": g.get("reviews", ""),
        "Rating": g.get("rating", ""),
        "Current Website Quality": website_quality,
        "Best Service Angle": angle,
        "Specific Observation": observation,
        "Mockup URL": "",
        "Video URL": "",
        "Outreach Channel": "Google Maps",
        "Status": "New",
        "Last Contacted": "",
        "Next Follow-Up": "",
        "Lead Priority": priority,
        "Offer Fit": offer_fit,
        "Site Status": w.get("site_status", ""),
        "Contact Form Detected": form_status(record, "contact_form_detected", "Unknown"),
        "Estimate Form Detected": form_status(record, "estimate_form_detected", "Unknown"),
        "Form Strength": form_status(record, "form_strength", "None"),
        "Form Detection Method": form_status(record, "form_detection_method", "none"),
        "CTA Strength": form_status(record, "cta_strength", "None"),
        "Rendered Site Status": rendered_site_status,
        "rendered_site_status": rendered_site_status,
        "Rendered Status Reason": rendered_status_reason,
        "rendered_status_reason": rendered_status_reason,
        "Desktop Screenshot Path": desktop_screenshot_path,
        "desktop_screenshot_path": desktop_screenshot_path,
        "Mobile Screenshot Path": mobile_screenshot_path,
        "mobile_screenshot_path": mobile_screenshot_path,
        "Visual Review Needed": evidence_profile.get("visual_review_needed", ""),
        "visual_review_needed": evidence_profile.get("visual_review_needed", ""),
        "Visual Quality Score": evidence_profile.get("visual_quality_score", ""),
        "visual_quality_score": evidence_profile.get("visual_quality_score", ""),
        "Premium Perception Score": evidence_profile.get("premium_perception_score", ""),
        "premium_perception_score": evidence_profile.get("premium_perception_score", ""),
        "Mobile Visual Score": evidence_profile.get("mobile_visual_score", ""),
        "mobile_visual_score": evidence_profile.get("mobile_visual_score", ""),
        "Hero Clarity Score": evidence_profile.get("hero_clarity_score", ""),
        "hero_clarity_score": evidence_profile.get("hero_clarity_score", ""),
        "CTA Visibility Score": evidence_profile.get("cta_visibility_score", ""),
        "cta_visibility_score": evidence_profile.get("cta_visibility_score", ""),
        "Gallery Visibility Score": evidence_profile.get("gallery_visibility_score", ""),
        "gallery_visibility_score": evidence_profile.get("gallery_visibility_score", ""),
        "Trust Proof Visibility Score": evidence_profile.get("trust_proof_visibility_score", ""),
        "trust_proof_visibility_score": evidence_profile.get("trust_proof_visibility_score", ""),
        "Visual Pain Reasons": visual_pain_reasons_text,
        "visual_pain_reasons": visual_pain_reasons,
        "Visible Pain Score": visible_pain_score,
        "visible_pain_score": visible_pain_score,
        "Visible Pain Reasons": visible_pain_reasons_text,
        "visible_pain_reasons": visible_pain_reasons,
        "Send Trigger": send_trigger,
        "send_trigger": send_trigger,
        "Money Signal": money_signal,
        "money_signal": money_signal,
        "Buyer Intent Signals": buyer_summary,
        "buyer_intent_signals": buyer_summary,
        "Evidence Notes": evidence_notes,
        "AI Confidence": "medium",
        "Manual Reviewed": "",
        "Skip Reason": skip_reason(record, priority, total, offer_fit, fit_flags),
        "Raw Evidence": json.dumps(record, ensure_ascii=False),
        "Score Total": total,
        "Score Strength": strength,
        "Score Weakness": weakness,
        "Score Offer": offer_score,
        "Score Buyer Intent": buyer_intent_score,
        "Fit Ceiling": fit_cap,
        "Fit Flags": ", ".join(fit_flags),
        "Review Fit Band": review_fit_band(record, vertical),
        "Opener": deterministic_opener(record, offer_fit),
        "Evidence Refs": json.dumps(evidence_refs, ensure_ascii=False),
        "Safe Outreach Claims": json.dumps(safe_claims, ensure_ascii=False),
        "Unsafe Claims": json.dumps(
            [
                "Do not claim they are losing jobs.",
                "Do not claim a form is broken unless the evidence verifies it.",
            ],
            ensure_ascii=False,
        ),
        "Packet Eligible": packet_eligible(priority, offer_fit),
        "Packet Eligibility Reason": packet_eligibility_reason(priority, offer_fit),
    }
    return apply_sendability(row)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames = list(rows[0].keys())

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


CORE_EVIDENCE_FIELDS = ("visible_pain_score", "visible_pain_reasons", "send_trigger")
RENDERED_EVIDENCE_FIELDS = (
    "rendered_site_status",
    "rendered_status_reason",
    "desktop_screenshot_path",
    "mobile_screenshot_path",
    "visual_review_needed",
    "visual_quality_score",
    "premium_perception_score",
    "mobile_visual_score",
    "hero_clarity_score",
    "cta_visibility_score",
    "gallery_visibility_score",
    "trust_proof_visibility_score",
    "visual_pain_reasons",
    "screenshots",
)


def _with_missing_rendered_evidence(record: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    if all(key in evidence for key in RENDERED_EVIDENCE_FIELDS):
        return record
    rebuilt = build_evidence_profile(record)
    merged = dict(evidence)
    for key in RENDERED_EVIDENCE_FIELDS:
        if key not in merged and key in rebuilt:
            merged[key] = rebuilt[key]
    updated = dict(record)
    updated["evidence"] = merged
    return updated


def ensure_evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    evidence = record.get("evidence")
    ensure_buyer_intent(record)
    if isinstance(evidence, dict) and all(key in evidence for key in CORE_EVIDENCE_FIELDS):
        return _with_missing_rendered_evidence(record, evidence)
    updated = dict(record)
    updated["evidence"] = build_evidence_profile(record)
    ensure_buyer_intent(updated)
    return updated


def evidence_ids(record: Dict[str, Any]) -> set[str]:
    evidence = record.get("evidence") or {}
    ids = {str(item.get("id")) for item in evidence.get("evidence_items", []) if item.get("id")}
    for option in _build_evidence_ref_options(record):
        option_id = option.get("id")
        if option_id:
            ids.add(str(option_id))
    return ids


def lead_id_for_record(record: Dict[str, Any], *, fallback_vertical: str) -> str:
    google = record.get("google", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    return lead.place_id_or_unique_key or score_file_for_record(record, fallback_vertical=fallback_vertical)


def compact_evidence_ref_ids(compact_record: Dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for option in compact_record.get("evidence_ref_options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "").strip()
        if option_id:
            refs.add(option_id)
    return refs


def _score_ref_debug_payload(
    *,
    lead_id: str,
    score_path: Path,
    allowed_refs: set[str],
) -> Dict[str, Any]:
    score_refs: List[str] = []
    safe_claim_refs: List[str] = []
    try:
        score_data = json.loads(score_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        score_data = {}
    if isinstance(score_data, dict):
        score_refs = normalize_evidence_refs(score_data.get("evidence_refs"))
        for claim in score_data.get("safe_outreach_claims") or []:
            if isinstance(claim, dict):
                safe_claim_refs.extend(normalize_evidence_refs(claim.get("evidence_refs")))
    offending_refs = sorted((set(score_refs) | set(safe_claim_refs)) - allowed_refs)
    return {
        "lead_id": lead_id,
        "score_path": str(score_path),
        "offending_evidence_refs": offending_refs,
        "evidence_refs": score_refs,
        "safe_outreach_claim_evidence_refs": sorted(set(safe_claim_refs)),
        "allowed_refs": sorted(allowed_refs),
    }


def _write_api_merge_validation_debug(
    bundle_dir: Path,
    *,
    lead_id: str,
    score_path: Path,
    allowed_refs: set[str],
    error: Exception,
) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    debug_path = bundle_dir / f"merge_validation_failed_{safe_slug(lead_id)}.json"
    payload = _score_ref_debug_payload(lead_id=lead_id, score_path=score_path, allowed_refs=allowed_refs)
    payload["error"] = str(error)
    debug_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return debug_path


def manual_score_candidates(record: Dict[str, Any], scores_dir: Path, fallback_vertical: str) -> Tuple[str, List[Path]]:
    google = record.get("google", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    candidates: List[str] = []
    for candidate in [safe_slug(lead.place_id_or_unique_key), safe_slug(lead.business_name)]:
        if candidate not in candidates:
            candidates.append(candidate)

    lead_name = lead.business_name or lead.place_id_or_unique_key or "lead"
    return lead_name, [scores_dir / f"{candidate}.json" for candidate in candidates]


def missing_manual_score_message(lead_name: str, expected_paths: List[Path]) -> str:
    return (
        f"Missing manual score JSON for {lead_name}. "
        f"Expected one of: {', '.join(str(path) for path in expected_paths)}"
    )


def manual_score_path(record: Dict[str, Any], scores_dir: Path, fallback_vertical: str) -> Path:
    lead_name, expected_paths = manual_score_candidates(record, scores_dir, fallback_vertical)
    for path in expected_paths:
        if path.exists():
            return path
    raise FileNotFoundError(missing_manual_score_message(lead_name, expected_paths))


def score_records_manual_run(
    records: List[Dict[str, Any]],
    *,
    scores_dir: Path,
    fallback_vertical: str,
    only_scored: bool = False,
    api_evidence_ref_ids_by_lead_id: Dict[str, set[str]] | None = None,
    api_merge_debug_dir: Path | None = None,
) -> ManualScoreRun:
    scored: List[Dict[str, Any]] = []
    missing_scores: List[MissingManualScore] = []
    has_known_evidence_ids = False
    for record in records:
        record = ensure_evidence(record)
        lead_name, expected_paths = manual_score_candidates(record, scores_dir, fallback_vertical)
        score_path = next((path for path in expected_paths if path.exists()), None)
        if score_path is None:
            if only_scored:
                missing_scores.append(MissingManualScore(lead_name=lead_name, expected_paths=expected_paths))
                continue
            raise FileNotFoundError(missing_manual_score_message(lead_name, expected_paths))

        lead_id = lead_id_for_record(record, fallback_vertical=fallback_vertical)
        api_refs = set((api_evidence_ref_ids_by_lead_id or {}).get(lead_id) or set())
        allowed_for_api_merge = original_evidence_ids(record) | api_refs
        allowed_for_load = evidence_ids(record) | api_refs
        try:
            score = load_manual_score(score_path, allowed_evidence_ids=allowed_for_load)
            row = merge_manual_score(
                record,
                score,
                fallback_vertical=fallback_vertical,
                allowed_extra_evidence_ids=api_refs,
            )
        except ValueError as exc:
            if api_merge_debug_dir is not None:
                debug_path = _write_api_merge_validation_debug(
                    api_merge_debug_dir,
                    lead_id=lead_id,
                    score_path=score_path,
                    allowed_refs=allowed_for_api_merge,
                    error=exc,
                )
                raise ValueError(f"{exc}; API merge validation debug written to {debug_path}") from exc
            raise
        if "Known Evidence IDs" in row or "known_evidence_ids" in row:
            has_known_evidence_ids = True
        scored.append(row)

    if has_known_evidence_ids:
        for row in scored:
            row.setdefault("Known Evidence IDs", "")
            row.setdefault("known_evidence_ids", [])

    return ManualScoreRun(rows=scored, missing_scores=missing_scores)


def score_records_manual(
    records: List[Dict[str, Any]],
    *,
    scores_dir: Path,
    fallback_vertical: str,
    only_scored: bool = False,
) -> List[Dict[str, Any]]:
    return score_records_manual_run(
        records,
        scores_dir=scores_dir,
        fallback_vertical=fallback_vertical,
        only_scored=only_scored,
    ).rows


def estimate_llm_usd(usage: Dict[str, Any]) -> float:
    input_tokens = safe_int(usage.get("input_tokens"))
    output_tokens = safe_int(usage.get("output_tokens"))
    return (
        input_tokens * SONNET_4_6_INPUT_USD_PER_MILLION_TOKENS
        + output_tokens * SONNET_4_6_OUTPUT_USD_PER_MILLION_TOKENS
    ) / 1_000_000


def estimate_prompt_tokens(prompt: str) -> int:
    if not prompt:
        return 0
    return max(1, (len(prompt) + 3) // 4)


def _truncate_for_llm(value: Any, *, max_chars: int = MAX_LLM_SCORING_STRING_CHARS) -> Any:
    if isinstance(value, str):
        cleaned = re.sub(r"\s+", " ", value).strip()
        if len(cleaned) <= max_chars:
            return cleaned
        if max_chars <= 3:
            return cleaned[:max_chars]
        return cleaned[: max_chars - 3].rstrip() + "..."
    if isinstance(value, list):
        return [_truncate_for_llm(item, max_chars=max_chars) for item in value]
    if isinstance(value, dict):
        return {str(key): _truncate_for_llm(item, max_chars=max_chars) for key, item in value.items()}
    return value


def _text_sources_for_llm(record: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    homepage = website.get("homepage", {}) if isinstance(website.get("homepage"), dict) else {}
    pages = website.get("pages", {}) if isinstance(website.get("pages"), dict) else {}
    sources: List[Tuple[str, str, str]] = []

    sources.append(("website.combined_text", str(website.get("final_url") or homepage.get("url") or ""), str(website.get("combined_text") or "")))
    sources.append(("website.homepage.text", str(homepage.get("url") or ""), str(homepage.get("text") or "")))
    for page_name, page in pages.items():
        if not isinstance(page, dict):
            continue
        sources.append((f"website.pages.{page_name}.text", str(page.get("url") or ""), str(page.get("text") or "")))
    return sources


def _snippet_around(text: str, start: int, *, max_chars: int = MAX_LLM_SCORING_SNIPPET_CHARS) -> str:
    sentence_left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start))
    sentence_right_candidates = [pos for pos in (text.find(".", start), text.find("!", start), text.find("?", start), text.find("\n", start)) if pos >= 0]
    if sentence_right_candidates:
        sentence_right = min(sentence_right_candidates) + 1
        sentence_left = sentence_left + 1 if sentence_left >= 0 else 0
        sentence = text[sentence_left:sentence_right].strip()
        if sentence and len(sentence) <= max_chars:
            return _truncate_for_llm(sentence, max_chars=max_chars)

    left = max(0, start - max_chars // 2)
    right = min(len(text), left + max_chars)
    left = max(0, right - max_chars)
    snippet = text[left:right].strip()
    if left > 0:
        snippet = "..." + snippet
    if right < len(text):
        snippet = snippet + "..."
    return _truncate_for_llm(snippet, max_chars=max_chars)


def _selected_text_snippets(record: Dict[str, Any], *, max_snippets: int = MAX_LLM_SCORING_SNIPPETS) -> List[Dict[str, str]]:
    snippets: List[Dict[str, str]] = []
    seen: set[str] = set()
    for source, source_url, text in _text_sources_for_llm(record):
        if len(snippets) >= max_snippets:
            break
        if not text:
            continue
        lowered = text.lower()
        for term in LLM_SCORING_SNIPPET_TERMS:
            if len(snippets) >= max_snippets:
                break
            index = lowered.find(term)
            if index < 0:
                continue
            snippet = _snippet_around(text, index)
            fingerprint = re.sub(r"\W+", "", snippet.lower())
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            snippets.append(
                {
                    "kind": "page_text",
                    "source": source,
                    "source_url": source_url,
                    "matched_term": term,
                    "text": snippet,
                }
            )
    return snippets


def _selected_evidence_item_snippets(record: Dict[str, Any], *, max_snippets: int) -> List[Dict[str, Any]]:
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    selected: List[Dict[str, Any]] = []
    for item in evidence.get("evidence_items", []) or []:
        if len(selected) >= max_snippets:
            break
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        value_text = json.dumps(item.get("value"), ensure_ascii=False) if isinstance(item.get("value"), (dict, list)) else str(item.get("value") or "")
        if field not in LLM_SCORING_EVIDENCE_FIELDS and not any(term in value_text.lower() for term in LLM_SCORING_SNIPPET_TERMS):
            continue
        selected.append(
            {
                "kind": "evidence_item",
                "id": str(item.get("id") or ""),
                "field": field,
                "value": _truncate_for_llm(item.get("value"), max_chars=MAX_LLM_SCORING_SNIPPET_CHARS),
                "source_url": _truncate_for_llm(str(item.get("source_url") or ""), max_chars=MAX_LLM_SCORING_SNIPPET_CHARS),
                "confidence": str(item.get("confidence") or ""),
            }
        )
    return selected


def _visual_review_capsule(evidence: Dict[str, Any], website: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "visual_review_needed": evidence.get("visual_review_needed") or website.get("visual_review_needed") or "",
        "visual_quality_score": evidence.get("visual_quality_score") or website.get("visual_quality_score") or "",
        "premium_perception_score": evidence.get("premium_perception_score") or website.get("premium_perception_score") or "",
        "mobile_visual_score": evidence.get("mobile_visual_score") or website.get("mobile_visual_score") or "",
        "hero_clarity_score": evidence.get("hero_clarity_score") or website.get("hero_clarity_score") or "",
        "cta_visibility_score": evidence.get("cta_visibility_score") or website.get("cta_visibility_score") or "",
        "gallery_visibility_score": evidence.get("gallery_visibility_score") or website.get("gallery_visibility_score") or "",
        "trust_proof_visibility_score": evidence.get("trust_proof_visibility_score") or website.get("trust_proof_visibility_score") or "",
        "visual_pain_reasons": evidence.get("visual_pain_reasons") or website.get("visual_pain_reasons") or [],
    }


def _short_snippet(value: Any, *, max_chars: int = 140) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    return _truncate_for_llm(text, max_chars=max_chars)


def _append_evidence_ref_option(
    options: List[Dict[str, str]],
    seen_ids: set[str],
    *,
    option_id: str,
    label: str,
    snippet: Any = "",
    source: str = "",
) -> None:
    cleaned_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(option_id or "").strip()).strip("_")
    if not cleaned_id or cleaned_id in seen_ids:
        return
    option = {
        "id": cleaned_id,
        "label": _short_snippet(label, max_chars=80),
    }
    snippet_text = _short_snippet(snippet)
    if snippet_text:
        option["snippet"] = snippet_text
    source_text = _short_snippet(source, max_chars=120)
    if source_text:
        option["source"] = source_text
    options.append(option)
    seen_ids.add(cleaned_id)


def _build_evidence_ref_options(record: Dict[str, Any], *, lead: Any | None = None) -> List[Dict[str, str]]:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    buyer_intent = record.get("buyer_intent", {}) if isinstance(record.get("buyer_intent"), dict) else {}
    options: List[Dict[str, str]] = []
    seen_ids: set[str] = set()

    rating = google.get("rating") or google.get("google_rating") or (getattr(lead, "google_rating", "") if lead else "")
    reviews = google.get("reviews") or google.get("google_review_count") or (getattr(lead, "google_review_count", "") if lead else "")
    google_bits = []
    if rating not in ("", None):
        google_bits.append(f"{rating} rating")
    if reviews not in ("", None):
        google_bits.append(f"{reviews} reviews")
    if not google_bits and google.get("business_name"):
        google_bits.append(str(google.get("business_name")))
    _append_evidence_ref_option(
        options,
        seen_ids,
        option_id="google_profile",
        label="Google rating/reviews",
        snippet=", ".join(google_bits),
        source=google.get("google_maps_url") or google.get("maps_url") or "google",
    )

    site_status = evidence.get("site_status") or website.get("site_status") or ""
    rendered_status = evidence.get("rendered_site_status") or website.get("rendered_site_status") or ""
    website_url = google.get("website_url") or google.get("website") or website.get("final_url") or ""
    if site_status or rendered_status or website_url:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="website_status",
            label="Website detected",
            snippet=", ".join(part for part in [str(site_status or "").strip(), str(rendered_status or "").strip()] if part),
            source=website_url or "evidence.website_status",
        )

    services = website.get("high_ticket_terms") or evidence.get("services_detected") or []
    if services:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="homepage_services",
            label="High-ticket services",
            snippet=" / ".join(str(item) for item in services[:6]),
            source=website.get("final_url") or "website.high_ticket_terms",
        )

    form_bits = []
    for label, value in (
        ("contact_form", evidence.get("contact_form_detected") or website.get("contact_form_detected")),
        ("estimate_form", evidence.get("estimate_form_detected") or website.get("estimate_form_detected")),
        ("form_strength", evidence.get("form_strength") or website.get("form_strength")),
        ("cta_strength", evidence.get("cta_strength") or website.get("cta_strength")),
    ):
        if value not in ("", None, [], {}):
            form_bits.append(f"{label}={value}")
    if form_bits:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="form_detection",
            label="Form/booking evidence",
            snippet=", ".join(form_bits),
            source=evidence.get("form_detection_method") or website.get("form_detection_method") or "evidence.forms",
        )

    buyer_summary = buyer_intent_summary(record) if buyer_intent else ""
    if buyer_summary:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="buyer_intent",
            label="Buyer intent evidence",
            snippet=buyer_summary,
            source="buyer_intent",
        )

    source_query = google.get("source_query") or google.get("search_query") or google.get("category") or ""
    if source_query:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="icp_source_query",
            label="ICP source query",
            snippet=source_query,
            source="google.source_query",
        )

    for item in evidence.get("evidence_items", []) or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id=item_id,
            label=str(item.get("field") or "Evidence item"),
            snippet=item.get("value"),
            source=item.get("source_url") or "evidence.evidence_items",
        )

    if not options:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="google_profile",
            label="Google profile",
            snippet=google.get("business_name") or source_query or "Lead source record",
            source="google",
        )
    return options


def build_llm_scoring_record(record: Dict[str, Any], *, fallback_vertical: str = "service") -> Dict[str, Any]:
    working = ensure_evidence(copy.deepcopy(record))
    google = working.get("google", {}) if isinstance(working.get("google"), dict) else {}
    website = working.get("website", {}) if isinstance(working.get("website"), dict) else {}
    evidence = working.get("evidence", {}) if isinstance(working.get("evidence"), dict) else {}
    buyer_intent = working.get("buyer_intent", {}) if isinstance(working.get("buyer_intent"), dict) else {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    screenshots = working.get("screenshots") or website.get("screenshots") or evidence.get("screenshots") or {}
    selected_text = _selected_text_snippets(working)
    selected_items = _selected_evidence_item_snippets(working, max_snippets=max(0, MAX_LLM_SCORING_SNIPPETS - len(selected_text)))

    capsule = {
        "score_file": score_file_for_record(working, fallback_vertical=fallback_vertical),
        "lead": {
            "business_name": lead.business_name,
            "vertical": lead.vertical,
            "category": google.get("category", ""),
            "city": lead.city,
            "address": google.get("address", ""),
            "phone": lead.phone,
            "rating": lead.google_rating,
            "reviews": lead.google_review_count,
            "website_url": lead.website,
            "google_maps_url": lead.maps_url,
        },
        "website_status": {
            "site_status": evidence.get("site_status") or website.get("site_status") or "",
            "rendered_site_status": evidence.get("rendered_site_status") or website.get("rendered_site_status") or "",
            "rendered_status_reason": evidence.get("rendered_status_reason") or website.get("rendered_status_reason") or "",
            "raw_flags": evidence.get("raw_flags") or website.get("site_issue_flags") or [],
        },
        "conversion_pain": {
            "current_website_quality": website.get("quality") or website.get("site_quality") or "",
            "visible_pain_score": evidence.get("visible_pain_score", ""),
            "visible_pain_reasons": evidence.get("visible_pain_reasons") or [],
            "visible_pain_categories": evidence.get("visible_pain_categories") or [],
            "send_trigger": evidence.get("send_trigger") or "",
            "cta_strength": evidence.get("cta_strength") or website.get("cta_strength") or "",
        },
        "forms_and_cta": {
            "contact_form_detected": evidence.get("contact_form_detected") or website.get("contact_form_detected") or "",
            "estimate_form_detected": evidence.get("estimate_form_detected") or website.get("estimate_form_detected") or "",
            "form_strength": evidence.get("form_strength") or website.get("form_strength") or "",
            "form_detection_method": evidence.get("form_detection_method") or website.get("form_detection_method") or "",
            "has_estimate_cta": evidence.get("has_estimate_cta", ""),
            "cta_terms": website.get("cta_terms") or [],
        },
        "provider_evidence": {
            "third_party_tools": evidence.get("third_party_tools") or website.get("third_party_tools") or [],
            "has_online_booking": evidence.get("has_online_booking", ""),
            "crm_booking_stack": buyer_intent.get("crm_booking_stack") or {},
        },
        "service_evidence": {
            "high_ticket_terms": website.get("high_ticket_terms") or evidence.get("services_detected") or [],
            "services_detected": evidence.get("services_detected") or [],
            "trust_signals": evidence.get("trust_signals") or [],
        },
        "buyer_intent": buyer_intent,
        "buyer_intent_summary": buyer_intent_summary(working),
        "money_signal": "Yes" if has_money_signal(working) else "No",
        "safe_outreach_claims": working.get("safe_outreach_claims") or evidence.get("safe_outreach_claims") or [],
        "sendability_evidence": {
            "minimum_evidence_inputs": {
                "visible_pain_score": evidence.get("visible_pain_score", ""),
                "visible_pain_reasons": evidence.get("visible_pain_reasons") or [],
                "send_trigger": evidence.get("send_trigger") or "",
                "money_signal": "Yes" if has_money_signal(working) else "No",
                "buyer_intent_summary": buyer_intent_summary(working),
            }
        },
        "screenshots": {
            "desktop": screenshots.get("desktop") or website.get("desktop_screenshot_path") or evidence.get("desktop_screenshot_path") or "",
            "mobile": screenshots.get("mobile") or website.get("mobile_screenshot_path") or evidence.get("mobile_screenshot_path") or "",
        },
        "visual_review": _visual_review_capsule(evidence, website),
        "selected_evidence_snippets": [*selected_text, *selected_items][:MAX_LLM_SCORING_SNIPPETS],
        "evidence_ref_options": _build_evidence_ref_options(working, lead=lead),
    }
    return _truncate_for_llm(capsule)


def build_api_llm_scoring_prompt(compact_records: List[Dict[str, Any]], *, batch_id: str) -> str:
    response_example = {
        "batch_id": batch_id,
        "scores": [
            {
                "score_file": compact_records[0]["score_file"] if compact_records else "phone-xxxxxxxxxx.json",
                "score": MANUAL_SCORE_SCHEMA,
            }
        ],
    }

    return f"""# API LLM Lead Scoring - {batch_id}

You are scoring compact lead evidence capsules for local-service outreach.

Important evidence boundary:
- Each compact record is a distilled evidence capsule built from the enriched JSONL.
- The full source evidence remains in the enriched file for packets, debugging, manual review, and future asset generation.
- Score only from the capsule below.
- Do not invent claims, visual observations, forms, service lines, money intent, or conversion pain that the capsule does not support.
- Preserve exact snippets when using them for safe outreach wording.

JSON-only response requirements:
- Response must be one valid JSON object only.
- First character must be {{ and last character must be }}.
- No markdown fences.
- No commentary.
- No reasoning outside JSON.
- Use one scores item per compact record.
- Every score_file must exactly match a score_file listed in compact_records.

Shared rubric:
- ICP Fit / 20
- Reputation + Demand Proof / 15
- Web Conversion Pain / 25
- Mockup Leverage / 15
- Reachability / 10
- Offer Angle Clarity / 10
- Evidence Confidence / 5

Rules:
- Score and confidence are separate.
- total_score must equal the sum of all subscores.
- grade rules: A = 80-100, B = 65-79, C = 50-64, Skip = below 50 or hard disqualifier.
- confidence must be one of low, medium, high.
- safe_outreach_claims must be evidence-backed and safe to say.
- safe_outreach_claims must be an array of objects with claim and evidence_refs.
- evidence_refs is REQUIRED for every score.
- evidence_refs must be a non-empty array of 1-3 strings; never output [] for evidence_refs.
- Each evidence_refs value must be copied exactly from that lead's evidence_ref_options[].id.
- If a lead is low quality, C, or Skip, still include evidence_refs explaining why.
- safe_outreach_claims[].evidence_refs must also use IDs copied exactly from that lead's evidence_ref_options[].id.
- If an exact text snippet supports a claim but no evidence_ref_options ID directly supports it, lower evidence confidence instead of inventing an evidence ref.
- unsafe_claims must list tempting claims that the capsule does not prove.
- Buyer intent is required for A/B quality: look for fixable gap times money signal, not just a bad website.
- Bad site alone without buyer_intent.money_signal should not be an A lead.
- Visual claims require screenshot paths, visual-review flags, rendered browser evidence, DOM evidence, or explicit manual review notes in the capsule.
- Do not infer missing forms if evidence is uncertain.
- If rendered_site_status is Loaded, do not claim the website is broken.
- If a strong estimate form exists, do not use missing-form, no-contact-path, or no-estimate-path pain unless the capsule also shows another concrete estimate-path weakness.
- opener must mention at least one specific evidence-backed observation and must not make unsupported money, competitor, or lost-job claims.

Allowed best_angle values:
- Website Rebuild
- Estimate Funnel
- Premium Perception Gap
- Portfolio Rebuild
- Mobile Quote Path Cleanup
- Service Page Cleanup
- Review/Trust Proof Upgrade
- Website Cleanup
- Tracking
- Advanced Optimization
- No-Site Launch
- Social/Jobber Escape

Manual score schema for each scores[].score:
{json.dumps(MANUAL_SCORE_SCHEMA, ensure_ascii=False, indent=2)}

Required response format:
{json.dumps(response_example, ensure_ascii=False, indent=2)}

compact_records:
{json.dumps(compact_records, ensure_ascii=False, indent=2)}
"""


def _hit_llm_cap(
    *,
    calls: int,
    tokens: int,
    usd: float,
    max_calls: int,
    max_tokens_per_run: int,
    max_usd_per_run: float,
) -> str:
    if max_calls > 0 and calls >= max_calls:
        return "MAX_LLM_CALLS_PER_RUN"
    if max_tokens_per_run > 0 and tokens >= max_tokens_per_run:
        return "MAX_LLM_TOKENS_PER_RUN"
    if max_usd_per_run > 0 and usd >= max_usd_per_run:
        return "MAX_LLM_USD_PER_RUN"
    return ""


def _hit_projected_llm_cap(
    *,
    prompt: str,
    max_tokens: int,
    tokens: int,
    usd: float,
    max_tokens_per_run: int,
    max_usd_per_run: float,
) -> str:
    projected_input_tokens = estimate_prompt_tokens(prompt)
    projected_output_tokens = max(0, max_tokens)
    projected_total_tokens = projected_input_tokens + projected_output_tokens

    if max_tokens_per_run > 0 and tokens + projected_total_tokens > max_tokens_per_run:
        return "MAX_LLM_TOKENS_PER_RUN"

    projected_usd = estimate_llm_usd(
        {
            "input_tokens": projected_input_tokens,
            "output_tokens": projected_output_tokens,
        }
    )
    if max_usd_per_run > 0 and usd + projected_usd > max_usd_per_run:
        return "MAX_LLM_USD_PER_RUN"

    return ""


def _print_llm_cap_check(
    *,
    estimated_prompt_tokens: int,
    max_tokens: int,
    used_tokens: int,
    run_cap: int,
    batch_size: int,
) -> None:
    projected_total = estimated_prompt_tokens + max(0, max_tokens)
    print(
        "[LLM CAP CHECK] "
        f"estimated_prompt_tokens={estimated_prompt_tokens} "
        f"max_tokens={max_tokens} "
        f"used_tokens={used_tokens} "
        f"run_cap={run_cap} "
        f"projected_total={projected_total} "
        f"batch_size={batch_size}"
    )


def _projected_usd_for_prompt(prompt_tokens: int, max_tokens: int) -> float:
    return estimate_llm_usd({"input_tokens": prompt_tokens, "output_tokens": max(0, max_tokens)})


def _lead_name_for_skip(record: Dict[str, Any], *, fallback_vertical: str) -> str:
    google = record.get("google", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    return lead.business_name


def _write_invalid_llm_response(bundle_dir: Path, *, batch_id: str, attempt: int, raw_text: str) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    debug_path = bundle_dir / f"{batch_id}_invalid_response_attempt_{attempt}.txt"
    debug_path.write_text(raw_text, encoding="utf-8")
    return debug_path


def _write_validation_failed_llm_response(bundle_dir: Path, *, batch_id: str, attempt: int, raw_text: str) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    debug_path = bundle_dir / f"{batch_id}_validation_failed_attempt_{attempt}.json"
    debug_path.write_text(raw_text, encoding="utf-8")
    return debug_path


def _looks_truncated_llm_response(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "unterminated string",
            "expecting value",
            "expecting ',' delimiter",
            "expecting property name enclosed in double quotes",
        )
    )


def _score_file_for_compact(compact_record: Dict[str, Any]) -> str:
    return str(compact_record.get("score_file") or "").strip()


def _affected_evidence_ref_options(
    compact_records: List[Dict[str, Any]],
    validation_message: str,
) -> List[Dict[str, Any]]:
    affected_indexes = {
        int(match.group(1))
        for match in re.finditer(r"scores\[(\d+)\]", validation_message)
        if int(match.group(1)) < len(compact_records)
    }
    affected_files = set(re.findall(r"\(([^()]+\.json)\)", validation_message))
    affected: List[Dict[str, Any]] = []

    for index, compact in enumerate(compact_records):
        score_file = _score_file_for_compact(compact)
        if affected_indexes and index not in affected_indexes and score_file not in affected_files:
            continue
        if affected_files and not affected_indexes and score_file not in affected_files:
            continue
        affected.append(
            {
                "score_file": score_file,
                "business_name": (compact.get("lead") or {}).get("business_name", ""),
                "evidence_ref_options": compact.get("evidence_ref_options") or [],
            }
        )

    if affected:
        return affected
    return [
        {
            "score_file": _score_file_for_compact(compact),
            "business_name": (compact.get("lead") or {}).get("business_name", ""),
            "evidence_ref_options": compact.get("evidence_ref_options") or [],
        }
        for compact in compact_records
    ]


def build_validation_repair_prompt(
    *,
    batch_id: str,
    validation_failures: str,
    invalid_bundle: Dict[str, Any],
    invalid_json_text: str,
    compact_records: List[Dict[str, Any]],
) -> str:
    affected_options = _affected_evidence_ref_options(compact_records, validation_failures)
    original_json = invalid_json_text.strip() or json.dumps(invalid_bundle, ensure_ascii=False, indent=2)
    return f"""# API LLM Score Bundle Schema Repair - {batch_id}

The previous response parsed as JSON but failed schema validation.

Validation failures:
{validation_failures}

Repair rules:
- Return one valid JSON object only.
- No markdown fences.
- No prose or commentary.
- Return the same score bundle with only schema repairs.
- Do not change batch_id.
- Do not change score_file values.
- Do not add, remove, or reorder leads.
- Do not rescore leads or change substantive scoring fields unless required for schema validity.
- evidence_refs is REQUIRED for every score.
- evidence_refs must be a non-empty array of 1-3 strings.
- Each evidence_refs value must be copied exactly from that lead's evidence_ref_options[].id.
- If the lead is low quality, C, or Skip, still include evidence_refs explaining why.
- Never output [] for evidence_refs.

Evidence ref options for affected leads:
{json.dumps(affected_options, ensure_ascii=False, indent=2)}

Original invalid JSON:
{original_json}
"""


def _fallback_fill_empty_evidence_refs(
    bundle: Dict[str, Any],
    compact_records: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any] | None, str]:
    fixed = copy.deepcopy(bundle)
    compact_by_file = {
        _score_file_for_compact(compact): compact
        for compact in compact_records
        if _score_file_for_compact(compact)
    }
    filled_files: List[str] = []

    for index, item in enumerate(fixed.get("scores") or []):
        if not isinstance(item, dict):
            return None, ""
        score = item.get("score")
        if not isinstance(score, dict):
            return None, ""
        if score.get("evidence_refs"):
            continue
        score_file = str(item.get("score_file") or "").strip()
        compact = compact_by_file.get(score_file)
        if compact is None and index < len(compact_records):
            compact = compact_records[index]
        options = (compact or {}).get("evidence_ref_options") or []
        fallback_refs = [str(option.get("id")) for option in options if option.get("id")][:3]
        if not fallback_refs:
            return None, ""
        score["evidence_refs"] = fallback_refs
        filled_files.append(score_file or f"scores[{index}]")

    if not filled_files:
        return None, ""

    try:
        validate_bundle_scores(fixed)
    except ValueError:
        return None, ""

    return fixed, f"deterministic evidence_refs fallback filled {', '.join(filled_files)}"


def _validation_failure_runtime_error(
    *,
    batch_id: str,
    validation_failures: str,
    raw_debug_path: Path,
    repair_debug_path: Path | None = None,
) -> RuntimeError:
    parts = [
        f"API LLM validation failed for {batch_id}",
        f"validation_failures={validation_failures}",
        f"raw_debug_path={raw_debug_path}",
    ]
    if repair_debug_path is not None:
        parts.append(f"repair_debug_path={repair_debug_path}")
    return RuntimeError("; ".join(parts))


def _score_batch_with_debug(
    prompt: str,
    *,
    batch_id: str,
    bundle_dir: Path,
    compact_records: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
) -> Dict[str, Any]:
    attempt = 1
    current_max_tokens = max_tokens
    while True:
        try:
            return llm_client.score_batch(prompt, model=model, max_tokens=current_max_tokens)
        except llm_client.InvalidLlmResponse as exc:
            parsed_json = getattr(exc, "parsed_json", None)
            if isinstance(parsed_json, dict):
                validation_debug_path = _write_validation_failed_llm_response(
                    bundle_dir,
                    batch_id=batch_id,
                    attempt=1,
                    raw_text=exc.raw_text,
                )
                validation_failures = str(exc)
                repair_prompt = build_validation_repair_prompt(
                    batch_id=batch_id,
                    validation_failures=validation_failures,
                    invalid_bundle=parsed_json,
                    invalid_json_text=exc.raw_text,
                    compact_records=compact_records,
                )
                try:
                    repaired = llm_client.score_batch(repair_prompt, model=model, max_tokens=current_max_tokens)
                    repaired["call_count"] = 2
                    repaired.setdefault("debug_notes", []).append(
                        f"validation repair succeeded for {batch_id}; raw response saved to {validation_debug_path}"
                    )
                    return repaired
                except llm_client.InvalidLlmResponse as repair_exc:
                    repair_debug_path: Path | None = None
                    repair_parsed_json = getattr(repair_exc, "parsed_json", None)
                    if isinstance(repair_parsed_json, dict):
                        repair_debug_path = _write_validation_failed_llm_response(
                            bundle_dir,
                            batch_id=batch_id,
                            attempt=2,
                            raw_text=repair_exc.raw_text,
                        )
                    fallback_bundle, fallback_note = _fallback_fill_empty_evidence_refs(parsed_json, compact_records)
                    if fallback_bundle is not None:
                        return {
                            "bundle": fallback_bundle,
                            "usage": {},
                            "call_count": 2,
                            "debug_notes": [
                                f"{fallback_note}; raw response saved to {validation_debug_path}; repair failed"
                            ],
                        }
                    raise _validation_failure_runtime_error(
                        batch_id=batch_id,
                        validation_failures=f"{validation_failures}\nRepair failures:\n{repair_exc}",
                        raw_debug_path=validation_debug_path,
                        repair_debug_path=repair_debug_path,
                    ) from repair_exc

            debug_path = _write_invalid_llm_response(
                bundle_dir,
                batch_id=batch_id,
                attempt=attempt,
                raw_text=exc.raw_text,
            )
            if attempt == 1 and current_max_tokens < 8192 and _looks_truncated_llm_response(exc):
                attempt += 1
                current_max_tokens = max(8192, current_max_tokens * 2)
                print(f"[LLM RETRY] Invalid/truncated JSON; raw response saved to {debug_path}; retrying with max_tokens={current_max_tokens}")
                continue
            raise ValueError(f"{exc}; raw LLM response saved to {debug_path}") from exc


def score_records_api_run(
    records: List[Dict[str, Any]],
    *,
    scores_dir: Path,
    fallback_vertical: str,
    batch_size: int = 5,
    model: str = DEFAULT_LLM_SCORING_MODEL,
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
    max_calls: int = DEFAULT_MAX_LLM_CALLS_PER_RUN,
    max_tokens_per_run: int = DEFAULT_MAX_LLM_TOKENS_PER_RUN,
    max_usd_per_run: float = DEFAULT_MAX_LLM_USD_PER_RUN,
) -> ApiLlmRun:
    selected = [ensure_evidence(record) for record in records]
    bundle_dir = scores_dir / "_api_bundles"
    calls = 0
    input_tokens = 0
    output_tokens = 0
    tokens = 0
    usd = 0.0
    stopped_by_cap = False
    stop_reason = ""
    skipped_records: List[ApiLlmSkip] = []
    debug_notes: List[str] = []
    api_evidence_ref_ids_by_lead_id: Dict[str, set[str]] = {}
    index = 0
    batch_index = 1
    configured_batch_size = max(1, batch_size)

    while index < len(selected):
        stop_reason = _hit_llm_cap(
            calls=calls,
            tokens=tokens,
            usd=usd,
            max_calls=max_calls,
            max_tokens_per_run=max_tokens_per_run,
            max_usd_per_run=max_usd_per_run,
        )
        if stop_reason:
            print(f"[STOP] Hit {stop_reason}")
            stopped_by_cap = True
            break

        current_batch_size = min(configured_batch_size, len(selected) - index)
        batch_id = f"batch_{batch_index:03d}"
        prompt = ""
        chunk: List[Dict[str, Any]] = []
        while current_batch_size > 0:
            chunk = selected[index : index + current_batch_size]
            compact_records = [
                build_llm_scoring_record(record, fallback_vertical=fallback_vertical)
                for record in chunk
            ]
            for record, compact in zip(chunk, compact_records):
                lead_id = lead_id_for_record(record, fallback_vertical=fallback_vertical)
                api_evidence_ref_ids_by_lead_id[lead_id] = compact_evidence_ref_ids(compact)
            prompt = build_api_llm_scoring_prompt(compact_records, batch_id=batch_id)
            estimated_prompt_tokens = estimate_prompt_tokens(prompt)
            projected_total_tokens = estimated_prompt_tokens + max(0, max_tokens)
            _print_llm_cap_check(
                estimated_prompt_tokens=estimated_prompt_tokens,
                max_tokens=max_tokens,
                used_tokens=tokens,
                run_cap=max_tokens_per_run,
                batch_size=current_batch_size,
            )

            chunk_exceeds_run_cap = max_tokens_per_run > 0 and projected_total_tokens > max_tokens_per_run
            remaining_exceeds_run_cap = (
                max_tokens_per_run > 0 and tokens + projected_total_tokens > max_tokens_per_run
            )
            projected_usd = _projected_usd_for_prompt(estimated_prompt_tokens, max_tokens)
            remaining_exceeds_usd_cap = max_usd_per_run > 0 and usd + projected_usd > max_usd_per_run

            if (chunk_exceeds_run_cap or remaining_exceeds_run_cap or remaining_exceeds_usd_cap) and current_batch_size > 1:
                current_batch_size = max(1, current_batch_size // 2)
                continue

            if chunk_exceeds_run_cap:
                skipped = ApiLlmSkip(
                    lead_name=_lead_name_for_skip(chunk[0], fallback_vertical=fallback_vertical),
                    score_file=score_file_for_record(chunk[0], fallback_vertical=fallback_vertical),
                    reason="single_record_exceeds_token_cap",
                )
                skipped_records.append(skipped)
                print(f"[LLM SKIP] {skipped.score_file}: {skipped.reason}")
                index += 1
                prompt = ""
                break

            if remaining_exceeds_run_cap:
                stop_reason = "MAX_LLM_TOKENS_PER_RUN"
                print(f"[STOP] Hit {stop_reason}")
                stopped_by_cap = True
                prompt = ""
                break

            if remaining_exceeds_usd_cap:
                stop_reason = "MAX_LLM_USD_PER_RUN"
                print(f"[STOP] Hit {stop_reason}")
                stopped_by_cap = True
                prompt = ""
                break

            break

        if stopped_by_cap:
            break
        if not prompt:
            continue

        result = _score_batch_with_debug(
            prompt,
            batch_id=batch_id,
            bundle_dir=bundle_dir,
            compact_records=compact_records,
            model=model,
            max_tokens=max_tokens,
        )
        bundle = result["bundle"]
        usage = result.get("usage") or {}
        calls += safe_int(result.get("call_count")) or 1
        debug_notes.extend(str(note) for note in result.get("debug_notes") or [] if str(note))
        batch_input_tokens = safe_int(usage.get("input_tokens"))
        batch_output_tokens = safe_int(usage.get("output_tokens"))
        batch_total_tokens = safe_int(usage.get("total_tokens")) or batch_input_tokens + batch_output_tokens
        input_tokens += batch_input_tokens
        output_tokens += batch_output_tokens
        tokens += batch_total_tokens
        usd += estimate_llm_usd(usage)

        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / f"{batch_id}_response.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        import_score_bundle(bundle_path, scores_dir)
        index += len(chunk)
        batch_index += 1

    manual_run = score_records_manual_run(
        selected,
        scores_dir=scores_dir,
        fallback_vertical=fallback_vertical,
        only_scored=True,
        api_evidence_ref_ids_by_lead_id=api_evidence_ref_ids_by_lead_id,
        api_merge_debug_dir=bundle_dir,
    )
    return ApiLlmRun(
        manual_run=manual_run,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens=tokens,
        usd=usd,
        caps={
            "MAX_LLM_CALLS_PER_RUN": max_calls,
            "MAX_LLM_TOKENS_PER_RUN": max_tokens_per_run,
            "MAX_LLM_USD_PER_RUN": max_usd_per_run,
        },
        stopped_by_cap=stopped_by_cap,
        stop_reason=stop_reason,
        skipped_records=skipped_records,
        debug_notes=debug_notes,
    )


def write_llm_usage(path: Path, run: ApiLlmRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "calls": run.calls,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.tokens,
        "estimated_usd": run.usd,
        "caps": run.caps,
        "stopped_by_cap": run.stopped_by_cap,
        "stop_reason": run.stop_reason,
        "skipped_records": [
            {"lead_name": skipped.lead_name, "score_file": skipped.score_file, "reason": skipped.reason}
            for skipped in (run.skipped_records or [])
        ],
        "debug_notes": run.debug_notes or [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_manual_only_scored_summary(run: ManualScoreRun) -> None:
    print(f"[MANUAL] Scored records processed: {len(run.rows)}")
    print(f"[MANUAL] Missing scores skipped: {len(run.missing_scores)}")

    if not run.missing_scores:
        print("[MANUAL] Skipped lead names: none")
        print("[MANUAL] Expected score paths: none")
        return

    print("[MANUAL] Skipped lead names:")
    for missing in run.missing_scores:
        print(f"[MANUAL] - {missing.lead_name}")

    print("[MANUAL] Expected score paths:")
    for missing in run.missing_scores:
        paths = ", ".join(str(path) for path in missing.expected_paths)
        print(f"[MANUAL] - {missing.lead_name}: {paths}")


def sort_key(row: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    priority = PRIORITY_RANK.get(row["Lead Priority"], 9)
    delta = DELTA_RANK.get(row["Offer Fit"], 9)
    site = SITE_RANK.get(row["Current Website Quality"], 9)

    packet = 0 if row.get("Packet Eligible") == "Yes" else 1

    # total only breaks ties after priority/delta/site.
    total = -safe_int(row.get("Score Total"))

    return (priority, packet, delta, site, total)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input enriched JSONL")
    parser.add_argument("--output", required=True, help="Output scored CSV")
    parser.add_argument("--vertical", default="service", help="Fallback vertical label")
    parser.add_argument("--mode", choices=["deterministic", "manual-llm", "api-llm"], default="deterministic")
    parser.add_argument("--scores-dir", default="", help="Manual/API LLM score JSON directory")
    parser.add_argument("--cards-output", default="", help="Optional lead cards JSONL output")
    parser.add_argument(
        "--only-scored",
        action="store_true",
        help="Manual LLM mode: skip enriched records without a matching score JSON",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max records to score")
    parser.add_argument("--llm-batch-size", type=int, default=5, help="API LLM leads per batch")
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        default=DEFAULT_LLM_MAX_TOKENS,
        help="API LLM max output tokens per batch",
    )
    parser.add_argument(
        "--llm-usage-output",
        default="",
        help="Optional API LLM usage/cap metadata JSON output",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(input_path)

    records = [ensure_evidence(record) for record in read_jsonl(input_path)]
    if args.limit > 0:
        records = records[: args.limit]

    if args.mode == "manual-llm":
        if not args.scores_dir:
            raise SystemExit("--scores-dir is required when --mode manual-llm")
        manual_run = score_records_manual_run(
            records,
            scores_dir=Path(args.scores_dir),
            fallback_vertical=args.vertical,
            only_scored=args.only_scored,
        )
        scored = manual_run.rows
        if args.only_scored:
            print_manual_only_scored_summary(manual_run)
    elif args.mode == "api-llm":
        if not args.scores_dir:
            raise SystemExit("--scores-dir is required when --mode api-llm")
        max_calls = getenv_int("MAX_LLM_CALLS_PER_RUN", DEFAULT_MAX_LLM_CALLS_PER_RUN)
        max_tokens_per_run = getenv_int("MAX_LLM_TOKENS_PER_RUN", DEFAULT_MAX_LLM_TOKENS_PER_RUN)
        max_usd_per_run = getenv_float("MAX_LLM_USD_PER_RUN", DEFAULT_MAX_LLM_USD_PER_RUN)
        api_run = score_records_api_run(
            records,
            scores_dir=Path(args.scores_dir),
            fallback_vertical=args.vertical,
            batch_size=args.llm_batch_size,
            model=(os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip(),
            max_tokens=args.llm_max_tokens,
            max_calls=max_calls,
            max_tokens_per_run=max_tokens_per_run,
            max_usd_per_run=max_usd_per_run,
        )
        scored = api_run.manual_run.rows
        print(f"[LLM] Calls: {api_run.calls}")
        print(f"[LLM] Tokens: {api_run.tokens}")
        print(f"[LLM] Estimated USD: {api_run.usd:.4f}")
        print(f"[LLM] Missing scores skipped: {len(api_run.manual_run.missing_scores)}")
        print(f"[LLM] Records skipped by LLM caps: {len(api_run.skipped_records or [])}")
        for skipped in api_run.skipped_records or []:
            print(f"[LLM] - {skipped.score_file}: {skipped.reason}")
        if api_run.stopped_by_cap:
            print(f"[LLM] Stopped by cap: {api_run.stop_reason}")
        if args.llm_usage_output:
            write_llm_usage(Path(args.llm_usage_output), api_run)
            print(f"[LLM] Usage metadata: {args.llm_usage_output}")
    else:
        scored = [score_record(record, args.vertical) for record in records]

    scored.sort(key=sort_key)

    write_csv(output_path, scored)

    if args.cards_output:
        cards = [build_lead_card(row) for row in scored]
        write_jsonl(Path(args.cards_output), cards)

    counts: Dict[str, int] = {}
    packets = 0

    for row in scored:
        counts[row["Lead Priority"]] = counts.get(row["Lead Priority"], 0) + 1
        if row.get("Packet Eligible") == "Yes":
            packets += 1

    print(f"[DONE] Read: {input_path}")
    print(f"[DONE] Wrote: {output_path}")
    if args.cards_output:
        print(f"[DONE] Lead cards: {args.cards_output}")
    print(f"[DONE] Rows: {len(scored)}")
    print(f"[DONE] Counts: {counts}")
    print(f"[DONE] Packet eligible: {packets}")

    print("\nTop leads:")
    for row in scored[:15]:
        print(
            f'{row.get("Lead Priority", ""):4} | '
            f'packet={row.get("Packet Eligible", "") or "No ":3} | '
            f'{row.get("Score Total", ""):3} | '
            f'{row.get("Business Name", "")[:38]:38} | '
            f'{row.get("Rating", "")}/{row.get("Reviews", "")} | '
            f'{row.get("Offer Fit", "")} | '
            f'{row.get("Current Website Quality", "")} | '
            f'{row.get("Review Fit Band", "")}'
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
