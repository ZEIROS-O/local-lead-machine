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
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from import_llm_score_bundle import import_score_bundle
from lead_machine.evidence import build_evidence_profile
from lead_machine.lead_card import build_lead_card
from lead_machine import llm_client
from lead_machine.llm_queue import safe_slug
from lead_machine.manual_scores import load_manual_score, merge_manual_score
from lead_machine.schema import normalize_lead
from lead_machine.sendability import apply_sendability
from lead_machine.verticals import VerticalProfile, resolve_profile
from make_llm_batch import build_batch_prompt, chunk_records, lead_packet_for_record


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
class ApiLlmRun:
    manual_run: ManualScoreRun
    calls: int
    tokens: int
    usd: float
    stopped_by_cap: bool
    stop_reason: str = ""


DEFAULT_LLM_SCORING_MODEL = "claude-sonnet-4-6"
DEFAULT_LLM_MAX_TOKENS = 4096
DEFAULT_MAX_LLM_CALLS_PER_RUN = 5
DEFAULT_MAX_LLM_TOKENS_PER_RUN = 100_000
DEFAULT_MAX_LLM_USD_PER_RUN = 5.0
SONNET_4_6_INPUT_USD_PER_MILLION_TOKENS = 3.0
SONNET_4_6_OUTPUT_USD_PER_MILLION_TOKENS = 15.0


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


def is_no_or_broken(record: Dict[str, Any]) -> bool:
    status = record.get("website", {}).get("site_status", "Unknown")
    return status in {"No Website", "Broken Website", "Social Only"}


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

    # Inverted-U review logic.
    # 5-20 reviews = best first-wave owner-led band. More reviews need clearer visible pain.
    if reviews >= 75:
        flags.append("75+ reviews requires very clear visible pain")
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
    if total < 35:
        priority = more_restrictive(priority, "C")

    if total < 25:
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
    record = ensure_evidence(record)
    vertical = infer_vertical(record, fallback_vertical)
    profile = resolve_profile(_record_vertical(record) or vertical, source_query=_record_source_query(record))

    strength, strength_evidence = score_business_strength(record, vertical, profile=profile)
    weakness, weakness_evidence = score_online_weakness(record)
    offer_fit, offer_score, offer_evidence = choose_offer_fit(record, strength, weakness)

    total = strength + weakness + offer_score
    priority, fit_cap, fit_flags = classify_priority(total, record, offer_fit, vertical, profile=profile)
    website_quality = infer_website_quality(record, weakness, offer_fit)
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
            f"Strength {strength}/40: {', '.join(strength_evidence) or 'none'}",
            f"Weakness {weakness}/40: {', '.join(weakness_evidence) or 'none'}",
            f"Offer {offer_score}/20: {offer_fit} ({', '.join(offer_evidence) or 'none'})",
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
        "Email": ", ".join(w.get("emails_found", [])),
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
        "Evidence Notes": evidence_notes,
        "AI Confidence": "medium",
        "Manual Reviewed": "",
        "Skip Reason": skip_reason(record, priority, total, offer_fit, fit_flags),
        "Raw Evidence": json.dumps(record, ensure_ascii=False),
        "Score Total": total,
        "Score Strength": strength,
        "Score Weakness": weakness,
        "Score Offer": offer_score,
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
    if isinstance(evidence, dict) and all(key in evidence for key in CORE_EVIDENCE_FIELDS):
        return _with_missing_rendered_evidence(record, evidence)
    updated = dict(record)
    updated["evidence"] = build_evidence_profile(record)
    return updated


def evidence_ids(record: Dict[str, Any]) -> set[str]:
    evidence = record.get("evidence") or {}
    return {str(item.get("id")) for item in evidence.get("evidence_items", []) if item.get("id")}


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
) -> ManualScoreRun:
    scored: List[Dict[str, Any]] = []
    missing_scores: List[MissingManualScore] = []
    for record in records:
        record = ensure_evidence(record)
        lead_name, expected_paths = manual_score_candidates(record, scores_dir, fallback_vertical)
        score_path = next((path for path in expected_paths if path.exists()), None)
        if score_path is None:
            if only_scored:
                missing_scores.append(MissingManualScore(lead_name=lead_name, expected_paths=expected_paths))
                continue
            raise FileNotFoundError(missing_manual_score_message(lead_name, expected_paths))

        score = load_manual_score(score_path, allowed_evidence_ids=evidence_ids(record))
        scored.append(merge_manual_score(record, score, fallback_vertical=fallback_vertical))

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
    tokens = 0
    usd = 0.0
    stopped_by_cap = False
    stop_reason = ""

    for batch_index, chunk in enumerate(chunk_records(selected, batch_size), start=1):
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

        batch_id = f"batch_{batch_index:03d}"
        packets = [lead_packet_for_record(record, fallback_vertical=fallback_vertical) for record in chunk]
        prompt = build_batch_prompt(packets, batch_id=batch_id)
        result = llm_client.score_batch(prompt, model=model, max_tokens=max_tokens)
        bundle = result["bundle"]
        usage = result.get("usage") or {}
        calls += 1
        tokens += safe_int(usage.get("total_tokens"))
        usd += estimate_llm_usd(usage)

        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / f"{batch_id}_response.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        import_score_bundle(bundle_path, scores_dir)

    manual_run = score_records_manual_run(
        selected,
        scores_dir=scores_dir,
        fallback_vertical=fallback_vertical,
        only_scored=True,
    )
    return ApiLlmRun(
        manual_run=manual_run,
        calls=calls,
        tokens=tokens,
        usd=usd,
        stopped_by_cap=stopped_by_cap,
        stop_reason=stop_reason,
    )


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
        api_run = score_records_api_run(
            records,
            scores_dir=Path(args.scores_dir),
            fallback_vertical=args.vertical,
            batch_size=args.llm_batch_size,
            model=(os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip(),
            max_tokens=args.llm_max_tokens,
            max_calls=getenv_int("MAX_LLM_CALLS_PER_RUN", DEFAULT_MAX_LLM_CALLS_PER_RUN),
            max_tokens_per_run=getenv_int("MAX_LLM_TOKENS_PER_RUN", DEFAULT_MAX_LLM_TOKENS_PER_RUN),
            max_usd_per_run=getenv_float("MAX_LLM_USD_PER_RUN", DEFAULT_MAX_LLM_USD_PER_RUN),
        )
        scored = api_run.manual_run.rows
        print(f"[LLM] Calls: {api_run.calls}")
        print(f"[LLM] Tokens: {api_run.tokens}")
        print(f"[LLM] Estimated USD: {api_run.usd:.4f}")
        print(f"[LLM] Missing scores skipped: {len(api_run.manual_run.missing_scores)}")
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
