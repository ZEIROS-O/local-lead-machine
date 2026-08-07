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
import shutil
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Tuple

from dotenv import load_dotenv

from import_llm_score_bundle import import_score_bundle, validate_bundle_scores
from lead_machine.buyer_signals import (
    buyer_intent_summary,
    demand_signal_state,
    ensure_buyer_intent,
    has_money_signal as buyer_has_money_signal,
    money_signal_label,
    money_signal_state,
    review_demand_band as buyer_review_demand_band,
)
from lead_machine.compliance import (
    ComplianceError,
    is_suppressed_record,
    load_configured_suppression_index,
    merge_suppression_disqualifiers,
    record_matches_suppression,
)
from lead_machine.email_hygiene import emails_text, sanitize_record_emails
from lead_machine.evidence import build_evidence_profile
from lead_machine.lead_card import build_lead_card
from lead_machine import llm_client
from lead_machine.llm_queue import safe_slug
from lead_machine.manual_scores import allowed_evidence_ids as original_evidence_ids
from lead_machine.manual_scores import load_manual_score, merge_manual_score
from lead_machine.score_schema import MANUAL_SCORE_SCHEMA, SHARED_COMMERCIAL_SCORING_RULES
from lead_machine.schema import normalize_lead
from lead_machine.scoring_rules import SHARED_SENDABILITY_RULES
from lead_machine.scoring_v3 import attach_scoring_v3
from lead_machine.sendability import apply_sendability, normalize_evidence_refs
from lead_machine.scoring_profiles import market_service_area_terms
from lead_machine.verticals import (
    VERTICAL_PROFILES,
    VerticalProfile,
    matches_any_term,
    resolve_profile,
)
from make_llm_batch import score_file_for_record


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

# Phrasings that show service-area clarity without naming a place. The place
# names themselves come from the lead's own market profile via
# `market_service_area_terms`, because this scorer now runs 11 metros and
# naming Cleveland's suburbs here penalised every business in the other ten.
GENERIC_SERVICE_AREA_TERMS = (
    "service area",
    "areas we serve",
    "serving",
    "county",
)

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

# Statuses meaning the business has no owned site to rebuild. "Jobber Only" was
# missing from the grade path entirely, so a Jobber-only business fell through
# to Skip unless non_standard_site_or_booking happened to catch it.
NO_OWNED_SITE_STATUSES = {"No Website", "Social Only", "Jobber Only"}

CONCRETE_PACKET_OFFERS = {
    "No-Site Launch",
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
    skipped_already_scored: int = 0
    new_records_selected: int = 0


@dataclass(frozen=True)
class LlmCostEstimate:
    records_selected: int
    batches: int
    prompt_tokens: int
    max_output_tokens: int
    projected_total_tokens: int
    projected_usd: float


DEFAULT_LLM_SCORING_MODEL = "claude-sonnet-5"
DEFAULT_LLM_MAX_TOKENS = 4096
# Sized against a real batch, 2026-08-05. The originals (5 calls / 100k tokens)
# came from the first LLM-scoring commit on 2026-06-20, when a "batch" was a
# handful of test leads; they were never revisited and had become the binding
# throughput limit — a 133-lead batch is 27 calls, so a 5-call ceiling scored 25
# leads and stopped, making concurrency worth ~1.25x instead of 4x.
#
# Reference point: 133 leads at --llm-batch-size 5 = 27 batches, 370,862
# projected tokens, $2.44 projected / ~$2.20 actual on kimi-k3. These leave
# roughly 2x headroom on that, because the halving path at _plan_api_llm_batches
# can split a batch and push the call count above the naive ceil(leads / size).
#
# MAX_LLM_USD_PER_RUN is the real backstop — it is denominated in the thing that
# actually hurts. The calls and tokens ceilings are blunt proxies kept as a
# second line of defence against a pricing bug making USD read low.
DEFAULT_MAX_LLM_CALLS_PER_RUN = 60
DEFAULT_MAX_LLM_TOKENS_PER_RUN = 800_000
DEFAULT_MAX_LLM_USD_PER_RUN = 10.0
# Worst-case billed calls one batch can spend inside `_score_batch_with_debug`:
# the initial call, a truncation retry, and a validation-repair call. A batch in
# flight has banked nothing yet, so the concurrent cap gate has to charge it at
# this worst case — otherwise `MAX_LLM_CALLS_PER_RUN` counts batches rather than
# calls and a cap of 60 admits ~180 billed calls. It is the same multiplier for
# tokens and USD: a retried batch re-sends its prompt and doubles its output
# allowance, so it lands at 2-3x the single-call reservation the planner made.
MAX_LLM_CALLS_PER_BATCH = 3
DEFAULT_LLM_SPEND_CONFIRM_USD = 3.00
# Per-model list prices in USD per million tokens, as (input, output).
#
# Cost was previously computed from two constants named for Sonnet 4.6 while
# DEFAULT_LLM_SCORING_MODEL had already moved to claude-sonnet-5 — so
# MAX_LLM_USD_PER_RUN, --dry-run-llm-cost and the spend-confirm threshold were
# all measuring a model the run was not using. An unknown model now raises
# rather than being priced as if it were Sonnet, because a silently wrong price
# on a spend cap is worse than a refusal.
LLM_PRICING_USD_PER_MILLION_TOKENS: Dict[str, Tuple[float, float]] = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-fable-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-3-5-haiku-20241022": (0.8, 4.0),
    # Moonshot list price, confirmed in Task 0 against the Moonshot console.
    "kimi-k3": (3.0, 15.0),
    # Subscription runners are $0 marginal; they still count against
    # MAX_LLM_CALLS_PER_RUN and MAX_LLM_TOKENS_PER_RUN.
    "subscription:codex": (0.0, 0.0),
    "subscription:claude": (0.0, 0.0),
}


class UnknownLlmModelError(ValueError):
    pass


def llm_pricing(model: str) -> Tuple[float, float]:
    """(input, output) USD per million tokens for *model*.

    Matches on the exact id first, then on the longest registered id that the
    given model starts with, so a dated snapshot such as
    "claude-sonnet-5-20260101" prices as claude-sonnet-5 rather than failing.
    """
    key = str(model or "").strip()
    if not key:
        raise UnknownLlmModelError("No LLM model configured; cannot price the run.")
    if key in LLM_PRICING_USD_PER_MILLION_TOKENS:
        return LLM_PRICING_USD_PER_MILLION_TOKENS[key]

    prefixes = [name for name in LLM_PRICING_USD_PER_MILLION_TOKENS if key.startswith(name)]
    if prefixes:
        return LLM_PRICING_USD_PER_MILLION_TOKENS[max(prefixes, key=len)]

    known = ", ".join(sorted(LLM_PRICING_USD_PER_MILLION_TOKENS))
    raise UnknownLlmModelError(
        f"No pricing registered for LLM model {key!r}; spend caps would be wrong. "
        f"Add it to LLM_PRICING_USD_PER_MILLION_TOKENS. Known models: {known}"
    )
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


def is_corporate_location_name(name: str, record: Dict[str, Any]) -> bool:
    """Does the business name read as "Brand of <place>"?

    In roofing that pattern usually marks a vendorised location page rather than
    an owner-led shop. The place list used to be five hardcoded Ohio names, so
    the check simply did not exist for the other ten metros. It now reads the
    lead's own market profile.
    """
    if " of " not in name:
        return False
    places = market_service_area_terms(record)
    return any(f" of {place}" in name for place in places)


