#!/usr/bin/env python3
"""
Phase 6: website enrichment.

Reads raw Google Places CSV.
Visits websites.
Scrapes homepage/contact/services/about/gallery text.
Extracts emails, plausible phones, social links, forms, CTAs, high-ticket terms.
Takes desktop/mobile screenshots with Playwright.
Writes enriched JSONL.

Examples:
    python enrich_leads.py --input output/raw_places_hardscaping.csv --output output/enriched_hardscaping.jsonl
    python enrich_leads.py --input output/raw_places_roofing.csv --output output/enriched_roofing.jsonl
    python enrich_leads.py --input output/raw_places_hardscaping.csv --output output/enriched_hardscaping_test.jsonl --limit 3
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import warnings
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import AbstractSet, Any, Dict, Iterable, List, Tuple
from urllib.parse import urljoin, urlparse

import requests
import tldextract
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from lead_machine.buyer_signals import build_buyer_intent
from lead_machine.email_hygiene import clean_emails
from lead_machine.evidence import (  # re-exported: the crawl and the evidence profile grade on one scale
    ESTIMATE_CTA_TERMS,
    STRENGTH_RANK,
    VISUAL_AUDIT_SCORE_FIELDS,
    VISUAL_UNKNOWN,
    apply_visual_audit_scores,
    build_evidence_profile,
)
from lead_machine.icp import hard_icp_gate
from lead_machine.schema import normalize_lead
from lead_machine.scoring_profiles import infer_market, infer_vertical_key
from lead_machine.verticals import resolve_profile
from lead_machine.review_velocity import ReviewSnapshotStore
from lead_machine.compliance import (  # re-exported: the enrichment entry points share these
    DNC_SUPPRESSED_REASON,
    ComplianceError,
    has_suppression_match,
    load_configured_suppression_index,
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 15
HOMEPAGE_FETCH_TIMEOUT = 12
CANDIDATE_PAGE_FETCH_TIMEOUT = 8
SLOW_ENRICH_SECONDS = 30.0
# How long a rendered page is given to go quiet, and what it gets when it never
# does. The old behaviour was an unconditional 1.5s sleep after
# `domcontentloaded`, paid twice per lead by pages that had already finished
# loading. The ceiling here is deliberately that same 1.5s: waiting for network
# idle *up to* the old sleep length is strictly cheaper -- a quiet page returns
# in a fraction of it, and a site with a chat or ad widget that never idles
# spends exactly what it used to, not more. (Measured: a 2.5s ceiling plus a
# fallback made the never-idle tail up to 6s/lead *slower* than the sleep it
# replaced.) The fallback is for a load state that fails to report at all,
# which is not the same as one that reports "still busy".
RENDER_SETTLE_TIMEOUT_MS = 1500
RENDER_SETTLE_FALLBACK_MS = 500
# `detect_rendered_form` reads the DOM rather than screenshotting it, and it is
# only called when the static crawl found no form -- so a shorter look is a
# look that can report "no form" about a site whose form is injected late, and
# absence is what `score_online_weakness` charges pain for (+20 no estimate
# form, +18/+12 weak/basic). That path keeps the full 1.5s window it always
# had; network idle may not cut it short.
RENDER_FORM_SETTLE_FLOOR_MS = 1500
PER_LEAD_ENRICH_TIMEOUT_SECONDS = 45.0
# A single lead may never stall a run for longer than this, however many
# workers are running.
MAX_PER_LEAD_ENRICH_TIMEOUT_SECONDS = 180.0
TLD_EXTRACTOR = tldextract.TLDExtract(cache_dir=False, suffix_list_urls=())
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?"
    r"(?:\(?\d{3}\)?[\s.\-]?)"
    r"\d{3}[\s.\-]?\d{4}"
)

SOCIAL_DOMAINS = [
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
]

CTA_TERMS = [
    "free estimate",
    "request estimate",
    "get estimate",
    "get a free estimate",
    "get a quote",
    "request a quote",
    "free quote",
    "contact us",
    "call today",
    "schedule",
    "book",
    "start now",
    "get started",
    "learn more",
    "call now",
    "submit",
]

CONTACT_ESTIMATE_PATHS = [
    "/contact",
    "/contact-us",
    "/request-estimate",
    "/free-estimate",
    "/get-a-quote",
    "/quote",
    "/estimate",
    "/schedule",
    "/request-a-quote",
    "/request-service",
    "/get-estimate",
]

# The keyword groups one lead probes: (keywords, page role, per-group page
# cap, what counts as having found what the group is for). 0 = every match;
# the contact group is the one worth going deep on.
#
# `needs_form` is the important column. A contact page that merely *loads* has
# not answered the question the contact group exists to ask, and stopping
# there reports "no form detected" about a site whose form sits one unlinked
# URL away. That matters because absence is not neutral downstream —
# `score_leads.score_online_weakness` sells the pain, so `no_form_detected`
# is worth +8 and pushes a lead *past* sendability's visible-pain floor.
CANDIDATE_PAGE_GROUPS: List[Tuple[List[str], str, int, bool]] = [
    (["contact", "estimate", "quote", "schedule", "request"], "contact", 0, True),
    (["service"], "services", 1, False),
    (["about"], "about", 1, False),
    (["gallery"], "gallery", 1, False),
]

# Nothing but the wall-clock timeout used to bound a lead's crawl, so a
# link-heavy site blew the per-lead budget and came back with a truncated
# crawl plus a false manual_visual_review_needed.
#
# This is a backstop against the pathological shape — a nav and footer with
# thirty contact URLs — not a trim on the fallback ladder. It has to leave
# room for a group that found nothing to run its guesses out: the contact
# ladder is eleven paths, plus one probe each for services, about and
# gallery. Cutting below that would report "no form detected" on a site whose
# form we simply stopped looking for, and downstream charges +8 for that.
# A site that answers on its linked pages still finishes in four or five.
MAX_CANDIDATE_PAGE_FETCHES = 16

# A crawl stopped by the ceiling is not a site that had nothing. Downstream
# infers pain from absence — a missing services page is +5 and corroborates
# two more +8s — so a truncated crawl has to be distinguishable from a
# complete one, the way the timeout path already is.
CANDIDATE_BUDGET_FLAG = "candidate_page_budget_exhausted"

# Same idea, different cause: the server said 200 and handed us a document with
# nothing in `<body>`. Like the budget flag this describes our crawl, not the
# prospect's business, so it is carried in `evidence.STATIC_FETCH_REVIEW_FLAGS`
# rather than in any list that grades a site down.
EMPTY_BODY_FLAG = "empty_body_on_200"

# `form_detection_method` is written on every lead and "none" already meant
# three different things — parsed and genuinely formless, body empty, fetch
# failed. Only the middle one is recoverable by a rendered pass, and only the
# middle one makes an absence claim unsupported, so it gets its own name.
NO_BODY_FORM_METHOD = "no_body"

FORM_PROVIDER_TERMS = [
    "hubspot",
    "jobber",
    "jotform",
    "typeform",
    "gravity forms",
    "gravityforms",
    "gravity form",
    "gform_wrapper",
    "wpforms",
    "formstack",
    "squarespace form",
    "sqs-block-form",
    "wix form",
    "wixforms",
    "captcha",
    "recaptcha",
    "g-recaptcha",
    "servicetitan",
    "service titan",
    "thryv",
    "urable",
    "booksy",
    "square appointments",
    "squareup.com/appointments",
    "mobile tech rx",
    "mobiletechrx",
]

CAPTCHA_TERMS = {"captcha", "recaptcha", "g-recaptcha"}
# One ladder, defined once in lead_machine.evidence: the crawl writes these
# strengths and the evidence profile grades the visual fields on the same rungs,
# so a copy here is a drift waiting to happen.
FORM_STRENGTH_RANK = STRENGTH_RANK

# Markup that dates a page on its own. A visual score built on these has to
# rest on signals no modern template emits by accident, because nothing here
# ever looks at the rendered page.
LEGACY_MARKUP_TAGS = ["font", "center", "marquee", "blink", "frameset", "frame"]

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
    "storm damage",
    "siding",
    "gutter",
    "insurance claim",
    "commercial roofing",
]

GENERIC_COPY_TERMS = [
    "transform your outdoor space",
    "professional solutions",
    "high quality service",
    "trusted experts",
    "we pride ourselves",
    "your dream",
    "bringing your vision to life",
    "quality workmanship",
    "customer satisfaction",
]

NON_STANDARD_SITE_HINTS = [
    "wixsite.com",
    "godaddysites.com",
    "weebly.com",
    "sites.google.com",
    "business.site",
    "clienthub.getjobber.com",
    "facebook.com",
    "instagram.com",
]


DEFAULT_ENRICH_WORKERS = 6
DEFAULT_BROWSER_ENRICH_WORKERS = 5
# How many of one lead's candidate pages are in flight at once. Distinct from
# the worker counts above, which are leads-at-a-time: this is the width *inside*
# a lead. Serial by default, for the measured reason in
# resolve_page_fetch_workers -- not because the concurrency does not work.
DEFAULT_PAGE_FETCH_WORKERS = 1
# How long a worker is given to reach the browser-shutdown rendezvous at the
# end of a run. Only reached when the pool never grew to its full width.
BROWSER_SHUTDOWN_RENDEZVOUS_SECONDS = 5.0


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def getenv_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def candidate_page_budget_floor() -> int:
    """Below this the fallback ladder cannot run, and pain is manufactured."""
    return len(CONTACT_ESTIMATE_PATHS) + len(CANDIDATE_PAGE_GROUPS) - 1


def resolve_candidate_page_budget() -> int:
    floor = candidate_page_budget_floor()
    requested = getenv_int("MAX_CANDIDATE_PAGE_FETCHES", MAX_CANDIDATE_PAGE_FETCHES)
    if requested < floor:
        # Not a tuning knob below the ladder: a short budget reports "no form
        # detected" on sites we simply stopped looking at, and downstream
        # charges pain for that on every lead in the run.
        print(
            f"[WARN] MAX_CANDIDATE_PAGE_FETCHES={requested} is below the fallback ladder; using {floor}",
            file=sys.stderr,
        )
        return floor
    return requested


def resolve_page_fetch_workers() -> int:
    """How many of one lead's candidate pages to fetch at once.

    The pages a lead crawls have no ordering dependency on each other, so
    fetching them together is free correctness-wise and was expected to be the
    next big win: serially a lead's wall clock is the sum of ~7 round trips,
    each with an 8s ceiling. It does work — and it is still shipped off,
    because of *where* it runs. Measured on 20-50 real epoxy leads, alternating
    widths so network drift could not pose as an effect:

        path (leads at once)          width 1        width 5       verdict
        idle box (1)             67.6/61.1/59.5  44.9/44.9/42.9   1.42x win
        browser, shipped (5)        90.7/99.3       94.2/94.7     flat
        network, shipped (6)             inconclusive: noise > effect

    The win is real and the flat result is not a contradiction — both follow
    from the same thing. Page concurrency can only pay by filling network
    waits, and both shipped paths already crawl five or six *leads* at once,
    which fills them already. On an idle box nothing else is competing, so the
    same change is worth 1.42x. What decides it is total in-flight fetches
    (leads x pages), and at the shipped defaults that product is already past
    the knee: the browser path bought 0.5% for ~320 MB more peak PSS, on a box
    where Phase 3 already measured 3.9 GB.

    So this is a knob, not a default. Turn it up for a run whose lead-level
    concurrency cannot saturate the box — a handful of leads, a topup, one
    lead being debugged — where it is worth about 1.4x.

    Fidelity does not depend on the width and was never the question in doubt:
    every width from 1 to 8 produced the same pages, the same 7.37 fetches per
    lead and the same graded fields, on both paths.
    """
    return max(1, getenv_int("PAGE_FETCH_WORKERS", DEFAULT_PAGE_FETCH_WORKERS))


def has_terminal_form(form_signals: Dict[str, Any]) -> bool:
    """Has the crawl found what downstream treats as a finished answer?

    `evidence._derive_visible_pain_profile` only stops charging pain at
    `strong_estimate_form` — estimate form detected *and* Strong. Anything
    less still scores (+20 for no estimate form, +18 weak, +12 basic), so a
    weaker find does not mean another page could not improve it.

    `forms_found` is emphatically not this bar: `detect_form_signals` grades
    any page carrying a `<button>`, `<input>` or `<iframe>` as Weak rather
    than None, so a hamburger menu would satisfy it.
    """
    return (
        str(form_signals.get("estimate_form_detected") or "").strip().lower() == "yes"
        and str(form_signals.get("form_strength") or "").strip().lower() == "strong"
    )


def elapsed_seconds(started_at: float) -> float:
    return max(0.0, time.monotonic() - started_at)


def format_elapsed(seconds: float) -> str:
    return f"{seconds:.1f}"


def short_error(text: Any, max_chars: int = 120) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def enrichment_log_fields(record: Dict[str, Any], *, fallback_error: str = "") -> Dict[str, Any]:
    website = record.get("website") or {}
    pages = website.get("pages") if isinstance(website, dict) else {}
    homepage = website.get("homepage") if isinstance(website, dict) else {}
    enrichment = record.get("enrichment") or {}

    error = (
        fallback_error
        or website.get("enrichment_error", "")
        or website.get("error", "")
        or (homepage.get("error", "") if isinstance(homepage, dict) else "")
        or enrichment.get("error", "")
    )
    return {
        "site_status": website.get("site_status", "Unknown") if isinstance(website, dict) else "Unknown",
        "pages_checked": len(pages) if isinstance(pages, dict) else 0,
        "error": short_error(error),
    }


def safe_slug(text: str, fallback: str = "lead") -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:80] or fallback


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def domain_of(url: str) -> str:
    if not url:
        return ""
    parsed = TLD_EXTRACTOR(url)
    if parsed.domain and parsed.suffix:
        return f"{parsed.domain}.{parsed.suffix}".lower()
    return urlparse(url).netloc.lower().replace("www.", "")


def is_social_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "facebook.com" in lowered or "instagram.com" in lowered


def is_non_standard_site_or_booking_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(hint in lowered for hint in NON_STANDARD_SITE_HINTS)


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def clean_text(text: str, max_chars: int = 12000) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:max_chars]


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return clean_text(soup.get_text(" ", strip=True))


def extract_visible_body_text(html: str) -> str:
    """The visible text of `<body>` alone — nothing from `<head>`.

    `extract_visible_text` above runs `get_text()` over the whole document and
    decomposes only `script/style/noscript/svg`. `<title>` is neither, and it
    lives in `<head>`, so a React shell that renders nothing still comes back
    with its title. That is the one document we must be able to recognise: a
    page whose body we never read is not a page with no form on it, and a
    whole-document reading cannot tell the two apart.

    Returns `""` for a document with no `<body>` at all — a 0-byte HTTP 200
    from a parked domain is `ok` as far as `fetch_url` is concerned, and this
    is the only place that notices.

    Deliberately structural, with no length threshold: the corpus holds real
    parsed bodies of 76, 94 and 225 characters, and any cutoff long enough to
    catch a title also throws those away.
    """
    soup = BeautifulSoup(html or "", "lxml")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    body = soup.body
    if body is None:
        return ""

    return clean_text(body.get_text(" ", strip=True))


def extract_title_meta(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html or "", "lxml")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    meta = ""
    meta_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta_tag and meta_tag.get("content"):
        meta = meta_tag["content"].strip()

    return clean_text(title, 300), clean_text(meta, 700)


def extract_page_structure(html: str) -> Dict[str, Any]:
    """Cheap DOM facts about a page that was already fetched.

    The visual-audit scores need something observable to stand on, and this
    pipeline never reads a screenshot back, so the markup is the evidence — the
    review prompt already accepts DOM evidence for visual claims. Collected here
    because scrape_site is the only place the raw HTML still exists; the crawl
    result keeps text, not markup. An empty dict downstream means "never
    parsed", which is why the failure paths leave it empty rather than zeroed.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")

    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    viewport_content = str(viewport.get("content") or "").lower() if viewport else ""

    legacy = {tag.name for tag in soup.find_all(LEGACY_MARKUP_TAGS)}
    if soup.find(attrs={"bgcolor": True}):
        legacy.add("bgcolor")
    # A table inside a table is layout markup, not tabular data.
    if any(table.find("table") for table in soup.find_all("table")):
        legacy.add("nested_table")

    headings = [clean_text(tag.get_text(" ", strip=True), 200) for tag in soup.find_all("h1")]

    return {
        "h1_texts": [text for text in headings if text][:3],
        "heading_count": len(soup.find_all(["h1", "h2", "h3"])),
        "image_count": len(soup.find_all("img")) + len(soup.find_all("picture")),
        "has_viewport_meta": bool(viewport),
        "responsive_viewport": "device-width" in viewport_content,
        "legacy_markup": sorted(legacy),
    }


def extract_links(base_url: str, html: str) -> Dict[str, List[str]]:
    soup = BeautifulSoup(html or "", "lxml")

    social_links = set()
    internal_links = set()
    all_links = set()

    base_domain = domain_of(base_url)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        absolute = urljoin(base_url, href)
        all_links.add(absolute)

        lowered = absolute.lower()
        if any(domain in lowered for domain in SOCIAL_DOMAINS):
            social_links.add(absolute)

        if domain_of(absolute) == base_domain:
            internal_links.add(absolute)

    return {
        "social_links": sorted(social_links),
        "internal_links": sorted(internal_links),
        "all_links": sorted(all_links),
    }


def extract_emails_phones(text: str, html: str) -> Tuple[List[str], List[str]]:
    """
    Extract emails and plausible US phone numbers.

    Key fix:
    Phone extraction uses visible text + tel: links. It does NOT scan all raw HTML
    for phone numbers because raw HTML often contains tracking/plugin/placeholder
    digit strings that look like phone numbers.
    """
    visible = text or ""
    raw_html = html or ""

    emails = sorted(set(EMAIL_RE.findall(f"{visible}\n{raw_html}")))

    soup = BeautifulSoup(raw_html, "lxml") if raw_html else BeautifulSoup("", "lxml")

    phone_candidates: List[str] = []
    phone_candidates.extend(PHONE_RE.findall(visible))

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            phone_candidates.append(href.replace("tel:", ""))

    phones = []
    seen = set()

    bad_patterns = {
        "0000000000",
        "1111111111",
        "2222222222",
        "3333333333",
        "4444444444",
        "5555555555",
        "6666666666",
        "7777777777",
        "8888888888",
        "9999999999",
        "1234567890",
        "0123456789",
    }

    for phone in phone_candidates:
        digits = re.sub(r"\D+", "", phone or "")

        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]

        if len(digits) != 10:
            continue

        if digits in bad_patterns:
            continue

        area = digits[:3]
        exchange = digits[3:6]

        # US NANP sanity checks.
        if area[0] in {"0", "1"}:
            continue
        if exchange[0] in {"0", "1"}:
            continue

        # Reject obvious repeated placeholders.
        if len(set(digits)) <= 2:
            continue

        formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

        if formatted not in seen:
            seen.add(formatted)
            phones.append(formatted)

    return emails, sorted(phones)


def detect_forms(html: str) -> bool:
    return detect_form_signals(html).get("forms_found") is True


def _yes_no(value: bool | None) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "Unknown"


def _empty_visual_audit_fields() -> Dict[str, Any]:
    return {
        "rendered_site_status": "Unknown",
        "rendered_status_reason": "",
        "desktop_screenshot_path": "",
        "mobile_screenshot_path": "",
        "visual_review_needed": "No",
        **{field: VISUAL_UNKNOWN for field in VISUAL_AUDIT_SCORE_FIELDS},
        "visual_pain_reasons": [],
    }


def _empty_form_signals() -> Dict[str, Any]:
    return {
        "forms_found": False,
        "contact_form_detected": "No",
        "estimate_form_detected": "No",
        "form_strength": "None",
        "form_strength_rank": 0,
        "form_detection_method": "none",
        "form_fields": [],
        "provider_terms": [],
        "has_recaptcha": False,
        "has_embedded_provider": False,
    }


def _field_text_for_tag(tag: Any) -> str:
    parts = [tag.name or "", tag.get_text(" ", strip=True)]
    for attr in ("type", "name", "id", "class", "placeholder", "aria-label", "title", "value"):
        value = tag.get(attr)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _detect_form_fields(soup: BeautifulSoup, text: str) -> List[str]:
    controls = soup.find_all(["input", "textarea", "select", "button"])
    control_text = " ".join(_field_text_for_tag(control) for control in controls)
    combined = f"{text} {control_text}".lower()
    combined = re.sub(r"[_\-]+", " ", combined)

    fields = set()

    if re.search(r"\b(first name|last name|full name|your name|name)\b", combined):
        fields.add("name")
    if "company" in combined or "business name" in combined:
        fields.add("company")
    if "email" in combined or any((control.get("type") or "").lower() == "email" for control in controls):
        fields.add("email")
    if re.search(r"\b(phone|tel|telephone|mobile)\b", combined) or any(
        (control.get("type") or "").lower() == "tel" for control in controls
    ):
        fields.add("phone")
    if re.search(r"\b(address|street|city|zip|postal|property address)\b", combined):
        fields.add("address")
    if re.search(
        r"\b(service details|project details|details|message|description|comments|notes|tell us|how can we help)\b",
        combined,
    ):
        fields.add("details")
    if re.search(r"\b(upload|attach|attachment|photo|image|file)\b", combined) or any(
        (control.get("type") or "").lower() == "file" for control in controls
    ):
        fields.add("upload")
    if re.search(r"\b(submit|send|send message|request|schedule|book|start)\b", combined) or any(
        (control.get("type") or "").lower() == "submit" for control in controls
    ):
        fields.add("submit")

    return sorted(fields)