def infer_vertical(record: Dict[str, Any], fallback: str) -> str:
    """Resolve the display vertical through the profile registry.

    This used to be four hardcoded substring rules that knew only roofing,
    hardscaping, masonry and landscaping — every other vertical fell through to
    `fallback.title()`, and any copy containing "concrete" or "brick" was
    relabelled "Masonry / Hardscaping". A detailing shop advertising work on
    concrete floors was reported as a masonry lead. AGENTS.md asks for verticals
    as data, not code, and `resolve_profile` already holds that data.
    """
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    declared = google.get("vertical") or record.get("vertical") or record.get("Vertical") or ""
    profile = resolve_profile(
        str(declared or fallback),
        source_query=_record_source_query(record),
        fallback=fallback,
    )

    # A declared vertical is the operator's own statement about the batch and
    # outranks anything inferred from scraped copy.
    if str(declared or "").strip():
        return profile.label

    blob = text_blob(record)
    name = str(google.get("business_name") or "").lower()
    text = f"{blob} {name}"

    # Only consider profiles the resolver actually knows, and require a match on
    # the profile's own batch terms — the terms that define the vertical, rather
    # than incidental service words that several verticals share.
    matches = [
        candidate
        for candidate in VERTICAL_PROFILES.values()
        if matches_any_term(text, candidate.batch_terms)
    ]
    if len(matches) == 1:
        return matches[0].label
    if matches:
        # Ambiguous copy: prefer the batch's own vertical if it is one of the
        # candidates, rather than letting term ordering decide.
        for candidate in matches:
            if candidate.key == profile.key:
                return candidate.label

    return profile.label


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
    if family == "roofing" and is_corporate_location_name(name, record):
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

    # The no-site demotion used to live here: `no_site and not money_signal -> C`.
    # It could not do anything but fire. money_signal comes from paid-CRM text
    # scraped off the website, so a business with no website could never satisfy
    # it — measured across two real batches, 25/25 and 13/13 no-site leads went
    # base=A -> ceiling=C -> final=C, which put them below the A/B floor for
    # Sendable. That inverted the product thesis: no-site businesses are the
    # easiest sell, and every search config sets allow_missing_website: true to
    # go and find them. Batch 2 made money_signal honest about the difference
    # between "no spend" and "not looked"; this removes the rule that punished
    # the lead for the pipeline's own blind spot.

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

    # Inverted-U: ideal_growth is the best reputation/demand fit, while mature businesses need sharper pain.
    review_band = review_fit_band(record, vertical)
    if review_band == "ideal_growth":
        score += 10
        evidence.append(f"{reviews} reviews in ideal_growth demand band")
    elif review_band == "early_proof":
        score += 6
        evidence.append(f"{reviews} reviews in early_proof demand band")
    elif review_band == "established":
        score += 5
        evidence.append(f"{reviews} reviews in established demand band; needs clear visible pain")
    elif review_band == "mature":
        score += 3
        evidence.append(f"{reviews} reviews in mature demand band; requires strong conversion pain")

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

    service_area_terms = (*GENERIC_SERVICE_AREA_TERMS, *market_service_area_terms(record))
    if not any(t in blob for t in service_area_terms):
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
    review_signal = buyer_intent.get("review_demand_signal") or buyer_intent.get("review_profile") or {}
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

    demand_band = str(review_signal.get("demand_band") or "").strip().lower()
    if demand_band == "ideal_growth":
        score += 5
        evidence.append("ideal-growth review demand band")
    elif demand_band in {"early_proof", "established", "mature"}:
        score += 2
        evidence.append(f"{demand_band} review demand band")

    yes_ads = [
        field
        for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge")
        if ads.get(field) == "yes"
    ]
    if yes_ads:
        score += 4
        evidence.append(f"ad signal detected ({', '.join(yes_ads)})")

    # money_signal is tri-state text, so a bare truthiness test would score
    # "no" and "unknown" as if they were a detected paid stack.
    money_state = money_signal_state(record)
    if money_state == "yes":
        score += 2
        evidence.append("buyer-intent money signal present")
    elif money_state == "unknown":
        evidence.append("buyer-intent money signal not observable (no crawlable site text)")
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

    # Split by whether an owned site exists at all. There is nothing to rebuild
    # for a business that only has a Facebook page or a Jobber booking link, and
    # the LLM path already draws this line (its "Social/Jobber Escape" maps to
    # No-Site Launch), so the deterministic path should agree. Same offer score
    # and same A grade either way — only the label the operator pitches differs.
    if status in NO_OWNED_SITE_STATUSES:
        evidence.append("no owned site: missing or social/booking-only")
        return "No-Site Launch", 20, evidence

    if status == "Broken Website":
        evidence.append("owned site is broken")
        return "Website Rebuild", 20, evidence

    if d.get("non_standard_site_or_booking"):
        evidence.append("uses booking/social URL instead of full owned site")
        return "No-Site Launch", 20, evidence

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

    if offer_fit in {"No-Site Launch", "Website Rebuild"}:
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

    if status in NO_OWNED_SITE_STATUSES or status == "Broken Website":
        return "A"

    if offer_fit in {"No-Site Launch", "Website Rebuild"}:
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


# The AirTable Prospects write path needs the complete grade vocabulary, not
# just the currently common A/B/C values: hard disqualifiers and low scores
# are deliberately emitted as Skip.
LEAD_PRIORITY_CHOICES = frozenset(PRIORITY_RANK)


ICP_CEILING_SHADOW_FIELDS = ("ICP Ceiling", "ICP Ceiling Flags", "Grade Disagreement")