def _detect_provider_terms(raw: str) -> List[str]:
    lowered = (raw or "").lower()
    found: List[str] = []
    for term in FORM_PROVIDER_TERMS:
        if term in lowered:
            normalized = term.replace("g-recaptcha", "recaptcha")
            if normalized not in found:
                found.append(normalized)
    return sorted(found)


def detect_form_signals(
    html: str,
    *,
    page_text: str = "",
    page_url: str = "",
    method: str = "static_html",
) -> Dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    visible_text = page_text or extract_visible_text(html)
    lowered_text = f"{visible_text} {page_url}".lower()
    raw_for_terms = " ".join([html or "", visible_text, page_url])

    forms = soup.find_all("form")
    controls = soup.find_all(["input", "textarea", "select", "button"])
    embedded = soup.find_all(["iframe", "embed"])
    provider_terms = _detect_provider_terms(raw_for_terms)
    has_recaptcha = any(term in CAPTCHA_TERMS for term in provider_terms)
    non_captcha_providers = [term for term in provider_terms if term not in CAPTCHA_TERMS]
    fields = _detect_form_fields(soup, visible_text)

    has_form = bool(forms)
    has_controls = bool(controls)
    has_embedded_provider = bool(embedded or non_captcha_providers)
    has_form_like_markup = has_form or has_controls or has_embedded_provider or has_recaptcha

    estimate_language = any(term in lowered_text for term in ESTIMATE_CTA_TERMS)
    contact_language = any(
        term in lowered_text
        for term in ["contact", "get in touch", "message", "email", "phone", "call us", "let's discuss"]
    )

    if not has_form_like_markup:
        return _empty_form_signals()

    has_email_or_phone = "email" in fields or "phone" in fields
    has_core_estimate_fields = (
        "name" in fields
        and has_email_or_phone
        and "address" in fields
        and "details" in fields
        and "submit" in fields
    )

    contact_detected = (
        has_form_like_markup
        and (contact_language or estimate_language or len(fields) >= 2)
        and (has_form or has_controls or has_embedded_provider or len(fields) >= 2)
    )
    estimate_detected = has_form_like_markup and estimate_language and (
        has_core_estimate_fields or len(fields) >= 4 or has_embedded_provider
    )

    embedded_strong_fields = (
        has_embedded_provider
        and len(fields) >= 3
        and has_email_or_phone
        and "details" in fields
        and "submit" in fields
    )

    if estimate_detected and (has_core_estimate_fields or embedded_strong_fields):
        strength = "Strong"
    elif estimate_detected and len(fields) >= 3 and has_email_or_phone:
        strength = "Basic"
    elif has_form and len(fields) >= 3 and has_email_or_phone:
        strength = "Basic"
    elif has_embedded_provider and estimate_language:
        strength = "Basic"
    else:
        strength = "Weak"

    resolved_method = method
    if has_embedded_provider:
        resolved_method = "embedded_provider"

    return {
        "forms_found": bool(contact_detected or estimate_detected or strength != "None"),
        "contact_form_detected": _yes_no(contact_detected),
        "estimate_form_detected": _yes_no(estimate_detected),
        "form_strength": strength,
        "form_strength_rank": FORM_STRENGTH_RANK[strength],
        "form_detection_method": resolved_method,
        "form_fields": fields,
        "provider_terms": provider_terms,
        "has_recaptcha": has_recaptcha,
        "has_embedded_provider": has_embedded_provider,
    }


def merge_form_signals(current: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not current:
        current = _empty_form_signals()
    if not candidate:
        return dict(current)

    merged = dict(current)

    fields = sorted(set(merged.get("form_fields") or []) | set(candidate.get("form_fields") or []))
    providers = sorted(set(merged.get("provider_terms") or []) | set(candidate.get("provider_terms") or []))

    if candidate.get("contact_form_detected") == "Yes":
        merged["contact_form_detected"] = "Yes"
    elif merged.get("contact_form_detected") not in {"Yes", "No"}:
        merged["contact_form_detected"] = candidate.get("contact_form_detected", "Unknown")

    if candidate.get("estimate_form_detected") == "Yes":
        merged["estimate_form_detected"] = "Yes"
    elif merged.get("estimate_form_detected") not in {"Yes", "No"}:
        merged["estimate_form_detected"] = candidate.get("estimate_form_detected", "Unknown")

    current_rank = int(merged.get("form_strength_rank") or FORM_STRENGTH_RANK.get(merged.get("form_strength"), 0))
    candidate_rank = int(candidate.get("form_strength_rank") or FORM_STRENGTH_RANK.get(candidate.get("form_strength"), 0))
    if candidate_rank > current_rank:
        merged["form_strength"] = candidate.get("form_strength", "None")
        merged["form_strength_rank"] = candidate_rank
        merged["form_detection_method"] = candidate.get("form_detection_method", "none")

    if candidate.get("form_detection_method") == "embedded_provider":
        merged["form_detection_method"] = "embedded_provider"

    merged["forms_found"] = bool(merged.get("forms_found") or candidate.get("forms_found"))
    merged["form_fields"] = fields
    merged["provider_terms"] = providers
    merged["has_recaptcha"] = bool(merged.get("has_recaptcha") or candidate.get("has_recaptcha"))
    merged["has_embedded_provider"] = bool(
        merged.get("has_embedded_provider") or candidate.get("has_embedded_provider")
    )
    return merged


def classify_cta_strength(ctas: List[str], text: str) -> str:
    if not ctas:
        return "None"
    lowered = " ".join([text or "", " ".join(ctas)]).lower()
    has_estimate_cta = any(term in lowered for term in ESTIMATE_CTA_TERMS)
    if has_estimate_cta and len(ctas) >= 2:
        return "Strong"
    if has_estimate_cta:
        return "Basic"
    return "Weak"


def detect_terms(text: str, terms: List[str]) -> List[str]:
    lowered = (text or "").lower()
    return sorted({term for term in terms if term in lowered})


_fetch_local = threading.local()


def fetch_session() -> requests.Session:
    """One connection pool per worker thread.

    A bare ``requests.get`` pays a fresh TCP+TLS handshake for every page, and a
    lead fetches several pages off the same host in a row. ``requests.Session``
    is not documented as thread-safe and enrichment runs a pool of workers, so
    the pool is thread-local rather than shared — the same shape as
    ``lead_machine.external_signals``.
    """
    session = getattr(_fetch_local, "session", None)
    if session is None:
        session = requests.Session()
        _fetch_local.session = session
    return session


def reset_fetch_session() -> None:
    """Hand the pool back at the end of a lead.

    Reuse is worth having across the pages of one site and worth nothing across
    leads, which are different hosts. What does carry is the cookie jar and the
    open sockets: a worker thread that enriched forty leads held one jar with
    forty sites' cookies in it. Cookies are scoped to their domain, so nothing
    leaks between leads -- but the jar and the connections should end with the
    crawl that opened them.
    """
    session = getattr(_fetch_local, "session", None)
    _fetch_local.session = None
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 1) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    session = fetch_session()
    last_result: Dict[str, Any] = {
        "ok": False,
        "status_code": "",
        "final_url": "",
        "html": "",
        "error": "not attempted",
        "error_type": "",
    }

    for attempt in range(max(1, retries + 1)):
        try:
            resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            last_result = {
                "ok": resp.status_code < 400,
                "status_code": resp.status_code,
                "final_url": resp.url,
                "html": resp.text or "",
                "error": "",
                "error_type": "",
            }
            if resp.status_code < 400:
                return last_result
            # 5xx is the transient case worth a second look. A 404 is the
            # server answering definitively, and most of this crawl's misses
            # are 404s — retrying them doubled the cost of every miss.
            if attempt < retries and resp.status_code >= 500:
                time.sleep(0.2)
                continue
            return last_result
        except requests.RequestException as exc:
            last_result = {
                "ok": False,
                "status_code": "",
                "final_url": "",
                "html": "",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }
            if attempt < retries and not isinstance(
                exc,
                (
                    requests.exceptions.SSLError,
                    requests.exceptions.TooManyRedirects,
                    requests.exceptions.InvalidURL,
                    requests.exceptions.MissingSchema,
                ),
            ):
                time.sleep(0.2)
                continue
            return last_result

    return last_result


def classify_hard_static_failure(fetch_result: Dict[str, Any]) -> Tuple[str, str]:
    status_code = fetch_result.get("status_code")
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        code = 0

    if code == 404 or code >= 500:
        # 5xx is retried once; 404 is taken at its word, so do not claim a
        # retry the fetch never made.
        suffix = " after retry" if code >= 500 else ""
        return "homepage_http_hard_failure", f"Homepage returned HTTP {code}{suffix}."

    error_type = str(fetch_result.get("error_type") or "").lower()
    error = str(fetch_result.get("error") or "").lower()
    if "ssl" in error_type or "certificate" in error or "cert" in error:
        return "ssl_cert_hard_failure", "Static fetch failed with a hard SSL/certificate error."
    if "toomanyredirects" in error_type or "too many redirects" in error:
        return "broken_redirect", "Static fetch failed because the homepage redirect chain did not resolve."
    if any(
        term in error
        for term in [
            "name or service not known",
            "temporary failure in name resolution",
            "failed to resolve",
            "nameresolutionerror",
            "name_resolution",
            "nodename nor servname",
        ]
    ):
        return "dns_domain_failure", "Static fetch failed because the domain could not be resolved."

    return "", ""


def candidate_pages_split(
    base_url: str, internal_links: List[str], keywords: List[str]
) -> Tuple[List[str], List[str]]:
    """Split this keyword group's candidates into linked pages and blind guesses.

    The guessed paths are a safety net for sites that never link their contact
    page. They used to be fetched unconditionally, so a site that properly
    links ``/contact-us`` still paid ten blind 404s. Both lists are always
    returned; it is the crawl that decides whether the net is needed, because
    only the crawl knows whether a linked page actually resolved.
    """
    root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    base_domain = domain_of(base_url)

    def add(url: str, into: List[str]) -> None:
        absolute = urljoin(root, url)
        if domain_of(absolute) != base_domain:
            return
        if absolute not in into:
            into.append(absolute)

    linked: List[str] = []
    for link in internal_links:
        lowered = link.lower()
        if any(k in lowered for k in keywords):
            add(link, linked)

    fallback_paths = []
    for keyword in keywords:
        if keyword in {"contact", "estimate", "quote", "schedule", "request"}:
            fallback_paths += CONTACT_ESTIMATE_PATHS
        elif keyword == "service":
            fallback_paths += ["/services", "/our-services", "/service"]
        elif keyword == "about":
            fallback_paths += ["/about", "/about-us"]
        elif keyword == "gallery":
            fallback_paths += ["/gallery", "/portfolio", "/projects", "/project-pictures", "/work"]

    guessed: List[str] = []
    for path in fallback_paths:
        add(path, guessed)

    return linked, [url for url in guessed if url not in linked]


def find_candidate_pages(base_url: str, internal_links: List[str], keywords: List[str]) -> List[str]:
    linked, guessed = candidate_pages_split(base_url, internal_links, keywords)
    return linked or guessed