def attach_icp_ceiling_shadow(
    row: Dict[str, Any],
    record: Dict[str, Any],
    *,
    fallback_vertical: str,
) -> Dict[str, Any]:
    """Record what the closeability layer *would* have said, without enforcing it.

    `fit_ceiling` runs only inside `score_record`, so in `--mode api-llm` — the
    mode production actually uses — the grade is a bare threshold on the LLM
    total and the ICP layer never runs at all. Measured across 161 leads, 15
    carried a sendable-eligible B against a C ceiling, so the two graders
    disagree often enough to matter and neither has been shown right.

    These are CSV and packet columns only. They are deliberately not added to
    the Airtable surface, which PR #9 just cut from 65 columns to 54 and which
    four separate lists have to agree on.
    """
    vertical = _record_vertical(record) or fallback_vertical
    try:
        profile = resolve_profile(vertical, source_query=_record_source_query(record))
        ceiling, fit_flags = fit_ceiling(record, vertical, profile=profile)
    except Exception as exc:
        # A shadow measurement must never be able to fail a scoring run.
        row["ICP Ceiling"] = ""
        row["ICP Ceiling Flags"] = f"ceiling_unavailable: {exc}"
        row["Grade Disagreement"] = ""
        return row

    grade = str(row.get("Lead Priority") or "").strip()
    row["ICP Ceiling"] = ceiling
    row["ICP Ceiling Flags"] = "; ".join(str(flag) for flag in fit_flags)
    # Only a grade *better* than the ceiling is a disagreement. A grade at or
    # below it is the ceiling doing nothing, which is not a finding.
    disagrees = bool(grade) and bool(ceiling) and more_restrictive(grade, ceiling) != grade
    row["Grade Disagreement"] = f"{grade}_vs_{ceiling}" if disagrees else ""
    return row


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

    if offer_fit == "No-Site Launch":
        return (
            f"{name} has credible local demand/proof ({rating} rating, {reviews} reviews; {strength}) but no owned website — site status is {status}. "
            f"Everything runs through a profile or booking link they do not control. Fit context: {fit}. The pitch is a first owned site with a working estimate path, not a redesign."
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
    return "Offer fit is not one of the concrete visible-pain offers"


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


def governed_opener(
    record: Dict[str, Any],
    offer_fit: str,
    *,
    channel: str = "sms",
    problem_severity: str = "material",
    commercial_materiality: str = "material",
    buyer_sophistication: str = "late",
    credibility: str = "lpm-identity",
    relationship_warmth: str = "cold",
    evidence_strength: str = "strong",
    asset_state: str = "none",
    desired_next_step: str = "show-the-mechanism",
    asset_id: str = "",
    asset_verifier_verdict: str = "",
) -> str:
    """Select one governed opening move from declared context; never imply nonexistent proof."""
    g = record.get("google", {}) or {}
    business = g.get("business_name", "your business")
    send_trigger = str((record.get("evidence", {}) or {}).get("send_trigger") or "").strip()
    observation = send_trigger or (
        "the estimate path is difficult to locate" if offer_fit == "Estimate Funnel"
        else "the online conversion path is weaker than the visible business proof"
    )
    if not send_trigger and evidence_strength == "strong":
        evidence_strength = "moderate"
    if asset_state == "verified-existing" and (not asset_id or asset_verifier_verdict != "PASS"):
        raise ValueError("verified-existing opening requires asset_id and PASS asset verifier verdict")
    if asset_state not in {"none", "planned", "verified-existing"}:
        raise ValueError(f"invalid asset_state: {asset_state}")
    if relationship_warmth not in {"cold", "warm"}:
        raise ValueError(f"invalid relationship_warmth: {relationship_warmth}")

    spoken = channel.lower() in {"phone", "call", "voicemail"}
    identity = "Lou with LPM"
    if relationship_warmth == "warm":
        lead = f"{identity}. Following up on our earlier conversation about {business}: {observation}."
    elif evidence_strength == "strong" and problem_severity == "material" and commercial_materiality == "material":
        lead = f"{identity}. I went through {business}'s customer path and found this: {observation}."
    else:
        lead = f"{identity}. I may be missing an intentional setup, but I found this on {business}'s customer path: {observation}."

    consequence = " That adds friction between existing demand and a completed inquiry."
    if buyer_sophistication == "high":
        consequence = " The mechanism is the handoff between visible demand and a completed inquiry."
    if asset_state == "verified-existing":
        next_step = f" I built a verified example ({asset_id}) that shows the path."
    elif asset_state == "planned":
        next_step = " I can build the example against the exact path I found."
    else:
        next_step = " I can show you what I mean without pretending the example already exists."
    if desired_next_step == "diagnostic-question" and relationship_warmth == "warm":
        next_step = " Is that path intentional, or is it worth walking through?"
    if spoken:
        next_step = ""
    del credibility
    return lead + consequence + next_step


def preferred_outreach_channel(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) or {}
    website = record.get("website", {}) or {}
    if str(google.get("phone") or "").strip():
        return "Phone"
    if website.get("emails_found"):
        return "Email"
    return ""


def deterministic_opener(record: Dict[str, Any], offer_fit: str) -> str:
    channel = preferred_outreach_channel(record)
    return governed_opener(record, offer_fit, channel=channel.lower() or "phone")


def review_fit_band(record: Dict[str, Any], vertical: str) -> str:
    return buyer_review_demand_band(record, vertical=vertical)


def infer_metro_context(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) or {}
    source = " ".join(
        str(google.get(key) or record.get(key) or "").lower()
        for key in ("source_batch", "Source Batch", "source_geo", "Source Geo")
    )
    profile_dir = Path(__file__).resolve().parent / "market_profiles"
    matches = []
    for path in profile_dir.glob("*.json"):
        slug = path.stem
        token = slug.replace("_", " ")
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", source.replace("_", " ")):
            matches.append(slug)
    return matches[0].replace("_", "-") if len(set(matches)) == 1 else ""


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
    money_signal = money_signal_label(record)
    demand_signal = demand_signal_state(record).capitalize()
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

    site_state = str(w.get("site_status") or "").strip().lower().replace("_", " ")
    pain_categories = {str(value).strip() for value in evidence_profile.get("visible_pain_categories") or []}
    issue_flags = {str(value).strip() for value in w.get("site_issue_flags") or []}
    broad_system_issue = site_state in {
        "no website", "missing", "broken website", "broken", "social only", "jobber only",
    } or bool(pain_categories & {
        "premium_perception_gap", "portfolio_presentation_pain", "estimate_path_pain",
        "mobile_usability_pain", "trust_proof_pain", "service_clarity_pain",
        "no_site_or_social_only_pain", "broken_site_pain", "followup_or_missed_call_pain",
    }) or bool(issue_flags & {
        "premium_perception_gap", "premium_brand_mismatch", "outdated_site", "weak_project_gallery",
        "buried_project_photos", "missing_estimate_path", "weak_mobile_path", "low_visible_trust",
        "reviews_not_near_cta", "followup_gap", "missed_call_path",
    })
    commercial_state = (
        "commercially-sendable"
        if priority in {"A", "B"} and send_trigger and broad_system_issue
        else "needs-broader-system-evidence" if send_trigger else "unverified"
    )
    offer_component = (
        "Get Found" if offer_fit in {"No-Site Launch", "Website Rebuild", "Website Cleanup", "Premium Perception Gap", "Portfolio Rebuild", "Service Page Cleanup"}
        else "Get the Lead" if offer_fit in {"Estimate Funnel", "Mobile Quote Path Cleanup", "Social/Jobber Escape", "Review/Trust Proof Upgrade"}
        else "See It All" if offer_fit == "Tracking" else "Get the Lead"
    )
    commercial_dimensions = {
        "Commercial Materiality": f"{send_trigger} affects a high-intent customer path." if commercial_state == "commercially-sendable" else "",
        "Recurring Consequence": "The same observed path is presented to each new prospective customer." if commercial_state == "commercially-sendable" else "",
        "Root Cause Confidence": "Fresh structured site evidence supports the named path-level issue." if commercial_state == "commercially-sendable" else "",
        "LPM Offer Component": offer_component if commercial_state == "commercially-sendable" else "",
        "Packageable Solution": f"A bounded {offer_fit} implementation tied to the verified path." if commercial_state == "commercially-sendable" else "",
        "Value Mechanism": "Reduce friction between existing demand and a completed inquiry or estimate request." if commercial_state == "commercially-sendable" else "",
        "Beyond Free Fix": "The issue spans the customer path, trust/CTA presentation, and conversion routing rather than one isolated edit." if commercial_state == "commercially-sendable" else "",
        "Self Fix Risk": "The observed multi-part path remains live and requires coordinated implementation, not a link swap." if commercial_state == "commercially-sendable" else "",
        "Intentionality": "The customer-facing path and stated service intent do not support treating the dead end as intentional." if commercial_state == "commercially-sendable" else "",
    }

    contact_email = emails_text(
        w.get("emails_found", []),
        business_domain=w.get("domain") or w.get("final_url") or g.get("website_url", ""),
    )
    row = {
        "Business Name": g.get("business_name", ""),
        "Owner Name": "",
        "Vertical": vertical,
        "City": g.get("city", ""),
        "Metro Context": infer_metro_context(record),
        "Phone": g.get("phone", ""),
        "Email": contact_email,
        "Website URL": g.get("website_url", ""),
        "Google Maps URL": g.get("google_maps_url", ""),
        "Reviews": g.get("reviews", ""),
        "Rating": g.get("rating", ""),
        "Current Website Quality": website_quality,
        "Best Service Angle": angle,
        "Specific Observation": observation,
        "Mockup URL": "",
        "Video URL": "",
        "Outreach Channel": preferred_outreach_channel(record),
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
        "Demand Signal": demand_signal,
        "demand_signal": demand_signal,
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
        "Review Demand Band": review_fit_band(record, vertical),
        "Opener": deterministic_opener(record, offer_fit),
        "Evidence Refs": json.dumps(evidence_refs, ensure_ascii=False),
        "Evidence Snapshot": json.dumps(evidence_profile, ensure_ascii=False),
        "Safe Outreach Claims": json.dumps(safe_claims, ensure_ascii=False),
        "Unsafe Claims": json.dumps(
            [
                "Do not claim they are losing jobs.",
                "Do not claim a form is broken unless the evidence verifies it.",
            ],
            ensure_ascii=False,
        ),
        "Commercial Fit State": commercial_state,
        "commercial_fit_state": commercial_state,
        **commercial_dimensions,
        "Contact Eligibility": "contact-now" if commercial_state == "commercially-sendable" else "hold-commercial-case",
        "Asset State": "none",
        "Promise State": "asset-free",
        "Packet Eligible": packet_eligible(priority, offer_fit),
        "Packet Eligibility Reason": packet_eligibility_reason(priority, offer_fit),
    }
    merge_suppression_disqualifiers(row, record)
    governed = apply_sendability(row)
    return attach_scoring_v3(record, governed, fallback_vertical=fallback_vertical)


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

    fieldnames: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def raise_csv_field_limit() -> None:
    """Allow csv to read very large fields (e.g. the Raw Evidence JSON blob).

    The default field-size limit is 128 KB; real scored CSVs routinely exceed it,
    which would otherwise raise `_csv.Error: field larger than field limit`.
    """
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    raise_csv_field_limit()
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _stable_key_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _phone_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _loads_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def stable_lead_keys_from_row(row: Dict[str, Any]) -> set[str]:
    raw = _loads_dict(row.get("Raw Evidence") or row.get("raw_evidence"))
    google = raw.get("google", {}) if isinstance(raw.get("google"), dict) else {}
    keys: set[str] = set()

    place_key = _stable_key_text(row.get("Place Key") or row.get("place_key"))
    if place_key:
        keys.add(f"place_key:{place_key}")
        if place_key.startswith("chij"):
            keys.add(f"place:{place_key}")

    place_id = _stable_key_text(row.get("place_id") or row.get("Place ID") or google.get("place_id"))
    if place_id:
        keys.add(f"place:{place_id}")

    maps_url = _stable_key_text(
        row.get("Google Maps URL")
        or row.get("google_maps_url")
        or google.get("google_maps_url")
        or google.get("maps_url")
    )
    if maps_url:
        keys.add(f"maps:{maps_url}")

    business_name = _stable_key_text(
        row.get("Business Name")
        or row.get("business_name")
        or google.get("business_name")
    )
    phone = _phone_digits(row.get("Phone") or row.get("phone") or google.get("phone"))
    if business_name and phone:
        keys.add(f"name_phone:{business_name}|{phone}")

    return keys


def stable_lead_keys_from_record(record: Dict[str, Any], *, fallback_vertical: str) -> set[str]:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    keys: set[str] = set()

    place_key = _stable_key_text(lead.place_id_or_unique_key)
    if place_key:
        keys.add(f"place_key:{place_key}")
        if place_key.startswith("chij"):
            keys.add(f"place:{place_key}")

    place_id = _stable_key_text(google.get("place_id"))
    if place_id:
        keys.add(f"place:{place_id}")

    maps_url = _stable_key_text(lead.maps_url or google.get("google_maps_url") or google.get("maps_url"))
    if maps_url:
        keys.add(f"maps:{maps_url}")

    business_name = _stable_key_text(lead.business_name or google.get("business_name"))
    phone = _phone_digits(lead.phone or google.get("phone"))
    if business_name and phone:
        keys.add(f"name_phone:{business_name}|{phone}")

    return keys


def stable_lead_keys_from_rows(rows: List[Dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        keys.update(stable_lead_keys_from_row(row))
    return keys


def filter_unscored_records(
    records: List[Dict[str, Any]],
    *,
    existing_keys: set[str],
    fallback_vertical: str,
) -> Tuple[List[Dict[str, Any]], int]:
    if not existing_keys:
        return records, 0
    existing_keys = {_stable_key_text(key) for key in existing_keys if _stable_key_text(key)}
    selected: List[Dict[str, Any]] = []
    skipped = 0
    for record in records:
        record_keys = stable_lead_keys_from_record(record, fallback_vertical=fallback_vertical)
        if record_keys & existing_keys:
            skipped += 1
            continue
        selected.append(record)
    return selected, skipped


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
EV_REF_ID_RE = re.compile(r"\bev-[A-Za-z0-9_-]+(?:[.:][A-Za-z0-9_-]+)*\b")


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
        attach_icp_ceiling_shadow(row, record, fallback_vertical=fallback_vertical)
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


def estimate_llm_usd(usage: Dict[str, Any], *, model: str = DEFAULT_LLM_SCORING_MODEL) -> float:
    input_price, output_price = llm_pricing(model)
    input_tokens = safe_int(usage.get("input_tokens"))
    output_tokens = safe_int(usage.get("output_tokens"))
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def resolve_projection_model() -> str:
    """Priciest model the chain could reach — the model to project spend with.

    `llm_client.resolve_scoring_model()` names the *first* provider's model, and
    a $0 subscription runner first in the chain projects $0 for the whole run.
    That silently disables the `LLM_SPEND_CONFIRM_USD` refusal gate even though
    a fallback to Kimi or the Claude API spends real money. A pre-spend guard
    has to assume the worst tier the chain can fall through to.
    """
    expected = llm_client.resolve_scoring_model()
    try:
        candidates = [llm_client.model_for_provider(p) for p in llm_client.provider_chain()]
    except Exception:  # never let projection break a run
        return expected
    priced: List[Tuple[float, str]] = []
    for candidate in {c for c in candidates if c} | {expected}:
        try:
            _, output_price = llm_pricing(candidate)
        except UnknownLlmModelError:
            continue
        priced.append((output_price, candidate))
    if not priced:
        return expected
    return max(priced)[1]


def resolve_served_model(result: Dict[str, Any], fallback: str) -> str:
    """Model id to price a served batch with, falling back to the run-level one.

    The chain reports which provider served the batch so spend lands on the
    right price row. But a provider is free-form about what it reports, and an
    id with no pricing entry must not kill a run mid-flight — the call is
    already paid for. Unknown ids fall back to the run-level model loudly;
    MAX_LLM_TOKENS_PER_RUN still bounds the run either way.
    """
    candidate = result.get("model")
    if not isinstance(candidate, str) or not candidate.strip():
        return fallback
    candidate = candidate.strip()
    try:
        llm_pricing(candidate)
    except UnknownLlmModelError:
        print(
            f"[WARN] Provider reported unpriced model {candidate!r}; "
            f"charging this batch at {fallback!r} instead. Add it to "
            f"LLM_PRICING_USD_PER_MILLION_TOKENS so spend caps stay accurate.",
            file=sys.stderr,
        )
        return fallback
    return candidate


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
) -> None:
    cleaned_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(option_id or "").strip()).strip("_")
    if not cleaned_id or cleaned_id in seen_ids:
        return
    options.append({"id": cleaned_id, "label": _short_snippet(label, max_chars=80)})
    seen_ids.add(cleaned_id)


def _iter_prompt_visible_strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: List[str] = []
        for item in value.values():
            strings.extend(_iter_prompt_visible_strings(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(_iter_prompt_visible_strings(item))
        return strings
    return []


def _add_prompt_visible_ev_ref_options(capsule: Dict[str, Any]) -> None:
    options = capsule.setdefault("evidence_ref_options", [])
    if not isinstance(options, list):
        options = []
        capsule["evidence_ref_options"] = options
    seen_ids = {
        str(option.get("id") or "").strip()
        for option in options
        if isinstance(option, dict) and option.get("id")
    }
    visible_payload = {
        key: value
        for key, value in capsule.items()
        if key != "evidence_ref_options"
    }
    for text in _iter_prompt_visible_strings(visible_payload):
        for match in EV_REF_ID_RE.finditer(text):
            _append_evidence_ref_option(
                options,
                seen_ids,
                option_id=match.group(0),
                label="Prompt-visible evidence ref",
            )


def _build_evidence_ref_options(
    record: Dict[str, Any],
    *,
    lead: Any | None = None,
    selected_evidence_item_ids: set[str] | None = None,
) -> List[Dict[str, str]]:
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
        )

    services = website.get("high_ticket_terms") or evidence.get("services_detected") or []
    if services:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="homepage_services",
            label="High-ticket services",
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
        )

    if buyer_intent:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="buyer_intent",
            label="Buyer intent evidence",
        )

    source_query = google.get("source_query") or google.get("search_query") or google.get("category") or ""
    if source_query:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="icp_source_query",
            label="ICP source query",
        )

    selected_evidence_item_ids = selected_evidence_item_ids or set()
    for item in evidence.get("evidence_items", []) or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id not in selected_evidence_item_ids:
            continue
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id=item_id,
            label=str(item.get("field") or "Evidence item"),
        )

    if not options:
        _append_evidence_ref_option(
            options,
            seen_ids,
            option_id="google_profile",
            label="Google profile",
        )
    return options