def find_candidate_page(base_url: str, internal_links: List[str], keywords: List[str]) -> str:
    pages = find_candidate_pages(base_url, internal_links, keywords)
    return pages[0] if pages else ""


def _candidate_page_name(page_url: str, existing: Dict[str, Dict[str, Any]], default: str) -> str:
    lowered = page_url.lower()
    if any(term in lowered for term in ["estimate", "quote", "schedule", "request"]):
        base = "estimate"
    elif "contact" in lowered:
        base = "contact"
    else:
        base = default

    if base not in existing:
        return base

    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"


def empty_site_result(status: str, flags: List[str], url: str = "", error: str = "") -> Dict[str, Any]:
    result = {
        "site_status": status,
        "site_issue_flags": flags,
        "final_url": url,
        "domain": domain_of(url),
        "homepage": {
            "url": url,
            "status_code": "",
            "title": "",
            "meta": "",
            "text": "",
            "error": error,
        },
        "pages": {},
        # Empty, not zeroed: nothing was parsed, so the visual scores must stay
        # Unknown rather than read this as a page with no images and no headings.
        "page_structure": {},
        "emails_found": [],
        "phones_found": [],
        "social_links": [],
        "forms_found": False,
        "contact_form_detected": "Unknown",
        "estimate_form_detected": "Unknown",
        "form_strength": "None",
        "form_detection_method": "none",
        "form_fields": [],
        "form_provider_terms": [],
        "has_recaptcha": False,
        "cta_strength": "None",
        "cta_terms": [],
        "high_ticket_terms": [],
        "generic_copy_terms": [],
        "combined_text": "",
    }
    result.update(_empty_visual_audit_fields())
    if "manual_visual_review_needed" in flags:
        result["visual_review_needed"] = "Yes"
    if error and not result.get("rendered_status_reason"):
        result["rendered_status_reason"] = error
    return result


def mark_per_lead_timeout(site: Dict[str, Any], *, elapsed: float, error: str = "") -> Dict[str, Any]:
    updated = dict(site)
    flags = {str(flag).strip() for flag in updated.get("site_issue_flags") or [] if str(flag).strip()}
    flags.add("per_lead_enrichment_timeout")
    if updated.get("site_status") != "No Website":
        flags.add("rendered_check_needed")
        flags.add("manual_visual_review_needed")
        updated["visual_review_needed"] = "Yes"
    updated["site_issue_flags"] = sorted(flags)
    updated["enrichment_elapsed_seconds"] = round(elapsed, 1)
    updated["enrichment_error"] = error or f"Per-lead enrichment ceiling exceeded after {format_elapsed(elapsed)}s."
    return updated