def _compact_buyer_intent(buyer_intent: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the decision-relevant buyer-intent signals for the prompt.

    The raw ``buyer_intent`` object carries verbose proof arrays and error blobs
    that duplicate ``buyer_intent_summary``, the top-level ``money_signal`` flag,
    and ``provider_evidence.crm_booking_stack``. This keeps the structured money
    and demand signals the rubric leans on without re-sending that overlap.
    """
    if not isinstance(buyer_intent, dict):
        return {}
    compact: Dict[str, Any] = {}

    money_signal = buyer_intent.get("money_signal")
    if money_signal not in (None, "", {}, []):
        compact["money_signal"] = money_signal

    demand_signal = buyer_intent.get("demand_signal")
    if isinstance(demand_signal, dict) and demand_signal.get("state"):
        compact["demand_signal"] = {
            key: demand_signal.get(key)
            for key in ("state", "source", "velocity_band", "demand_band")
            if demand_signal.get(key) not in (None, "")
        }

    ads = buyer_intent.get("ads")
    if isinstance(ads, dict):
        known_ads = {
            key: value
            for key, value in ads.items()
            if key != "errors" and str(value or "").strip().lower() in {"yes", "no"}
        }
        if known_ads:
            compact["ads"] = known_ads
        # An unqualified absence of ad keys reads as "checked, found nothing".
        # Name the checks that never ran so the rubric can tell the difference.
        unimplemented = [
            str(error).split(":", 1)[0].strip()
            for error in ads.get("errors") or []
            if str(error).strip().endswith("unimplemented")
        ]
        if unimplemented:
            compact["ads_not_checked"] = sorted(set(unimplemented))

    for key in ("review_demand_signal", "review_profile"):
        value = buyer_intent.get(key)
        if isinstance(value, dict):
            band = value.get("demand_band")
            if band:
                compact["review_demand_band"] = band
            break

    velocity = buyer_intent.get("review_velocity")
    if isinstance(velocity, dict):
        compact["review_velocity"] = {
            key: velocity.get(key)
            for key in (
                "state",
                "velocity_band",
                "reviews_per_30d",
                "cohort_percentile",
                "source",
                "bound",
                "confidence",
            )
            if velocity.get(key) not in (None, "")
        }

    return compact


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
    selected_evidence_item_ids = {
        str(item.get("id") or "").strip()
        for item in selected_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

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
        "buyer_intent": _compact_buyer_intent(buyer_intent),
        "buyer_intent_summary": buyer_intent_summary(working),
        "money_signal": money_signal_label(working),
        "demand_signal": demand_signal_state(working).capitalize(),
        "safe_outreach_claims": working.get("safe_outreach_claims") or evidence.get("safe_outreach_claims") or [],
        "sendability_evidence": {
            "minimum_evidence_inputs": {
                "visible_pain_score": evidence.get("visible_pain_score", ""),
                "visible_pain_reasons": evidence.get("visible_pain_reasons") or [],
                "send_trigger": evidence.get("send_trigger") or "",
                "money_signal": money_signal_label(working),
                "demand_signal": demand_signal_state(working).capitalize(),
                "buyer_intent_summary": buyer_intent_summary(working),
            }
        },
        "screenshots": {
            "desktop": screenshots.get("desktop") or website.get("desktop_screenshot_path") or evidence.get("desktop_screenshot_path") or "",
            "mobile": screenshots.get("mobile") or website.get("mobile_screenshot_path") or evidence.get("mobile_screenshot_path") or "",
        },
        "visual_review": _visual_review_capsule(evidence, website),
        "selected_evidence_snippets": [*selected_text, *selected_items][:MAX_LLM_SCORING_SNIPPETS],
        "evidence_ref_options": _build_evidence_ref_options(
            working,
            lead=lead,
            selected_evidence_item_ids=selected_evidence_item_ids,
        ),
    }
    compact = _truncate_for_llm(capsule)
    if isinstance(compact, dict):
        _add_prompt_visible_ev_ref_options(compact)
    return compact


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
{SHARED_SENDABILITY_RULES}
{SHARED_COMMERCIAL_SCORING_RULES}
- Buyer intent is required for A/B quality: look for fixable gap times money signal, not just a bad website.
- Paid CRM/booking tools and known ad signals can support money intent.
- buyer_intent.money_signal is yes/no/unknown. "unknown" means there was no crawlable site text to read a paid stack from — it is an unanswered question, not evidence the business does not spend. Do not treat it as a negative.
- Bad site with money_signal "no" should not be an A lead. Bad site with money_signal "unknown" may still be an A lead when demand_signal is yes and the visible pain is concrete.
- buyer_intent.demand_signal is a separate claim from spend: it says the market is pulling on this business. Use it for demand, never as proof of budget.
- buyer_intent.ads_not_checked lists ad checks that are not implemented. Their "unknown" carries no information; do not read it as "no ads found".
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
{json.dumps(compact_records, ensure_ascii=False, separators=(",", ":"))}
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
    model: str = DEFAULT_LLM_SCORING_MODEL,
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
        },
        model=model,
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


def _projected_usd_for_prompt(
    prompt_tokens: int, max_tokens: int, *, model: str = DEFAULT_LLM_SCORING_MODEL
) -> float:
    return estimate_llm_usd(
        {"input_tokens": prompt_tokens, "output_tokens": max(0, max_tokens)}, model=model
    )


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
            "did not include text content",
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


def _accumulate_usage(base: Dict[str, Any] | None, extra: Dict[str, Any] | None) -> Dict[str, int]:
    """Sum two usage dicts so failed attempts are not dropped from cap accounting."""
    input_tokens = safe_int((base or {}).get("input_tokens")) + safe_int((extra or {}).get("input_tokens"))
    output_tokens = safe_int((base or {}).get("output_tokens")) + safe_int((extra or {}).get("output_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


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
    # Track usage/calls from attempts that raised so they still count toward caps.
    failed_usage: Dict[str, int] = {}
    # And which model burned it — the chain may have fallen past a $0
    # subscription runner onto a paid API, so `model` is the wrong price here.
    failed_model = ""
    calls_made = 0
    while True:
        try:
            calls_made += 1
            result = llm_client.score_batch(prompt, model=model, max_tokens=current_max_tokens)
        except llm_client.InvalidLlmResponse as exc:
            failed_usage = _accumulate_usage(failed_usage, getattr(exc, "usage", None))
            failed_model = str(getattr(exc, "model", "") or "").strip() or failed_model
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
                    calls_made += 1
                    repaired = llm_client.score_batch(repair_prompt, model=model, max_tokens=current_max_tokens)
                    repaired["usage"] = _accumulate_usage(failed_usage, repaired.get("usage"))
                    repaired["call_count"] = calls_made
                    repaired.setdefault("debug_notes", []).append(
                        f"validation repair succeeded for {batch_id}; raw response saved to {validation_debug_path}"
                    )
                    return repaired
                except llm_client.InvalidLlmResponse as repair_exc:
                    failed_usage = _accumulate_usage(failed_usage, getattr(repair_exc, "usage", None))
                    failed_model = str(getattr(repair_exc, "model", "") or "").strip() or failed_model
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
                            "usage": failed_usage,
                            "model": failed_model,
                            "call_count": calls_made,
                            "debug_notes": [
                                f"{fallback_note}; raw response saved to {validation_debug_path}; repair failed"
                            ],
                        }
                    err = _validation_failure_runtime_error(
                        batch_id=batch_id,
                        validation_failures=f"{validation_failures}\nRepair failures:\n{repair_exc}",
                        raw_debug_path=validation_debug_path,
                        repair_debug_path=repair_debug_path,
                    )
                    err.usage = failed_usage
                    err.model = failed_model
                    err.call_count = calls_made
                    raise err from repair_exc

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
            err = ValueError(f"{exc}; raw LLM response saved to {debug_path}")
            err.usage = failed_usage
            err.model = failed_model
            err.call_count = calls_made
            raise err from exc
        else:
            if failed_usage:
                result["usage"] = _accumulate_usage(failed_usage, result.get("usage"))
            result["call_count"] = calls_made
            return result


def estimate_api_llm_cost(
    records: List[Dict[str, Any]],
    *,
    fallback_vertical: str,
    batch_size: int = 5,
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
    resume_scored_keys: set[str] | None = None,
    model: str = DEFAULT_LLM_SCORING_MODEL,
) -> LlmCostEstimate:
    """
    Estimate API-LLM cost without making any Anthropic call.

    Selects records the same way a real run would (resume filtering), batches them
    exactly like ``score_records_api_run`` would at the configured batch size, and
    sums estimated prompt tokens plus the worst-case output tokens per batch.
    """
    unscored_records, _ = filter_unscored_records(
        records,
        existing_keys=resume_scored_keys or set(),
        fallback_vertical=fallback_vertical,
    )
    selected = [ensure_evidence(record) for record in unscored_records]
    configured_batch_size = max(1, batch_size)

    prompt_tokens = 0
    batches = 0
    for index in range(0, len(selected), configured_batch_size):
        chunk = selected[index : index + configured_batch_size]
        compact_records = [
            build_llm_scoring_record(record, fallback_vertical=fallback_vertical)
            for record in chunk
        ]
        batches += 1
        prompt = build_api_llm_scoring_prompt(compact_records, batch_id=f"batch_{batches:03d}")
        prompt_tokens += estimate_prompt_tokens(prompt)

    max_output_tokens = batches * max(0, max_tokens)
    projected_total_tokens = prompt_tokens + max_output_tokens
    projected_usd = estimate_llm_usd(
        {"input_tokens": prompt_tokens, "output_tokens": max_output_tokens}, model=model
    )
    return LlmCostEstimate(
        records_selected=len(selected),
        batches=batches,
        prompt_tokens=prompt_tokens,
        max_output_tokens=max_output_tokens,
        projected_total_tokens=projected_total_tokens,
        projected_usd=projected_usd,
    )


def scores_dir_has_artifacts(scores_dir: Path) -> bool:
    if not scores_dir.exists():
        return False
    if any(scores_dir.glob("*.json")):
        return True
    bundle_dir = scores_dir / "_api_bundles"
    return bundle_dir.exists() and any(bundle_dir.iterdir())


def clear_scores_dir(scores_dir: Path) -> None:
    """Remove score JSON files and the _api_bundles directory under scores_dir."""
    if not scores_dir.exists():
        return
    for path in scores_dir.glob("*.json"):
        if path.is_file():
            path.unlink()
    bundle_dir = scores_dir / "_api_bundles"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)


DEFAULT_LLM_SCORING_CONCURRENCY = 4


def resolve_llm_scoring_concurrency() -> int:
    """Batches in flight at once during API-LLM scoring.

    The safe ceiling is provider-specific and is NOT one number: the CLI runners
    (`codex`, `claude`) are bounded by local CPU/memory per subprocess, while
    `kimi` and `anthropic` are bounded by per-org tokens-per-minute limits. 4 is
    conservative for all four. Raise it only against a provider whose rate limit
    you have actually checked.
    """
    return max(1, getenv_int("LLM_SCORING_CONCURRENCY", DEFAULT_LLM_SCORING_CONCURRENCY))


@dataclass(frozen=True)
class _PlannedBatch:
    """One batch composed before any API call, so it can be submitted concurrently."""

    batch_id: str
    chunk: List[Dict[str, Any]]
    compact_records: List[Dict[str, Any]]
    prompt: str
    reserved_tokens: int
    reserved_usd: float


def _plan_api_llm_batches(
    selected: List[Dict[str, Any]],
    *,
    fallback_vertical: str,
    batch_size: int,
    projection_model: str,
    max_tokens: int,
    max_tokens_per_run: int,
    max_usd_per_run: float,
) -> Tuple[List[_PlannedBatch], List[ApiLlmSkip], str]:
    """Compose every batch up front, reserving worst-case tokens/spend per batch.

    The serial loop decides whether the *next* batch fits by adding its projection
    to the *actual* usage banked so far. Planning happens before any call, so it
    reserves `estimated_prompt_tokens + max_tokens` instead — the cost of exactly
    one call. That is a *ceiling on the first call*, not on the batch: a truncation
    retry re-sends the prompt with a doubled output allowance and a validation
    repair sends a larger prompt, so a batch can land at 2-3x its reservation.
    Planning is therefore the first line of defence only; the submission loop in
    `score_records_api_run` reconciles against banked actuals so a cap can still
    stop the run after planning is done.

    `projection_model` prices the reservation and must be
    `resolve_projection_model()` — the priciest model the chain could reach — not
    the caller's `model`. A `subscription:*` entry at the head of the chain is
    priced (0.0, 0.0), so pricing with the caller's model zeroes every reservation
    and `MAX_LLM_USD_PER_RUN` stops binding at all, while a fall-through to Kimi or
    the Claude API spends real money.
    """
    planned: List[_PlannedBatch] = []
    skipped: List[ApiLlmSkip] = []
    stop_reason = ""
    index = 0
    batch_index = 1
    reserved_tokens = 0
    reserved_usd = 0.0
    configured_batch_size = max(1, batch_size)

    while index < len(selected):
        current_batch_size = min(configured_batch_size, len(selected) - index)
        batch_id = f"batch_{batch_index:03d}"
        while current_batch_size > 0:
            chunk = selected[index : index + current_batch_size]
            compact_records = [
                build_llm_scoring_record(record, fallback_vertical=fallback_vertical)
                for record in chunk
            ]
            prompt = build_api_llm_scoring_prompt(compact_records, batch_id=batch_id)
            estimated_prompt_tokens = estimate_prompt_tokens(prompt)
            projected_total_tokens = estimated_prompt_tokens + max(0, max_tokens)
            projected_usd = _projected_usd_for_prompt(
                estimated_prompt_tokens, max_tokens, model=projection_model
            )

            chunk_exceeds_run_cap = max_tokens_per_run > 0 and projected_total_tokens > max_tokens_per_run
            remaining_exceeds_run_cap = (
                max_tokens_per_run > 0 and reserved_tokens + projected_total_tokens > max_tokens_per_run
            )
            remaining_exceeds_usd_cap = (
                max_usd_per_run > 0 and reserved_usd + projected_usd > max_usd_per_run
            )

            if (
                chunk_exceeds_run_cap or remaining_exceeds_run_cap or remaining_exceeds_usd_cap
            ) and current_batch_size > 1:
                current_batch_size = max(1, current_batch_size // 2)
                continue

            if chunk_exceeds_run_cap:
                skipped_record = ApiLlmSkip(
                    lead_name=_lead_name_for_skip(chunk[0], fallback_vertical=fallback_vertical),
                    score_file=score_file_for_record(chunk[0], fallback_vertical=fallback_vertical),
                    reason="single_record_exceeds_token_cap",
                )
                skipped.append(skipped_record)
                print(f"[LLM SKIP] {skipped_record.score_file}: {skipped_record.reason}")
                index += 1
                break

            if remaining_exceeds_run_cap:
                return planned, skipped, "MAX_LLM_TOKENS_PER_RUN"
            if remaining_exceeds_usd_cap:
                return planned, skipped, "MAX_LLM_USD_PER_RUN"

            planned.append(
                _PlannedBatch(
                    batch_id=batch_id,
                    chunk=chunk,
                    compact_records=compact_records,
                    prompt=prompt,
                    reserved_tokens=projected_total_tokens,
                    reserved_usd=projected_usd,
                )
            )
            reserved_tokens += projected_total_tokens
            reserved_usd += projected_usd
            index += len(chunk)
            batch_index += 1
            break

    return planned, skipped, stop_reason


def score_records_api_run(
    records: List[Dict[str, Any]],
    *,
    scores_dir: Path,
    fallback_vertical: str,
    batch_size: int = 5,
    model: str = DEFAULT_LLM_SCORING_MODEL,
    projection_model: str = "",
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
    max_calls: int = DEFAULT_MAX_LLM_CALLS_PER_RUN,
    max_tokens_per_run: int = DEFAULT_MAX_LLM_TOKENS_PER_RUN,
    max_usd_per_run: float = DEFAULT_MAX_LLM_USD_PER_RUN,
    resume_scored_keys: set[str] | None = None,
    usage_output_path: Path | None = None,
    concurrency: int = 1,
) -> ApiLlmRun:
    """Score `records` through the API-LLM chain, bounded by the per-run caps.

    `projection_model` is the model the *reservations* are priced with — pass
    `resolve_projection_model()`, the priciest model the chain could reach. It
    defaults to `model` only so existing callers keep working; a caller running a
    `subscription:*`-first chain that leaves it unset gets a $0 reservation and no
    working `MAX_LLM_USD_PER_RUN`.
    """
    unscored_records, skipped_already_scored = filter_unscored_records(
        records,
        existing_keys=resume_scored_keys or set(),
        fallback_vertical=fallback_vertical,
    )
    reservation_model = str(projection_model or "").strip() or model
    selected = [ensure_evidence(record) for record in unscored_records]
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

    def _write_partial_usage() -> None:
        if usage_output_path is None:
            return
        _write_llm_usage_payload(
            usage_output_path,
            _llm_usage_payload(
                calls=calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=tokens,
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
                partial=True,
            ),
        )

    def _bank_batch(result: Dict[str, Any], *, batch_id: str, chunk: List[Dict[str, Any]]) -> None:
        """Merge one completed batch. Called on the calling thread, in submit order."""
        nonlocal calls, input_tokens, output_tokens, tokens, usd
        served_model = resolve_served_model(result, model)
        runner_model = result.get("runner_model")
        runner_model = runner_model.strip() if isinstance(runner_model, str) else ""
        print(
            f"[LLM PROVIDER] batch={batch_id} provider={result.get('provider', 'unknown')} "
            f"model={served_model}" + (f" runner_model={runner_model}" if runner_model else "")
        )
        bundle = result["bundle"]
        usage = result.get("usage") or {}
        calls += safe_int(result.get("call_count")) or 1
        debug_notes.extend(str(note) for note in result.get("debug_notes") or [] if str(note))
        batch_input_tokens = safe_int(usage.get("input_tokens"))
        batch_output_tokens = safe_int(usage.get("output_tokens"))
        batch_total_tokens = (
            safe_int(usage.get("total_tokens")) or batch_input_tokens + batch_output_tokens
        )
        input_tokens += batch_input_tokens
        output_tokens += batch_output_tokens
        tokens += batch_total_tokens
        usd += estimate_llm_usd(usage, model=served_model)

        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / f"{batch_id}_response.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        import_score_bundle(bundle_path, scores_dir)
        _write_partial_usage()

    def _bank_failed_batch(exc: BaseException) -> None:
        nonlocal calls, input_tokens, output_tokens, tokens, usd
        usage = getattr(exc, "usage", None) or {}
        calls += safe_int(getattr(exc, "call_count", 0))
        batch_input_tokens = safe_int(usage.get("input_tokens"))
        batch_output_tokens = safe_int(usage.get("output_tokens"))
        batch_total_tokens = (
            safe_int(usage.get("total_tokens")) or batch_input_tokens + batch_output_tokens
        )
        input_tokens += batch_input_tokens
        output_tokens += batch_output_tokens
        tokens += batch_total_tokens
        usd += estimate_llm_usd(usage, model=str(getattr(exc, "model", "") or "").strip() or model)
        _write_partial_usage()

    def _record_batch_failure(
        exc: BaseException, *, batch_id: str, chunk: List[Dict[str, Any]]
    ) -> None:
        """Bound a batch failure to its own batch instead of aborting the run.

        A batch can come back unparseable after both the truncation retry and the
        validation-repair call have been spent — 2026-08-06 saw kimi-k3 emit a
        `safe_outreach_claims` entry with no `"claim":` key, a JSON syntax error
        that raises before schema validation and so reaches neither path. Letting
        that propagate discarded 24 already-banked batches and wrote no CSV at
        all. Treat it like a cap stop instead: charge the usage, name the leads in
        `skipped_records`, and let the run finish with what it did score.
        """
        print(f"[LLM BATCH FAILED] batch={batch_id} leads={len(chunk)} error={exc}")
        debug_notes.append(f"batch {batch_id} failed and its leads were skipped: {exc}")
        for record in chunk:
            skipped_records.append(
                ApiLlmSkip(
                    lead_name=_lead_name_for_skip(record, fallback_vertical=fallback_vertical),
                    score_file=score_file_for_record(record, fallback_vertical=fallback_vertical),
                    reason="batch_failed",
                )
            )

    if concurrency > 1 and selected:
        # Concurrent path. Batches are composed up front against *reserved*
        # worst-case usage (see _plan_api_llm_batches), then fed to a bounded pool
        # through the sliding window below.
        planned, planned_skips, plan_stop_reason = _plan_api_llm_batches(
            selected,
            fallback_vertical=fallback_vertical,
            batch_size=configured_batch_size,
            projection_model=reservation_model,
            max_tokens=max_tokens,
            max_tokens_per_run=max_tokens_per_run,
            max_usd_per_run=max_usd_per_run,
        )
        skipped_records.extend(planned_skips)
        if plan_stop_reason:
            stopped_by_cap = True
            stop_reason = plan_stop_reason
            print(f"[STOP] Hit {stop_reason}")

        for planned_batch in planned:
            for record, compact in zip(planned_batch.chunk, planned_batch.compact_records):
                lead_id = lead_id_for_record(record, fallback_vertical=fallback_vertical)
                api_evidence_ref_ids_by_lead_id[lead_id] = compact_evidence_ref_ids(compact)

        # Submission and collection interleave through a sliding window rather
        # than "submit everything, then collect everything". Submitting the whole
        # plan up front left no point at which a cap could still stop the run: the
        # only gate was the planned-batch *index* against `max_calls`, which
        # counts batches while the bill is in calls, and the reservations behind
        # it cover a single call per batch while a batch can spend three.
        #
        # The window keeps at most `concurrency` batches in flight and re-checks
        # `_hit_llm_cap` against banked actuals before every submission, charging
        # each in-flight batch at its worst case (MAX_LLM_CALLS_PER_BATCH x its
        # reservation) because it has banked nothing yet. With nothing in flight
        # that reduces to exactly the serial check, so the two paths stop at the
        # same batch whenever the cap trips on a drained window.
        #
        # Residual, and it is inherent to running calls in parallel: batches
        # already in flight when the cap trips are paid for and are still banked,
        # so a token/USD cap can be overshot by their actual usage. Serial can
        # overshoot by one batch; concurrent by up to `concurrency`.
        pool = ThreadPoolExecutor(max_workers=concurrency)
        in_flight: Deque[Tuple[_PlannedBatch, Any]] = deque()
        in_flight_tokens = 0
        in_flight_usd = 0.0
        next_position = 0
        submission_stopped = False
        try:
            while True:
                while (
                    not submission_stopped
                    and next_position < len(planned)
                    and len(in_flight) < concurrency
                ):
                    cap_reason = _hit_llm_cap(
                        calls=calls + MAX_LLM_CALLS_PER_BATCH * len(in_flight),
                        tokens=tokens + MAX_LLM_CALLS_PER_BATCH * in_flight_tokens,
                        usd=usd + MAX_LLM_CALLS_PER_BATCH * in_flight_usd,
                        max_calls=max_calls,
                        max_tokens_per_run=max_tokens_per_run,
                        max_usd_per_run=max_usd_per_run,
                    )
                    if cap_reason:
                        # With work still in flight this is a throttle, not a
                        # verdict — the reservations being charged may turn out to
                        # cost far less. Drain first and re-decide on real numbers.
                        if in_flight:
                            break
                        submission_stopped = True
                        stopped_by_cap = True
                        stop_reason = cap_reason
                        print(f"[STOP] Hit {stop_reason}")
                        break
                    planned_batch = planned[next_position]
                    next_position += 1
                    in_flight_tokens += planned_batch.reserved_tokens
                    in_flight_usd += planned_batch.reserved_usd
                    in_flight.append(
                        (
                            planned_batch,
                            pool.submit(
                                _score_batch_with_debug,
                                planned_batch.prompt,
                                batch_id=planned_batch.batch_id,
                                bundle_dir=bundle_dir,
                                compact_records=planned_batch.compact_records,
                                model=model,
                                max_tokens=max_tokens,
                            ),
                        )
                    )

                if not in_flight:
                    break

                # Merged strictly in submit order — completion order would change
                # which bundle wins a score_file collision and would renumber batch
                # ids relative to a serial run.
                planned_batch, future = in_flight.popleft()
                in_flight_tokens -= planned_batch.reserved_tokens
                in_flight_usd -= planned_batch.reserved_usd
                try:
                    result = future.result()
                except Exception as exc:
                    _bank_failed_batch(exc)
                    _record_batch_failure(
                        exc, batch_id=planned_batch.batch_id, chunk=planned_batch.chunk
                    )
                    continue
                _bank_batch(result, batch_id=planned_batch.batch_id, chunk=planned_batch.chunk)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Serial path — unchanged, and still the default (`concurrency` == 1) so every
    # existing caller keeps byte-identical batch composition and cap semantics.
    while concurrency <= 1 and index < len(selected):
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
            projected_usd = _projected_usd_for_prompt(estimated_prompt_tokens, max_tokens, model=model)
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

        try:
            result = _score_batch_with_debug(
                prompt,
                batch_id=batch_id,
                bundle_dir=bundle_dir,
                compact_records=compact_records,
                model=model,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            # Same containment as the concurrent path: charge what the failed
            # attempt burned, record the leads it cost us, and move to the next
            # batch rather than discarding every batch already banked.
            _bank_failed_batch(exc)
            _record_batch_failure(exc, batch_id=batch_id, chunk=chunk)
            index += len(chunk)
            batch_index += 1
            continue
        # Price by the model that actually served the batch, not the run-level
        # one. With a subscription runner first in the chain, `model` is
        # "subscription:<runner>" at $0/$0 — so a fallback to a paid API would
        # bill every token at zero and MAX_LLM_USD_PER_RUN would never trip.
        served_model = resolve_served_model(result, model)
        runner_model = result.get("runner_model")
        runner_model = runner_model.strip() if isinstance(runner_model, str) else ""
        print(
            f"[LLM PROVIDER] provider={result.get('provider', 'unknown')} model={served_model}"
            + (f" runner_model={runner_model}" if runner_model else "")
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
        usd += estimate_llm_usd(usage, model=served_model)

        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / f"{batch_id}_response.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        import_score_bundle(bundle_path, scores_dir)
        index += len(chunk)
        batch_index += 1

        if usage_output_path is not None:
            # Write usage metadata after every successful call so dashboard spend
            # stays visible locally even if a later merge/validation step crashes.
            _write_llm_usage_payload(
                usage_output_path,
                _llm_usage_payload(
                    calls=calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=tokens,
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
                    partial=True,
                ),
            )

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
        skipped_already_scored=skipped_already_scored,
        new_records_selected=len(selected),
    )


def _llm_usage_payload(
    *,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    usd: float,
    caps: Dict[str, int | float],
    stopped_by_cap: bool,
    stop_reason: str,
    skipped_records: List[ApiLlmSkip] | None,
    debug_notes: List[str] | None,
    partial: bool = False,
) -> Dict[str, Any]:
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_usd": usd,
        "caps": caps,
        "stopped_by_cap": stopped_by_cap,
        "stop_reason": stop_reason,
        "skipped_records": [
            {"lead_name": skipped.lead_name, "score_file": skipped.score_file, "reason": skipped.reason}
            for skipped in (skipped_records or [])
        ],
        "debug_notes": debug_notes or [],
        "partial": partial,
    }


def _write_llm_usage_payload(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_llm_usage(path: Path, run: ApiLlmRun) -> None:
    payload = _llm_usage_payload(
        calls=run.calls,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        total_tokens=run.tokens,
        usd=run.usd,
        caps=run.caps,
        stopped_by_cap=run.stopped_by_cap,
        stop_reason=run.stop_reason,
        skipped_records=run.skipped_records,
        debug_notes=run.debug_notes,
        partial=False,
    )
    _write_llm_usage_payload(path, payload)


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
    raise_csv_field_limit()
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
    parser.add_argument(
        "--resume-scored",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="API LLM mode: skip records already present in the output CSV (default: true for api-llm)",
    )
    parser.add_argument("--offset", type=int, default=0, help="Optional input record offset before scoring")
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
    parser.add_argument(
        "--dry-run-llm-cost",
        action="store_true",
        help="API LLM mode: estimate batches/tokens/cost and exit before any Anthropic call",
    )
    parser.add_argument(
        "--confirm-llm-spend",
        action="store_true",
        help="API LLM mode: confirm projected spend above the confirmation threshold",
    )
    parser.add_argument(
        "--fresh-scores-dir",
        action="store_true",
        help="API LLM mode: clear old score JSON and _api_bundles under scores-dir before scoring",
    )
    parser.add_argument(
        "--allow-stale-scores-dir",
        action="store_true",
        help="API LLM mode: allow scoring into a scores-dir that already has files with an empty output CSV",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(input_path)

    records = [ensure_evidence(record) for record in read_jsonl(input_path)]
    if args.offset > 0:
        records = records[args.offset :]

    # Drop suppressed leads before anything is spent on them. Two checks, and
    # both are needed: the stored flag covers businesses already suppressed when
    # they were enriched, and the live index covers the ordinary case where the
    # opt-out arrived after enrichment and the flag on disk is a stale "clean".
    # merge_suppression_disqualifiers still backstops this at the sendability
    # gate, but in api-llm mode that happens after the tokens are paid for.
    try:
        suppression_index = load_configured_suppression_index()
    except ComplianceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    def _suppressed(record: Dict[str, Any]) -> bool:
        return is_suppressed_record(record) or record_matches_suppression(record, suppression_index)

    kept = [record for record in records if not _suppressed(record)]
    if len(kept) != len(records):
        print(f"[SKIP] Suppressed leads dropped before scoring: {len(records) - len(kept)}")
        records = kept

    existing_scored_rows: List[Dict[str, Any]] = []
    append_existing_scored_rows = False

    if args.mode == "manual-llm":
        if args.limit > 0:
            records = records[: args.limit]
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
        resume_scored = True if args.resume_scored is None else bool(args.resume_scored)
        existing_scored_keys: set[str] = set()
        skipped_already_scored = 0
        if resume_scored and output_path.exists():
            existing_scored_rows = read_csv_rows(output_path)
            existing_scored_keys = stable_lead_keys_from_rows(existing_scored_rows)
            records, skipped_already_scored = filter_unscored_records(
                records,
                existing_keys=existing_scored_keys,
                fallback_vertical=args.vertical,
            )
            append_existing_scored_rows = bool(existing_scored_rows)
        if args.limit > 0:
            records = records[: args.limit]
        if resume_scored:
            print(f"[RESUME] Existing scored rows: {len(existing_scored_rows)}")
            print(f"[RESUME] Skipping already scored: {skipped_already_scored}")
            print(f"[RESUME] New records selected: {len(records)}")
        max_calls = getenv_int("MAX_LLM_CALLS_PER_RUN", DEFAULT_MAX_LLM_CALLS_PER_RUN)
        max_tokens_per_run = getenv_int("MAX_LLM_TOKENS_PER_RUN", DEFAULT_MAX_LLM_TOKENS_PER_RUN)
        max_usd_per_run = getenv_float("MAX_LLM_USD_PER_RUN", DEFAULT_MAX_LLM_USD_PER_RUN)

        llm_model = llm_client.resolve_scoring_model()
        # Project against the priciest tier the chain can fall through to, not
        # the first one — otherwise a $0 subscription runner in front zeroes the
        # projection and disables the LLM_SPEND_CONFIRM_USD refusal below.
        projection_model = resolve_projection_model()
        try:
            estimate = estimate_api_llm_cost(
                records,
                fallback_vertical=args.vertical,
                batch_size=args.llm_batch_size,
                max_tokens=args.llm_max_tokens,
                model=projection_model,
            )
        except UnknownLlmModelError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

        print(
            f"[LLM COST] model={llm_model}"
            + (f" projected_as={projection_model}" if projection_model != llm_model else "")
        )
        print(
            f"[LLM COST] records_selected={estimate.records_selected} "
            f"batches={estimate.batches} "
            f"prompt_tokens={estimate.prompt_tokens} "
            f"max_output_tokens={estimate.max_output_tokens} "
            f"projected_total_tokens={estimate.projected_total_tokens} "
            f"projected_usd={estimate.projected_usd:.4f}"
        )

        if args.dry_run_llm_cost:
            print("[DRY-RUN] No LLM API call made.")
            return 0

        scores_dir_path = Path(args.scores_dir)
        existing_csv_row_count = len(read_csv_rows(output_path)) if output_path.exists() else 0
        if args.fresh_scores_dir:
            clear_scores_dir(scores_dir_path)
            print(f"[FRESH] Cleared score JSON and _api_bundles under {scores_dir_path}")
        elif (
            scores_dir_has_artifacts(scores_dir_path)
            and existing_csv_row_count == 0
            and not args.allow_stale_scores_dir
        ):
            print(
                f"[REFUSE] {scores_dir_path} already contains score artifacts but the output CSV has 0 rows. "
                "Re-run with --fresh-scores-dir to clear them or --allow-stale-scores-dir to keep them."
            )
            return 1

        confirm_threshold = getenv_float("LLM_SPEND_CONFIRM_USD", DEFAULT_LLM_SPEND_CONFIRM_USD)
        if max_usd_per_run > 0 and estimate.projected_usd > max_usd_per_run:
            print(
                f"[REFUSE] Projected LLM cost ${estimate.projected_usd:.4f} exceeds "
                f"MAX_LLM_USD_PER_RUN=${max_usd_per_run:.2f}. Lower the batch/limit or raise the cap."
            )
            return 1
        if estimate.projected_usd > confirm_threshold and not args.confirm_llm_spend:
            print(
                f"[REFUSE] Projected LLM cost ${estimate.projected_usd:.4f} exceeds "
                f"${confirm_threshold:.2f}. Re-run with --confirm-llm-spend to proceed."
            )
            return 1

        api_run = score_records_api_run(
            records,
            scores_dir=scores_dir_path,
            fallback_vertical=args.vertical,
            batch_size=args.llm_batch_size,
            model=llm_model,
            # Reservations price against the priciest tier the chain can fall
            # through to, for the same reason the estimate above does.
            projection_model=projection_model,
            max_tokens=args.llm_max_tokens,
            max_calls=max_calls,
            max_tokens_per_run=max_tokens_per_run,
            max_usd_per_run=max_usd_per_run,
            usage_output_path=Path(args.llm_usage_output) if args.llm_usage_output else None,
            concurrency=resolve_llm_scoring_concurrency(),
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
        if args.limit > 0:
            records = records[: args.limit]
        scored = [score_record(record, args.vertical) for record in records]

    scored.sort(key=sort_key)
    if append_existing_scored_rows:
        scored = [*existing_scored_rows, *scored]

    write_csv(output_path, scored)

    if args.cards_output:
        cards = [build_lead_card(row) for row in scored]
        write_jsonl(Path(args.cards_output), cards)

    counts: Dict[str, int] = {}
    packets = 0

    for row in scored:
        priority = str(row.get("Lead Priority", ""))
        counts[priority] = counts.get(priority, 0) + 1
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
            f'{row.get("Review Demand Band", "") or row.get("Review Fit Band", "")}'
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