def scrape_site(
    website_url: str,
    *,
    rendered_form_check: bool = False,
    high_ticket_terms: List[str] | tuple[str, ...] | None = None,
    started_at: float | None = None,
    per_lead_timeout: float = PER_LEAD_ENRICH_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    started_at = time.monotonic() if started_at is None else started_at
    url = normalize_url(website_url)
    high_ticket_terms = high_ticket_terms or HIGH_TICKET_TERMS

    if not url:
        site = empty_site_result("No Website", ["no_website"])
        site["enrichment_elapsed_seconds"] = round(elapsed_seconds(started_at), 1)
        return site

    flags: List[str] = []

    if is_non_standard_site_or_booking_url(url):
        flags.append("non_standard_site_or_booking_url")

    home = fetch_url(url, timeout=HOMEPAGE_FETCH_TIMEOUT)

    if not home["ok"]:
        hard_flag, hard_reason = classify_hard_static_failure(home)
        failure_flags = flags + ["static_fetch_failed"]
        if hard_flag:
            site = empty_site_result(
                "Broken Website",
                failure_flags + [hard_flag],
                home.get("final_url") or url,
                hard_reason or home.get("error") or "",
            )
            site["enrichment_elapsed_seconds"] = round(elapsed_seconds(started_at), 1)
            return site
        site = empty_site_result(
            "Unknown",
            failure_flags + ["rendered_check_needed", "manual_visual_review_needed"],
            home.get("final_url") or url,
            home.get("error") or "Static fetch failed; browser-rendered check needed before site-status claims.",
        )
        site["enrichment_elapsed_seconds"] = round(elapsed_seconds(started_at), 1)
        return site

    final_url = home["final_url"] or url
    html = home["html"]

    if is_non_standard_site_or_booking_url(final_url) and "non_standard_site_or_booking_url" not in flags:
        flags.append("non_standard_site_or_booking_url")

    title, meta = extract_title_meta(html)
    text = extract_visible_text(html)
    # The homepage answered, but did it answer with a page? A 200 whose body is
    # an empty SPA root — or empty outright — leaves every detector below
    # reading nothing, and everything they report is then a statement about our
    # parser rather than about the site. `text` cannot be used for this: it
    # includes `<title>`, so the blankest SPA in the corpus reads as 56
    # characters of content.
    homepage_body_blank = not extract_visible_body_text(html)
    if homepage_body_blank:
        flags.append(EMPTY_BODY_FLAG)
    links = extract_links(final_url, html)

    emails, phones = extract_emails_phones(text, html)
    emails = clean_emails(emails, business_domain=domain_of(final_url))
    form_signals = detect_form_signals(html, page_text=text, page_url=final_url, method="static_html")
    ctas = detect_terms(text, CTA_TERMS)
    high_ticket = detect_terms(text, high_ticket_terms)
    generic_copy = detect_terms(text, GENERIC_COPY_TERMS)

    pages: Dict[str, Dict[str, Any]] = {}

    def timed_out() -> bool:
        return elapsed_seconds(started_at) > per_lead_timeout

    seen_page_urls = set()

    def dedupe_targets(urls: List[str], default_name: str) -> List[Tuple[str, str]]:
        targets: List[Tuple[str, str]] = []
        for page_url in urls:
            if not page_url or page_url in seen_page_urls:
                continue
            seen_page_urls.add(page_url)
            targets.append((default_name, page_url))
        return targets

    resolved_groups: List[Tuple[str, List[str], List[str], bool]] = []
    for keywords, default_name, limit, needs_form in CANDIDATE_PAGE_GROUPS:
        linked, guessed = candidate_pages_split(final_url, links["internal_links"], keywords)
        if limit:
            linked, guessed = linked[:limit], guessed[:limit]
        resolved_groups.append((default_name, linked, guessed, needs_form))

    def interleave(per_group: List[Tuple[str, List[str]]]) -> List[Tuple[str, str]]:
        """Round-robin across groups so no one group can drain the budget.

        A nav and footer full of contact links is ordinary, and taking them in
        group order spent the whole ceiling there — leaving `weak_services` to
        fire about a services page that was never requested.
        """
        targets: List[Tuple[str, str]] = []
        for offset in range(max((len(urls) for _name, urls in per_group), default=0)):
            for default_name, urls in per_group:
                if offset < len(urls):
                    targets += dedupe_targets([urls[offset]], default_name)
        return targets

    combined_text_parts = [text]
    enrichment_timed_out = False
    budget_exhausted = False
    fetched_pages = 0
    page_budget = resolve_candidate_page_budget()
    page_fetch_workers = resolve_page_fetch_workers()
    fetch_pool: ThreadPoolExecutor | None = None

    def fetch_wave(wave: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
        """Fetch a wave of candidate pages at once, in submission order.

        The pool is per-lead and lazy, so a site whose crawl never needs two
        pages at once never starts one and keeps its fetches on the calling
        thread, where the pooled session is. Results come back as a list
        indexed by submission — not by completion — because the caller reads
        them in that order and the page slot names depend on it.
        """
        nonlocal fetch_pool
        if len(wave) == 1:
            return [fetch_url(wave[0][1], timeout=CANDIDATE_PAGE_FETCH_TIMEOUT)]
        if fetch_pool is None:
            fetch_pool = ThreadPoolExecutor(
                max_workers=page_fetch_workers, thread_name_prefix="page-fetch"
            )
        futures = [
            fetch_pool.submit(fetch_url, page_url, timeout=CANDIDATE_PAGE_FETCH_TIMEOUT)
            for _name, page_url in wave
        ]
        return [future.result() for future in futures]

    def crawl_targets(targets: List[Tuple[str, str]], *, reserve: int = 0) -> Dict[str, bool]:
        """Fetch each target; report which groups came back with a live page.

        `reserve` holds slots back for the pass that follows. Without it a nav
        of per-city contact links drains the budget in pass 1, and a group
        with no linked page never gets the one probe that would tell us
        whether it exists.

        Targets are admitted a wave at a time and their results processed
        serially, in submission order. Only the fetching overlaps: the parsing
        is BeautifulSoup under one GIL and gains nothing from being parallel,
        and the page slot names (`estimate`, `estimate_2`, ...) come from
        insertion order, so naming off completion order would reshuffle the
        output of every lead with two contactish pages.

        The budget check is per target, exactly as it was. The clock check is
        per target too, but the whole wave is admitted before any of it is
        fetched — so a lead that runs out of time mid-wave finishes the pages
        already dispatched instead of stopping at the next one. That is one
        wave of slack, bounded by the candidate-page timeout.
        """
        nonlocal emails, phones, ctas, high_ticket, generic_copy, form_signals
        nonlocal enrichment_timed_out, fetched_pages, budget_exhausted
        group_ok: Dict[str, bool] = {}
        ceiling = max(1, page_budget - reserve)
        index = 0
        stopped = False

        while index < len(targets) and not stopped:
            wave: List[Tuple[str, str]] = []
            while index < len(targets) and len(wave) < page_fetch_workers:
                default_page_name, page_url = targets[index]
                if fetched_pages >= ceiling:
                    budget_exhausted = budget_exhausted or fetched_pages >= page_budget
                    stopped = True
                    break
                if timed_out():
                    enrichment_timed_out = True
                    stopped = True
                    break
                index += 1
                if not page_url:
                    continue

                # Stay on same business domain.
                if domain_of(page_url) != domain_of(final_url):
                    continue

                fetched_pages += 1
                wave.append((default_page_name, page_url))

            if not wave:
                continue

            # Pace the crawl between waves, not after the last one — that
            # trailing sleep bought nothing and every lead paid it. Within a
            # wave there is nothing to pace: those requests are deliberately
            # concurrent.
            if fetched_pages > len(wave):
                time.sleep(0.1)

            # strict: a result list shorter than the wave would silently drop
            # the tail of the crawl, which is quieter than the reshuffle
            # SubmissionOrderTest catches.
            for (default_page_name, page_url), result in zip(wave, fetch_wave(wave), strict=True):
                page_html = result.get("html") or ""
                page_text = extract_visible_text(page_html) if result.get("ok") else ""
                page_title, page_meta = extract_title_meta(page_html) if page_html else ("", "")

                page_emails, page_phones = extract_emails_phones(page_text, page_html)
                emails = clean_emails(emails + page_emails, business_domain=domain_of(final_url))
                phones = sorted(set(phones + page_phones))

                if page_text:
                    combined_text_parts.append(page_text)

                page_form_signals = detect_form_signals(
                    page_html,
                    page_text=page_text,
                    page_url=page_url,
                    method="candidate_page",
                )
                form_signals = merge_form_signals(form_signals, page_form_signals)

                ctas = sorted(set(ctas + detect_terms(page_text, CTA_TERMS)))
                high_ticket = sorted(set(high_ticket + detect_terms(page_text, high_ticket_terms)))
                generic_copy = sorted(set(generic_copy + detect_terms(page_text, GENERIC_COPY_TERMS)))

                page_name = _candidate_page_name(page_url, pages, default_page_name)
                page_links = extract_links(result.get("final_url") or page_url, page_html) if page_html else {
                    "internal_links": [], "external_links": [], "social_links": []
                }
                page_soup = BeautifulSoup(page_html, "html.parser") if page_html else BeautifulSoup("", "html.parser")
                pages[page_name] = {
                    "url": page_url,
                    "ok": result.get("ok"),
                    "status_code": result.get("status_code"),
                    "title": page_title,
                    "meta": page_meta,
                    "text": clean_text(page_text, 6000),
                    "contact_form_detected": page_form_signals.get("contact_form_detected", "No"),
                    "estimate_form_detected": page_form_signals.get("estimate_form_detected", "No"),
                    "form_strength": page_form_signals.get("form_strength", "None"),
                    "form_detection_method": page_form_signals.get("form_detection_method", "none"),
                    # Counted here so gallery/portfolio pages can be graded later on how
                    # much work they actually show, not just on the page existing.
                    "image_count": len(page_soup.find_all("img")) + len(page_soup.find_all("picture")),
                    "verification_snapshot": {
                        "page_role": default_page_name,
                        "requested_url": page_url,
                        "final_url": result.get("final_url") or page_url,
                        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        "response_status": result.get("status_code"),
                        "title": page_title,
                        "visible_text": clean_text(page_text, 6000),
                        "visible_links": page_links.get("all_links", []),
                        "form_count": len(page_soup.find_all("form")),
                        "control_count": len(page_soup.find_all(["input", "textarea", "select", "button"])),
                        "render_outcome": "loaded" if result.get("ok") and page_text.strip() else "blank" if result.get("ok") else "connection_error",
                    },
                }

                group_ok[default_page_name] = group_ok.get(default_page_name, False) or bool(result.get("ok"))

        return group_ok

    def group_answered(name: str, needs_form: bool, linked_ok: Dict[str, bool]) -> bool:
        """Would fetching this group's guesses still be able to change anything?

        For the form-bearing group the question is not "did a page load" — a
        soft 404 loads, and so does a contact page offering nothing but a
        phone number. It is answered only by a form downstream treats as
        final, and that is asked of the crawl's *cumulative* signals, so a
        strong form found on the homepage ends the search too.
        """
        if needs_form:
            return has_terminal_form(form_signals)
        return bool(linked_ok.get(name))

    try:
        # Pass 1: the pages the homepage actually links. A linked page beats any
        # guess. Hold back one slot per group for pass 2.
        linked_ok = crawl_targets(
            interleave([(name, linked) for name, linked, _g, _f in resolved_groups]),
            reserve=len(resolved_groups),
        )

        # Pass 2: the blind guesses, for each group still unanswered. "The HTML
        # linked something" is not enough — a HubSpot template can emit an
        # unrendered `/f/contact/$hs_path` that matches the keywords and 404s, and
        # the guess it suppressed was the page actually serving the form.
        crawl_targets(
            interleave(
                [
                    (name, guessed)
                    for name, _linked, guessed, needs_form in resolved_groups
                    if not group_answered(name, needs_form, linked_ok)
                ]
            )
        )
    except BaseException:
        # An interrupt must not sit and wait on in-flight fetches.
        if fetch_pool is not None:
            fetch_pool.shutdown(wait=False, cancel_futures=True)
        raise

    if fetch_pool is not None:
        # Both passes share the pool, so pass 2 reuses pass 1's threads and the
        # sessions on them. Nothing is ever left in flight — every wave is
        # collected before the next is admitted — so this only joins threads
        # that have already finished. Joining rather than detaching is the
        # point: a pool thread's session is thread-local, so `reset_fetch_session`
        # on the lead thread cannot reach it, and it is the thread ending that
        # ends its cookie jar and its sockets inside the lead that opened them.
        fetch_pool.shutdown(wait=True)

    combined_text = clean_text(" ".join(combined_text_parts), 30000)
    cta_strength = classify_cta_strength(ctas, combined_text)

    if timed_out():
        enrichment_timed_out = True

    # `and ctas` was the whole trigger, and it skipped precisely the page that
    # most needs a browser: an SPA whose static body is empty yields no CTA
    # terms by construction, so the one check that could have recovered its
    # form never ran on it.
    if (
        rendered_form_check
        and not enrichment_timed_out
        and form_signals.get("form_strength_rank", 0) <= 1
        and (ctas or homepage_body_blank)
    ):
        rendered_signals = detect_rendered_form(final_url)
        form_signals = merge_form_signals(form_signals, rendered_signals)

    forms_found = bool(form_signals.get("forms_found"))

    if budget_exhausted:
        flags.append(CANDIDATE_BUDGET_FLAG)

    # A search we cut short did not establish "this site has no estimate
    # form" — it established that we stopped looking. Downstream charges +20
    # for `estimate_form_detected == "no"`, +14 for contact, and +18/+12 for a
    # weak/basic strength, and that pain is what carries a lead past
    # `sendability.VISIBLE_PAIN_BLOCKER`. "Unknown" is the honest answer and
    # the one the pain profile already ignores.
    #
    # `cta_strength` moves with them. `evidence._derive_visible_pain_profile`
    # charges its +14 on `cta_strength in {none, weak}` *or* the flag, so
    # leaving the graded field at "None" keeps the charge standing no matter
    # what the flags say.
    #
    # The budget is one way to end a crawl without an answer; an empty body is
    # the other, and it was ungated. `fetch_url` sets `ok = status < 400`, so a
    # 200 carrying an empty `<body>` is not a failure and never reaches
    # `empty_site_result` — the one path that already writes "Unknown"
    # correctly. It takes this path instead, `detect_form_signals` early-returns
    # `_empty_form_signals()`, and the flags below get appended about a page
    # nobody read. Measured on the Phase 3 corpus: twelve businesses, eight of
    # them with a title-only body.
    truncated_without_answer = budget_exhausted and not has_terminal_form(form_signals)
    absence_unproven = truncated_without_answer or homepage_body_blank
    # The rewrite is the narrower of the two, and deliberately so. A blank
    # static body is exactly the case `--rendered-form-check` above now reaches,
    # and a browser that came back with a form has answered the question the
    # static parse could not. Blanking that to "Unknown" would throw away the
    # evidence the rendered pass exists to collect — which would leave §2.3 and
    # §2.1 of this fix working against each other: one sends the browser at the
    # SPA precisely so it can read the form, the other then discards what it
    # read. `has_terminal_form` alone is too strict a save, because it only
    # spares a *Strong* form: measured on the 13 recovered leads, twelve came
    # back `embedded_provider` with a real Basic form and would still have been
    # blanked. A Basic form is a true finding and a legitimate +12 pain; the
    # bar is whether anything actually read the page, not how good the answer
    # was.
    #
    # The escape belongs to the blank-body branch only. Truncation keeps its
    # original T4.4 semantics: having parsed *some* pages says nothing about
    # the page we never fetched, and a form found on page 3 does not prove
    # there is no better one on page 17 — which is why the bar there stays
    # `has_terminal_form`. A blank homepage is the opposite situation: there
    # is one page in question, and if anything positively found a form on it
    # we have read it.
    blank_body_answered = homepage_body_blank and bool(form_signals.get("forms_found"))
    rewrite_needed = truncated_without_answer or (homepage_body_blank and not blank_body_answered)
    if rewrite_needed and not has_terminal_form(form_signals):
        form_signals = dict(form_signals)
        for field in ("contact_form_detected", "estimate_form_detected", "form_strength"):
            form_signals[field] = "Unknown"
        cta_strength = "Unknown"

    # The other half of the same sentence. Rewriting the fields to "Unknown"
    # while still appending the absence flags says both things at once, and
    # the flags win: `score_leads.score_online_weakness` charges +8 for
    # `no_form_detected` and +8 for `no_clear_cta_detected` inside
    # `not strong_estimate_form`, a branch the "Unknown" rewrite *guarantees*
    # is entered. A site whose only form sits past the ceiling would grade as
    # a site with no form — our own crawl budget sold as the prospect's
    # weakness. We did not look; we do not get to report.
    #
    # `no_email_detected` and `no_high_ticket_terms_detected` had no gate at
    # all, and they are read the same way: the first is +5 in
    # `score_online_weakness` *and* one of the corroborators that turns the
    # other two +2s into +8s, and the second decides how the packet describes
    # what the business sells. Both are read out of page text, so a page with
    # no text cannot support either.
    if not forms_found and not absence_unproven:
        flags.append("no_form_detected")
    if not ctas and not absence_unproven:
        flags.append("no_clear_cta_detected")
    if not emails and not absence_unproven:
        flags.append("no_email_detected")
    if not high_ticket and not absence_unproven:
        flags.append("no_high_ticket_terms_detected")

    # Only when nothing anywhere found a form: a subpage or the rendered pass
    # answering the question outranks the homepage's empty body, and the method
    # that answered it is the one worth recording.
    form_detection_method = form_signals.get("form_detection_method", "none")
    if homepage_body_blank and form_detection_method == "none":
        form_detection_method = NO_BODY_FORM_METHOD

    if is_social_url(final_url):
        site_status = "Social Only"
    else:
        site_status = "Loaded"

    result = {
        "site_status": site_status,
        "site_issue_flags": sorted(set(flags)),
        "final_url": final_url,
        "domain": domain_of(final_url),
        "homepage": {
            "url": final_url,
            "status_code": home.get("status_code"),
            "title": title,
            "meta": meta,
            "text": clean_text(text, 9000),
        },
        "pages": pages,
        "page_structure": extract_page_structure(html),
        "emails_found": emails,
        "phones_found": phones,
        "social_links": links["social_links"],
        "forms_found": forms_found,
        "contact_form_detected": form_signals.get("contact_form_detected", "No"),
        "estimate_form_detected": form_signals.get("estimate_form_detected", "No"),
        "form_strength": form_signals.get("form_strength", "None"),
        "form_detection_method": form_detection_method,
        "form_fields": form_signals.get("form_fields", []),
        "form_provider_terms": form_signals.get("provider_terms", []),
        "has_recaptcha": form_signals.get("has_recaptcha", False),
        "cta_terms": ctas,
        "cta_strength": cta_strength,
        "high_ticket_terms": high_ticket,
        "generic_copy_terms": generic_copy,
        "combined_text": combined_text,
        "verification_snapshot": {
            "page_role": "homepage",
            "requested_url": url,
            "final_url": final_url,
            "retrieved_at": retrieved_at,
            "response_status": home.get("status_code"),
            "title": title,
            "visible_text": clean_text(text, 9000),
            "visible_links": links.get("all_links", []),
            "form_count": len(BeautifulSoup(html, "html.parser").find_all("form")),
            "control_count": len(BeautifulSoup(html, "html.parser").find_all(["input", "textarea", "select", "button"])),
            "render_outcome": "loaded" if text.strip() else "blank",
        },
        "verification_snapshots": [],
    }
    result["verification_snapshots"] = [result["verification_snapshot"]] + [
        page["verification_snapshot"] for page in pages.values() if page.get("verification_snapshot")
    ]
    result.update(_empty_visual_audit_fields())
    elapsed = elapsed_seconds(started_at)
    result["enrichment_elapsed_seconds"] = round(elapsed, 1)
    if enrichment_timed_out:
        result = mark_per_lead_timeout(result, elapsed=elapsed)
    # Static-only grading, after the timeout flags land so it grades the crawl
    # that actually happened. enrich_row re-derives once the browser pass has
    # run, so a mobile render gets folded in when there is one.
    return apply_visual_audit_scores(result)


class BrowserHandle:
    """A Chromium and the Playwright driver that owns it.

    Both have to be shut down together: closing the browser alone leaves the
    driver subprocess running, and stopping the driver alone orphans Chromium.
    """

    def __init__(self, playwright: Any, browser: Any) -> None:
        self.playwright = playwright
        self.browser = browser

    def is_alive(self) -> bool:
        is_connected = getattr(self.browser, "is_connected", None)
        if is_connected is None:
            return True
        try:
            return bool(is_connected())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass


_browser_local = threading.local()


def _launch_browser() -> BrowserHandle:
    """The real factory. Tests patch this name to avoid needing the binary."""
    playwright = sync_playwright().start()
    return BrowserHandle(playwright, playwright.chromium.launch(headless=True))


def thread_browser() -> Any:
    """One Chromium per worker thread, launched on first use.

    A launch costs roughly a second, and a lead that needed both the rendered
    site check and the rendered form check paid two of them. Playwright's sync
    API is bound to the thread that created it, so the browser is thread-local
    rather than shared -- the same shape as ``fetch_session``.
    """
    handle = getattr(_browser_local, "handle", None)
    if handle is not None and not handle.is_alive():
        # A crashed Chromium would otherwise poison every remaining lead on
        # this thread, which costs more than the launch reuse saved.
        close_thread_browser()
        handle = None
    if handle is None:
        handle = _launch_browser()
        _browser_local.handle = handle
    return handle.browser


def new_browser_context(**options: Any) -> Any:
    """Open a per-lead context, discarding the browser if it cannot.

    ``is_alive`` catches a browser that has disconnected, not one that is still
    connected but out of file descriptors or memory after a long run. That
    failure surfaces here, and the callers grade it as a *site* problem
    ("Unknown", no mobile screenshot) -- so a poisoned browser would spend the
    rest of the thread's leads misreporting them. Dropping it costs one launch.
    """
    try:
        return thread_browser().new_context(**options)
    except Exception:
        close_thread_browser()
        raise


def close_thread_browser() -> None:
    """Shut down this thread's Chromium, if it opened one."""
    handle = getattr(_browser_local, "handle", None)
    _browser_local.handle = None
    if handle is not None:
        handle.close()


def settle_rendered_page(page: Any, timeout_ms: int, *, floor_ms: int = 0) -> None:
    """Wait for the page to go quiet, and never for less than ``floor_ms``.

    The floor is the safety half of this. Cutting a wait short is not free:
    whatever the page had not rendered yet is read as absence, and absence is
    what the pain score charges for. Callers that only screenshot the page can
    take the early return; callers that read evidence out of the DOM pass the
    window they used to get unconditionally.
    """
    ceiling = max(1, min(RENDER_SETTLE_TIMEOUT_MS, timeout_ms))
    floor = min(floor_ms, ceiling)
    started_at = time.monotonic()
    try:
        page.wait_for_load_state("networkidle", timeout=ceiling)
    except PlaywrightTimeoutError:
        # Sites carrying ad or chat widgets never reach network idle. This wait
        # has already spent the full ceiling on them -- the same time the old
        # fixed sleep did -- so there is nothing left to make up.
        return
    except Exception:
        # The load state could not be reported at all, so nothing was waited
        # out. Give the page at least the short fallback before it is read.
        floor = max(floor, min(RENDER_SETTLE_FALLBACK_MS, ceiling))

    shortfall = floor - int((time.monotonic() - started_at) * 1000)
    if shortfall > 0:
        try:
            page.wait_for_timeout(shortfall)
        except Exception:
            pass


def detect_rendered_form(url: str, timeout_ms: int = 20000) -> Dict[str, Any]:
    signals = _empty_form_signals()
    signals["form_detection_method"] = "rendered_browser"

    if not url:
        return signals

    context = None
    try:
        context = new_browser_context(
            viewport={"width": 1366, "height": 1000},
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        settle_rendered_page(page, timeout_ms, floor_ms=RENDER_FORM_SETTLE_FLOOR_MS)
        html = page.content()
        try:
            text = page.inner_text("body", timeout=3000)
        except Exception:
            text = ""
        rendered = detect_form_signals(html, page_text=text, page_url=url, method="rendered_browser")
        if rendered.get("form_detection_method") != "embedded_provider":
            rendered["form_detection_method"] = "rendered_browser"
        return rendered
    except PlaywrightTimeoutError:
        return signals
    except Exception:
        return signals
    finally:
        close_browser_context(context)


def close_browser_context(context: Any) -> None:
    """Per-lead isolation now that the browser outlives the lead.

    Cookies, storage and service workers live on the context, so one that is
    left open carries the last lead's site into the next one on this thread.
    """
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass


def _classify_rendered_exception(exc: Exception) -> Tuple[str, str]:
    if isinstance(exc, PlaywrightTimeoutError):
        return "Timeout", f"Browser render timed out: {exc}"

    message = str(exc)
    lowered = message.lower()
    if any(
        term in lowered
        for term in [
            "err_name_not_resolved",
            "err_cert",
            "certificate",
            "ssl",
            "too_many_redirects",
            "err_too_many_redirects",
            "err_connection_refused",
        ]
    ):
        return "Broken", f"Browser-rendered load failed: {message}"
    return "Unknown", f"Browser-rendered check unavailable: {message}"


def check_rendered_site(
    url: str,
    *,
    desktop_path: Path | None = None,
    mobile_path: Path | None = None,
    timeout_ms: int = 25000,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "desktop": "",
        "mobile": "",
        "error": "",
        "rendered_site_status": "Unknown",
        "rendered_status_reason": "",
    }
    if not url:
        result["error"] = "no_url"
        result["rendered_status_reason"] = "No URL available for browser-rendered check."
        return result

    if desktop_path:
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
    if mobile_path:
        mobile_path.parent.mkdir(parents=True, exist_ok=True)

    desktop_context = None
    mobile_context = None
    try:
        # Two contexts, not two pages in one: `is_mobile` is a context option
        # in Playwright, so a shared context would render the mobile shot as a
        # narrow desktop and move `mobile_visual_score`. Same options the two
        # `new_page` calls used to pass, so the render is unchanged -- only the
        # launch is amortised.
        desktop_context = new_browser_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=USER_AGENT,
        )
        page = desktop_context.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        settle_rendered_page(page, timeout_ms)
        status_code = response.status if response else 0
        final_url = page.url
        if status_code == 404 or status_code >= 500:
            result["rendered_site_status"] = "Broken"
            result["rendered_status_reason"] = f"Browser rendered homepage returned HTTP {status_code}."
            return result
        if status_code >= 400:
            result["rendered_site_status"] = "Unknown"
            result["rendered_status_reason"] = (
                f"Browser rendered homepage returned HTTP {status_code}; manual review needed before broken-site claims."
            )
            return result
        if desktop_path:
            page.screenshot(path=str(desktop_path), full_page=True)
            result["desktop"] = str(desktop_path)

        result["rendered_site_status"] = "Loaded"
        result["rendered_status_reason"] = (
            f"Browser rendered homepage successfully"
            + (f" at {final_url}." if final_url and final_url != url else ".")
        )

        if mobile_path:
            mobile_context = new_browser_context(
                viewport={"width": 390, "height": 1200},
                is_mobile=True,
                user_agent=USER_AGENT,
            )
            mobile = mobile_context.new_page()
            mobile_response = mobile.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            settle_rendered_page(mobile, timeout_ms)
            mobile_status = mobile_response.status if mobile_response else 0
            if mobile_status >= 400:
                result["error"] = f"mobile_http_{mobile_status}"
            else:
                mobile.screenshot(path=str(mobile_path), full_page=True)
                result["mobile"] = str(mobile_path)

        return result

    except Exception as exc:
        status, reason = _classify_rendered_exception(exc)
        result["rendered_site_status"] = status
        result["rendered_status_reason"] = reason
        result["error"] = reason
        return result
    finally:
        close_browser_context(desktop_context)
        close_browser_context(mobile_context)


def screenshot_paths(business_name: str, place_id: str, screenshots_dir: Path) -> Tuple[Path, Path]:
    ident = safe_slug(f"{business_name}-{place_id[:8]}")
    return (
        screenshots_dir / f"{ident}-desktop.png",
        screenshots_dir / f"{ident}-mobile.png",
    )


def take_screenshots(url: str, desktop_path: Path, mobile_path: Path, timeout_ms: int = 25000) -> Dict[str, Any]:
    return check_rendered_site(
        url,
        desktop_path=desktop_path,
        mobile_path=mobile_path,
        timeout_ms=timeout_ms,
    )


def apply_rendered_site_result(site: Dict[str, Any], rendered: Dict[str, Any]) -> Dict[str, Any]:
    if not rendered:
        return site

    updated = dict(site)
    flags = {str(flag).strip() for flag in updated.get("site_issue_flags") or [] if str(flag).strip()}
    rendered_status = str(rendered.get("rendered_site_status") or "Unknown").strip() or "Unknown"
    rendered_reason = str(rendered.get("rendered_status_reason") or rendered.get("error") or "").strip()

    updated["rendered_site_status"] = rendered_status
    updated["rendered_status_reason"] = rendered_reason
    updated["desktop_screenshot_path"] = str(rendered.get("desktop") or updated.get("desktop_screenshot_path") or "")
    updated["mobile_screenshot_path"] = str(rendered.get("mobile") or updated.get("mobile_screenshot_path") or "")

    if rendered_status == "Loaded":
        flags.discard("rendered_check_needed")
        if updated.get("site_status") not in {"No Website", "Social Only", "Jobber Only"}:
            updated["site_status"] = "Loaded"
        if "static_fetch_failed" in flags or updated.get("desktop_screenshot_path") or updated.get("mobile_screenshot_path"):
            flags.add("manual_visual_review_needed")
            updated["visual_review_needed"] = "Yes"
    elif rendered_status == "Broken":
        flags.add("rendered_load_failed")
        if updated.get("site_status") != "No Website":
            updated["site_status"] = "Broken Website"
        updated["visual_review_needed"] = "No"
    elif rendered_status in {"Timeout", "Unknown"}:
        if updated.get("site_status") not in {"No Website", "Broken Website", "Social Only", "Jobber Only"}:
            updated["site_status"] = "Unknown"
        flags.add("manual_visual_review_needed")
        updated["visual_review_needed"] = "Yes"

    updated["site_issue_flags"] = sorted(flags)
    return updated


def normalize_digits(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def enrich_row(
    row: Dict[str, str],
    screenshots_dir: Path,
    take_shots: bool,
    rendered_form_check: bool = False,
    rendered_site_check: bool = False,
    buyer_intent_ads_check: bool = False,
    review_snapshot_store: ReviewSnapshotStore | None = None,
    suppression_index: AbstractSet[str] | None = None,
    per_lead_timeout: float = PER_LEAD_ENRICH_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    lead = normalize_lead(row, fallback_vertical=row.get("category", "") or "service")
    profile = resolve_profile(lead.vertical, source_query=lead.source_query)
    gate = hard_icp_gate(lead)

    website_url = row.get("website_url", "") or ""
    google_phone = row.get("phone", "")

    # Check the phone Google already gave us before spending anything. The full
    # crawl costs page fetches and, with screenshots on, a browser launch — all
    # of it wasted on a business we are not allowed to contact. The scraped
    # emails and phones still get checked below for leads that survive this.
    suppressed_before_crawl = bool(suppression_index) and has_suppression_match(
        phones=[google_phone],
        suppression_index=suppression_index,
    )

    if suppressed_before_crawl:
        # "Unknown", not a new "Suppressed" status: Site Status is a typed
        # Airtable select, and an unmapped value normalizes to None and is
        # dropped silently. The dnc_suppressed flag carries the meaning.
        site = empty_site_result("Unknown", [DNC_SUPPRESSED_REASON], url=website_url)
    else:
        try:
            site = scrape_site(
                website_url,
                rendered_form_check=rendered_form_check,
                high_ticket_terms=profile.high_ticket_terms,
                started_at=started_at,
                per_lead_timeout=per_lead_timeout,
            )
        finally:
            reset_fetch_session()

    screenshots = {"desktop": "", "mobile": "", "error": ""}
    screenshot_url = site.get("final_url") or normalize_url(website_url)
    elapsed_after_static = elapsed_seconds(started_at)
    per_lead_timed_out = elapsed_after_static > per_lead_timeout or "per_lead_enrichment_timeout" in set(
        site.get("site_issue_flags") or []
    )

    should_check_rendered = (
        screenshot_url
        and not per_lead_timed_out
        and site.get("site_status") != "No Website"
        and (
            take_shots
            or rendered_site_check
            or "rendered_check_needed" in set(site.get("site_issue_flags") or [])
        )
    )

    if should_check_rendered:
        remaining_timeout_ms = max(1000, int((per_lead_timeout - elapsed_seconds(started_at)) * 1000))
        rendered_timeout_ms = min(25000, remaining_timeout_ms)
        if take_shots:
            desktop_path, mobile_path = screenshot_paths(
                row.get("business_name", "lead"),
                row.get("place_id", ""),
                screenshots_dir,
            )
            screenshots = take_screenshots(screenshot_url, desktop_path, mobile_path, timeout_ms=rendered_timeout_ms)
        else:
            screenshots = check_rendered_site(screenshot_url, timeout_ms=rendered_timeout_ms)
        site = apply_rendered_site_result(site, screenshots)

    # Last word on the visual scores: the browser pass can turn a static
    # failure into a loaded page, or add the 390px render the mobile score
    # needs, and either flips what the evidence supports.
    site = apply_visual_audit_scores(site)

    website_phones = site.get("phones_found", [])

    google_digits = normalize_digits(google_phone)
    website_digits = [normalize_digits(p) for p in website_phones]

    phone_consistency = "unknown"
    if google_digits and website_digits:
        phone_consistency = "matched" if google_digits in website_digits else "different"
    elif google_digits and not website_digits:
        phone_consistency = "google_only"
    elif not google_digits and website_digits:
        phone_consistency = "website_only"

    record = {
        "google": row,
        "website": site,
        "screenshots": screenshots,
        "derived": {
            "phone_consistency": phone_consistency,
            "has_email": bool(site.get("emails_found")),
            "has_form": bool(site.get("forms_found")),
            "contact_form_detected": site.get("contact_form_detected", "Unknown"),
            "estimate_form_detected": site.get("estimate_form_detected", "Unknown"),
            "form_strength": site.get("form_strength", "None"),
            "form_detection_method": site.get("form_detection_method", "none"),
            "has_cta": bool(site.get("cta_terms")),
            "cta_strength": site.get("cta_strength", "None"),
            "rendered_site_status": site.get("rendered_site_status", "Unknown"),
            "visual_review_needed": site.get("visual_review_needed", "No"),
            "has_social_links": bool(site.get("social_links")),
            "has_high_ticket_terms": bool(site.get("high_ticket_terms")),
            "non_standard_site_or_booking": is_non_standard_site_or_booking_url(website_url)
            or is_non_standard_site_or_booking_url(site.get("final_url", "")),
        },
    }

    hard_icp_skip = gate.should_skip
    hard_icp_reasons = list(gate.reasons)

    # Re-check against everything the crawl turned up. The pre-crawl check only
    # saw the Google phone; a business can be suppressed under an address or a
    # second line that only appears on the site.
    if suppression_index and (
        suppressed_before_crawl
        or has_suppression_match(
            emails=site.get("emails_found") or [],
            phones=(site.get("phones_found") or []) + [google_phone],
            suppression_index=suppression_index,
        )
    ):
        hard_icp_skip = True
        if DNC_SUPPRESSED_REASON not in hard_icp_reasons:
            hard_icp_reasons.append(DNC_SUPPRESSED_REASON)

    record["derived"]["hard_icp_skip"] = hard_icp_skip
    record["derived"]["hard_icp_reasons"] = hard_icp_reasons

    if review_snapshot_store is not None and lead.place_id_or_unique_key:
        # Record before reading. Enrichment is the one stage every entry point
        # runs, so this is where the observation has to be banked: without it
        # the store never accumulates two counts 14 days apart and
        # build_review_velocity is permanently stuck on "insufficient_history".
        # A snapshot failure must not lose the lead, so it is logged, not raised.
        if lead.google_review_count is not None:
            try:
                review_snapshot_store.record_snapshot(
                    place_key=lead.place_id_or_unique_key,
                    review_count=int(lead.google_review_count),
                    rating=float(lead.google_rating) if lead.google_rating else None,
                    vertical=infer_vertical_key(record, fallback=lead.vertical),
                    market=infer_market(record),
                    source="enrichment",
                )
            except Exception as exc:
                print(
                    f"[WARN] Review snapshot not recorded for {lead.business_name}: {exc}",
                    file=sys.stderr,
                )
        record["review_count_history"] = review_snapshot_store.history(lead.place_id_or_unique_key)
    total_elapsed = elapsed_seconds(started_at)
    record["enrichment"] = {"elapsed_seconds": round(total_elapsed, 1)}
    record["website"]["enrichment_elapsed_seconds"] = round(total_elapsed, 1)
    record["evidence"] = build_evidence_profile(record)
    build_buyer_intent(record, ads_check=buyer_intent_ads_check)
    record["enrichment"]["elapsed_seconds"] = round(elapsed_seconds(started_at), 1)
    record["website"]["enrichment_elapsed_seconds"] = record["enrichment"]["elapsed_seconds"]
    return record


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class JsonlAppender:
    """Append records to a JSONL file as they complete, flushing each one.

    Enrichment used to hold every record in memory and write once at the end,
    so a Ctrl-C or a failure in the final write threw away the whole run's
    scraping — the expensive part. Each line is flushed and fsynced, so an
    interrupted run leaves a valid, complete-as-far-as-it-got file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("w", encoding="utf-8")
        self.count = 0

    def append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self.count += 1

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()

    def __enter__(self) -> "JsonlAppender":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def build_enrichment_error_record(
    row: Dict[str, str],
    *,
    error: str,
    elapsed: float,
    review_snapshot_store: ReviewSnapshotStore | None = None,
    buyer_intent_ads_check: bool = False,
) -> Dict[str, Any]:
    """A record for a lead whose enrichment raised, so the lead is not lost.

    `enrich_leads.main` built one of these inline while `make_search_batch` only
    appended to its `errors` list and dropped the lead — the same failure with
    two different outcomes depending on which entry point you happened to run.
    A lead that failed to scrape still has a Google profile worth scoring, and
    site_status "Unknown" says honestly that nothing was observed.
    """
    record: Dict[str, Any] = {
        "google": row,
        "website": {
            "site_status": "Unknown",
            "site_issue_flags": ["enrichment_exception"],
            "error": error,
            "pages": {},
        },
        "screenshots": {},
        "derived": {},
        "enrichment": {"elapsed_seconds": round(elapsed, 1), "error": error},
    }
    place_key = str(row.get("place_id") or row.get("place_id_or_unique_key") or "").strip()
    if review_snapshot_store is not None and place_key:
        record["review_count_history"] = review_snapshot_store.history(place_key)
    record["evidence"] = build_evidence_profile(record)
    build_buyer_intent(record, ads_check=buyer_intent_ads_check)
    return record


def resolve_per_lead_timeout(workers: int) -> float:
    """Scale the per-lead ceiling to the number of concurrent workers.

    PER_LEAD_ENRICH_TIMEOUT_SECONDS is a wall-clock budget, and it was
    calibrated when leads ran one at a time, so wall-clock and work time were
    the same thing. Under concurrency they are not: bandwidth, CPU for parsing
    and the GIL inflate each lead's wall-clock without its actual work growing.
    Measured on 20 real plumbing leads, two sites that finished comfortably
    serially blew the 45s ceiling at 6 workers and were truncated mid-crawl —
    one lost 4 pages and 12k of text, and gained a false
    `manual_visual_review_needed` flag.

    Scaling by the worker count keeps the invariant the ceiling was protecting:
    the worst-case wall-clock for a whole run is no worse than the serial run it
    replaced. MAX_PER_LEAD_ENRICH_TIMEOUT_SECONDS still stops one hung site from
    holding a worker indefinitely.
    """
    if workers <= 1:
        return PER_LEAD_ENRICH_TIMEOUT_SECONDS
    return min(
        PER_LEAD_ENRICH_TIMEOUT_SECONDS * workers, MAX_PER_LEAD_ENRICH_TIMEOUT_SECONDS
    )


def resolve_enrich_workers(*, browser_work: bool) -> int:
    """How many leads to enrich at once.

    Enrichment is pure I/O with a per-lead timeout, so it ran one lead at a time
    against a wall of network latency. Browser work still gets its own default,
    but no longer for the original reason: the Chromium is per worker thread
    now, not per lead, so the launch cost is amortised and what is left is
    contention. Both defaults were measured on 50 real epoxy leads. Two numbers
    per path, because they disagree and only one of them decides the default:

    *per lead* is worker time summed over the 47 leads that are stable
    run-to-run, which repeat runs reproduce to within 2%. *batch* is that plus
    the 222s the three dead hosts spend in connect timeouts (dead on every
    network run, to the second), divided by the workers — what the run costs.

        workers   browser: per lead / batch    network: per lead / batch
              3         1.00x /  258s              1.00x /  159s
              4         1.20x /  222s              1.40x /  145s
              5         1.39x /  198s              1.91x /  142s
              6         1.98x /  219s              2.55x /  146s
             10             -                      4.88x /  147s

    Browser work parallelises because Chromium renders in its own processes:
    five workers cost 1.39x per lead for 1.67x the threads, and the batch is
    fastest there. Six is break-even with three, so five is a knee, not a
    ceiling chosen for headroom.

    The network path does not parallelise. Its BeautifulSoup parsing is
    in-process Python behind one GIL, and per-lead cost rises slightly *faster*
    than the worker count, so on leads that actually crawl, fewer workers is
    better. What flattens its batch column is the dead hosts — they are pure
    waiting, and more workers genuinely do compress them. Those two effects
    cancel at this batch's 3-in-50 dead-host rate, which is why measured wall
    clock is flat from 4 workers to 12 (185-210s). So six is a defensible
    middle rather than an optimum: raising it to ten gains nothing and doubles
    per-lead latency, and a batch with a different dead-host rate would move
    the balance. Do not read the flatness as headroom.

    Neither limit is memory. Peak PSS across the process tree was 3.9 GB at
    five browser workers, and it is a transient set by which heavy sites happen
    to render together rather than something that accumulates over a run, so it
    does not grow with batch length.

    An explicit ENRICH_WORKERS is always honoured; both numbers above are only
    defaults.
    """
    explicit = os.getenv("ENRICH_WORKERS", "").strip()
    if explicit:
        return max(1, getenv_int("ENRICH_WORKERS", DEFAULT_ENRICH_WORKERS))
    return DEFAULT_BROWSER_ENRICH_WORKERS if browser_work else DEFAULT_ENRICH_WORKERS


def enrich_rows_concurrently(
    rows: List[Dict[str, str]],
    *,
    workers: int,
    enrich: Any,
    on_complete: Any,
) -> None:
    """Enrich *rows* with a bounded pool, calling ``on_complete`` per finished lead.

    ``on_complete(index, row, record, error, elapsed)`` runs on the main thread
    in completion order, so callers do not need their own locking to log or
    persist. Records therefore land in completion order, not input order.
    """
    total = len(rows)
    if total == 0:
        return
    if workers <= 1:
        try:
            for index, row in enumerate(rows, start=1):
                started_at = time.monotonic()
                try:
                    on_complete(index, row, enrich(row), "", elapsed_seconds(started_at))
                except Exception as exc:
                    on_complete(index, row, None, str(exc), elapsed_seconds(started_at))
        finally:
            close_thread_browser()
        return

    started_at_by_index: Dict[int, float] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    interrupted = False
    try:
        futures = {}
        for index, row in enumerate(rows, start=1):
            started_at_by_index[index] = time.monotonic()
            futures[pool.submit(enrich, row)] = (index, row)
        try:
            for future in as_completed(futures):
                index, row = futures[future]
                elapsed = elapsed_seconds(started_at_by_index[index])
                try:
                    on_complete(index, row, future.result(), "", elapsed)
                except Exception as exc:
                    on_complete(index, row, None, str(exc), elapsed)
        except KeyboardInterrupt:
            # All rows are submitted up front, so the pool's default shutdown
            # (wait=True, cancel_futures=False) would block here until every
            # queued lead finishes -- the whole remaining batch, not just what
            # is already running. cancel_futures drops anything not yet
            # started so the interrupt is prompt; the up to `workers` leads
            # already mid-enrichment are left to finish in the `finally`
            # below, since a thread cannot be killed mid-request.
            interrupted = True
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    finally:
        if not interrupted:
            # Skipped on Ctrl-C: the pool is already shutting down there, so a
            # submit would raise and mask the interrupt. Those browsers die
            # with the process.
            close_worker_browsers(pool, workers)
        pool.shutdown(wait=True)


def close_worker_browsers(pool: ThreadPoolExecutor, workers: int) -> None:
    """Close each worker's Chromium on the thread that opened it.

    Playwright's sync API is bound to its creating thread, so the main thread
    cannot close a worker's browser -- the worker has to do it. A barrier is
    what makes the hand-out fair: a thread that has taken a closing task waits
    there instead of coming back for a second one while a peer still holds an
    open browser. The party count is the pool's *actual* width, not the
    configured one -- a pool only spawns a thread when no idle one is free, so
    a short run never grows to `workers` and a barrier that waited for them
    would spend its whole timeout at the end of every such run. Every task
    submitted here lands on an existing thread, since they are all idle by now.
    Closing a thread that has no browser is a no-op.
    """
    threads = getattr(pool, "_threads", None)
    parties = max(1, min(len(threads) if threads else workers, workers))
    barrier = threading.Barrier(parties)

    def close_one() -> None:
        try:
            barrier.wait(timeout=BROWSER_SHUTDOWN_RENDEZVOUS_SECONDS)
        except threading.BrokenBarrierError:
            pass
        close_thread_browser()

    for future in [pool.submit(close_one) for _ in range(parties)]:
        try:
            future.result()
        except Exception:
            pass


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input raw places CSV")
    parser.add_argument("--output", required=True, help="Output enriched JSONL")
    parser.add_argument("--screenshots-dir", default="screenshots", help="Screenshot directory")
    parser.add_argument("--limit", type=int, default=0, help="Optional max leads to enrich")
    parser.add_argument("--no-screenshots", action="store_true", help="Skip screenshots")
    parser.add_argument(
        "--review-snapshot-db",
        default=os.getenv("REVIEW_SNAPSHOT_DB", "run_state/review_velocity.sqlite3"),
        help="SQLite path containing longitudinal Google review-count observations",
    )
    parser.add_argument(
        "--rendered-form-check",
        action="store_true",
        help="Use Playwright DOM rendering for uncertain form detection without taking screenshots",
    )
    parser.add_argument(
        "--rendered-site-check",
        action="store_true",
        help="Use Playwright browser rendering to classify loaded/broken/timeout site status without requiring screenshots",
    )
    args = parser.parse_args()

    # After parse_args, so --help works, and reported as an [ERROR] rather than
    # an unhandled traceback — this fires on every run until the operator
    # configures a suppression source, so it has to read as a config problem.
    try:
        suppression_index = load_configured_suppression_index()
    except ComplianceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    input_path = Path(args.input)
    output_path = Path(args.output)
    screenshots_dir = Path(args.screenshots_dir)

    if not input_path.exists():
        print(f"[ERROR] Missing input file: {input_path}", file=sys.stderr)
        return 1

    # Raised 40 -> 200 on 2026-08-05. Unlike the LLM caps this one *discards
    # leads* rather than stopping short, so an undersized value is a silent
    # yield loss: a 133-lead batch under the old 40 threw away 93 already-paid-
    # for Places results. 200 clears a full metro batch with headroom.
    # NOTE: .env sets MAX_WEBSITES_PER_RUN=90, which still binds ahead of this.
    max_websites = getenv_int("MAX_WEBSITES_PER_RUN", 200)

    rows = read_csv(input_path)

    if args.limit > 0:
        rows = rows[: args.limit]

    if len(rows) > max_websites:
        # This used to truncate silently, so a run could drop leads with no
        # trace. The cap counts every lead, not only those with a website,
        # despite its name.
        print(f"[STOP] Hit MAX_WEBSITES_PER_RUN={max_websites} (dropped {len(rows) - max_websites} leads)")
        rows = rows[:max_websites]

    print(f"[INFO] Loaded {len(rows)} leads from {input_path}")
    print(f"[INFO] MAX_WEBSITES_PER_RUN={max_websites}")
    print(f"[INFO] Screenshots={'off' if args.no_screenshots else 'on'}")
    print(f"[INFO] Rendered form check={'on' if args.rendered_form_check else 'off'}")
    print(f"[INFO] Rendered site check={'on' if args.rendered_site_check else 'off'}")
    buyer_intent_ads_check = getenv_bool("BUYER_INTENT_ADS_CHECK", False)
    print(f"[INFO] Buyer-intent ads check={'on' if buyer_intent_ads_check else 'off'}")

    review_snapshot_store = ReviewSnapshotStore(args.review_snapshot_db)
    browser_work = not args.no_screenshots or args.rendered_form_check or args.rendered_site_check
    workers = resolve_enrich_workers(browser_work=browser_work)
    per_lead_timeout = resolve_per_lead_timeout(workers)
    print(f"[INFO] Enrichment workers={workers} per_lead_timeout={per_lead_timeout:.0f}s")

    def enrich(row: Dict[str, str]) -> Dict[str, Any]:
        return enrich_row(
            row,
            screenshots_dir,
            take_shots=not args.no_screenshots,
            rendered_form_check=args.rendered_form_check,
            rendered_site_check=args.rendered_site_check,
            buyer_intent_ads_check=buyer_intent_ads_check,
            review_snapshot_store=review_snapshot_store,
            suppression_index=suppression_index,
            per_lead_timeout=per_lead_timeout,
        )

    # Records are written as they finish rather than accumulated, so an
    # interrupted run keeps the scraping it already paid for.
    with JsonlAppender(output_path) as appender:

        def on_complete(
            index: int,
            row: Dict[str, str],
            record: Dict[str, Any] | None,
            error: str,
            elapsed: float,
        ) -> None:
            name = row.get("business_name", "")
            website = row.get("website_url", "")
            if record is None:
                print(f"[ERROR] Enrichment failed for {name}: {error}", file=sys.stderr)
                record = build_enrichment_error_record(
                    row,
                    error=error,
                    elapsed=elapsed,
                    review_snapshot_store=review_snapshot_store,
                    buyer_intent_ads_check=buyer_intent_ads_check,
                )
            appender.append(record)
            reported = float(record.get("enrichment", {}).get("elapsed_seconds") or elapsed)
            fields = enrichment_log_fields(record, fallback_error=error)
            print(
                f"[ENRICH {index}/{len(rows)}] {name or 'unknown'} "
                f"elapsed={format_elapsed(reported)}s "
                f"site_status={fields['site_status']} "
                f"pages_checked={fields['pages_checked']} "
                f"errors={fields['error']}",
                flush=True,
            )
            if reported > SLOW_ENRICH_SECONDS:
                print(
                    f"[SLOW ENRICH] {name or 'unknown'} elapsed={format_elapsed(reported)}s website={website}",
                    flush=True,
                )

        try:
            enrich_rows_concurrently(rows, workers=workers, enrich=enrich, on_complete=on_complete)
        except KeyboardInterrupt:
            print(
                f"[STOP] Interrupted; kept {appender.count} of {len(rows)} enriched leads in {output_path}",
                file=sys.stderr,
            )
            return 1

    print(f"[DONE] Enriched leads: {appender.count}")
    print(f"[DONE] Wrote: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
