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
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, urlparse

import requests
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from lead_machine.evidence import build_evidence_profile
from lead_machine.icp import hard_icp_gate
from lead_machine.schema import normalize_lead
from lead_machine.verticals import resolve_profile


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 15
TLD_EXTRACTOR = tldextract.TLDExtract(cache_dir=False, suffix_list_urls=())

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

ESTIMATE_CTA_TERMS = [
    "estimate",
    "free estimate",
    "request estimate",
    "schedule estimate",
    "schedule an estimate",
    "quote",
    "free quote",
    "request a quote",
    "get a quote",
    "request service",
    "schedule",
    "appointment",
    "consultation",
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
]

CAPTCHA_TERMS = {"captcha", "recaptcha", "g-recaptcha"}
FORM_STRENGTH_RANK = {"None": 0, "Weak": 1, "Basic": 2, "Strong": 3}
VISUAL_UNKNOWN = "Unknown"
VISUAL_AUDIT_SCORE_FIELDS = [
    "visual_quality_score",
    "premium_perception_score",
    "mobile_visual_score",
    "hero_clarity_score",
    "cta_visibility_score",
    "gallery_visibility_score",
    "trust_proof_visibility_score",
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


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


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


def extract_title_meta(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html or "", "lxml")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    meta = ""
    meta_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta_tag and meta_tag.get("content"):
        meta = meta_tag["content"].strip()

    return clean_text(title, 300), clean_text(meta, 700)


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


def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 1) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
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
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
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
            if attempt < retries and (resp.status_code == 404 or resp.status_code >= 500):
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
        return "homepage_http_hard_failure", f"Homepage returned HTTP {code} after retry."

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


def find_candidate_pages(base_url: str, internal_links: List[str], keywords: List[str]) -> List[str]:
    root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    base_domain = domain_of(base_url)
    candidates: List[str] = []

    def add(url: str) -> None:
        absolute = urljoin(root, url)
        if domain_of(absolute) != base_domain:
            return
        if absolute not in candidates:
            candidates.append(absolute)

    for link in internal_links:
        lowered = link.lower()
        if any(k in lowered for k in keywords):
            add(link)

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

    for path in fallback_paths:
        add(path)

    return candidates


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


def scrape_site(
    website_url: str,
    *,
    rendered_form_check: bool = False,
    high_ticket_terms: List[str] | tuple[str, ...] | None = None,
) -> Dict[str, Any]:
    url = normalize_url(website_url)
    high_ticket_terms = high_ticket_terms or HIGH_TICKET_TERMS

    if not url:
        return empty_site_result("No Website", ["no_website"])

    flags: List[str] = []

    if is_non_standard_site_or_booking_url(url):
        flags.append("non_standard_site_or_booking_url")

    home = fetch_url(url)

    if not home["ok"]:
        hard_flag, hard_reason = classify_hard_static_failure(home)
        failure_flags = flags + ["static_fetch_failed"]
        if hard_flag:
            return empty_site_result(
                "Broken Website",
                failure_flags + [hard_flag],
                home.get("final_url") or url,
                hard_reason or home.get("error") or "",
            )
        return empty_site_result(
            "Unknown",
            failure_flags + ["rendered_check_needed", "manual_visual_review_needed"],
            home.get("final_url") or url,
            home.get("error") or "Static fetch failed; browser-rendered check needed before site-status claims.",
        )

    final_url = home["final_url"] or url
    html = home["html"]

    if is_non_standard_site_or_booking_url(final_url) and "non_standard_site_or_booking_url" not in flags:
        flags.append("non_standard_site_or_booking_url")

    title, meta = extract_title_meta(html)
    text = extract_visible_text(html)
    links = extract_links(final_url, html)

    emails, phones = extract_emails_phones(text, html)
    form_signals = detect_form_signals(html, page_text=text, page_url=final_url, method="static_html")
    ctas = detect_terms(text, CTA_TERMS)
    high_ticket = detect_terms(text, high_ticket_terms)
    generic_copy = detect_terms(text, GENERIC_COPY_TERMS)

    pages: Dict[str, Dict[str, Any]] = {}

    page_targets: List[Tuple[str, str]] = []
    seen_page_urls = set()

    def add_page_targets(urls: List[str], default_name: str) -> None:
        for page_url in urls:
            if not page_url or page_url in seen_page_urls:
                continue
            seen_page_urls.add(page_url)
            page_targets.append((default_name, page_url))

    add_page_targets(
        find_candidate_pages(final_url, links["internal_links"], ["contact", "estimate", "quote", "schedule", "request"]),
        "contact",
    )
    add_page_targets([find_candidate_page(final_url, links["internal_links"], ["service"])], "services")
    add_page_targets([find_candidate_page(final_url, links["internal_links"], ["about"])], "about")
    add_page_targets([find_candidate_page(final_url, links["internal_links"], ["gallery"])], "gallery")

    combined_text_parts = [text]

    for default_page_name, page_url in page_targets:
        if not page_url:
            continue

        # Stay on same business domain.
        if domain_of(page_url) != domain_of(final_url):
            continue

        result = fetch_url(page_url, timeout=DEFAULT_TIMEOUT)
        page_html = result.get("html") or ""
        page_text = extract_visible_text(page_html) if result.get("ok") else ""
        page_title, page_meta = extract_title_meta(page_html) if page_html else ("", "")

        page_emails, page_phones = extract_emails_phones(page_text, page_html)
        emails = sorted(set(emails + page_emails))
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
        }

        time.sleep(0.1)

    combined_text = clean_text(" ".join(combined_text_parts), 30000)
    cta_strength = classify_cta_strength(ctas, combined_text)

    if rendered_form_check and form_signals.get("form_strength_rank", 0) <= 1 and ctas:
        rendered_signals = detect_rendered_form(final_url)
        form_signals = merge_form_signals(form_signals, rendered_signals)

    forms_found = bool(form_signals.get("forms_found"))

    if not forms_found:
        flags.append("no_form_detected")
    if not ctas:
        flags.append("no_clear_cta_detected")
    if not emails:
        flags.append("no_email_detected")
    if not high_ticket:
        flags.append("no_high_ticket_terms_detected")

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
        "emails_found": emails,
        "phones_found": phones,
        "social_links": links["social_links"],
        "forms_found": forms_found,
        "contact_form_detected": form_signals.get("contact_form_detected", "No"),
        "estimate_form_detected": form_signals.get("estimate_form_detected", "No"),
        "form_strength": form_signals.get("form_strength", "None"),
        "form_detection_method": form_signals.get("form_detection_method", "none"),
        "form_fields": form_signals.get("form_fields", []),
        "form_provider_terms": form_signals.get("provider_terms", []),
        "has_recaptcha": form_signals.get("has_recaptcha", False),
        "cta_terms": ctas,
        "cta_strength": cta_strength,
        "high_ticket_terms": high_ticket,
        "generic_copy_terms": generic_copy,
        "combined_text": combined_text,
    }
    result.update(_empty_visual_audit_fields())
    return result


def detect_rendered_form(url: str, timeout_ms: int = 20000) -> Dict[str, Any]:
    signals = _empty_form_signals()
    signals["form_detection_method"] = "rendered_browser"

    if not url:
        return signals

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1366, "height": 1000}, user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            html = page.content()
            try:
                text = page.inner_text("body", timeout=3000)
            except Exception:
                text = ""
            browser.close()
        rendered = detect_form_signals(html, page_text=text, page_url=url, method="rendered_browser")
        if rendered.get("form_detection_method") != "embedded_provider":
            rendered["form_detection_method"] = "rendered_browser"
        return rendered
    except PlaywrightTimeoutError:
        return signals
    except Exception:
        return signals


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

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            page = browser.new_page(
                viewport={"width": 1440, "height": 1200},
                user_agent=USER_AGENT,
            )
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            status_code = response.status if response else 0
            final_url = page.url
            if status_code == 404 or status_code >= 500:
                result["rendered_site_status"] = "Broken"
                result["rendered_status_reason"] = f"Browser rendered homepage returned HTTP {status_code}."
                browser.close()
                return result
            if status_code >= 400:
                result["rendered_site_status"] = "Unknown"
                result["rendered_status_reason"] = (
                    f"Browser rendered homepage returned HTTP {status_code}; manual review needed before broken-site claims."
                )
                browser.close()
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
                mobile = browser.new_page(
                    viewport={"width": 390, "height": 1200},
                    is_mobile=True,
                    user_agent=USER_AGENT,
                )
                mobile_response = mobile.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                mobile.wait_for_timeout(1500)
                mobile_status = mobile_response.status if mobile_response else 0
                if mobile_status >= 400:
                    result["error"] = f"mobile_http_{mobile_status}"
                else:
                    mobile.screenshot(path=str(mobile_path), full_page=True)
                    result["mobile"] = str(mobile_path)

            browser.close()
            return result

    except Exception as exc:
        status, reason = _classify_rendered_exception(exc)
        result["rendered_site_status"] = status
        result["rendered_status_reason"] = reason
        result["error"] = reason
        return result


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
) -> Dict[str, Any]:
    lead = normalize_lead(row, fallback_vertical=row.get("category", "") or "service")
    profile = resolve_profile(lead.vertical, source_query=lead.source_query)
    gate = hard_icp_gate(lead)

    website_url = row.get("website_url", "") or ""
    site = scrape_site(website_url, rendered_form_check=rendered_form_check, high_ticket_terms=profile.high_ticket_terms)

    screenshots = {"desktop": "", "mobile": "", "error": ""}
    screenshot_url = site.get("final_url") or normalize_url(website_url)

    should_check_rendered = (
        screenshot_url
        and site.get("site_status") != "No Website"
        and (
            take_shots
            or rendered_site_check
            or "rendered_check_needed" in set(site.get("site_issue_flags") or [])
        )
    )

    if should_check_rendered:
        if take_shots:
            desktop_path, mobile_path = screenshot_paths(
                row.get("business_name", "lead"),
                row.get("place_id", ""),
                screenshots_dir,
            )
            screenshots = take_screenshots(screenshot_url, desktop_path, mobile_path)
        else:
            screenshots = check_rendered_site(screenshot_url)
        site = apply_rendered_site_result(site, screenshots)

    google_phone = row.get("phone", "")
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
            "hard_icp_skip": gate.should_skip,
            "hard_icp_reasons": gate.reasons,
        },
    }
    record["evidence"] = build_evidence_profile(record)
    return record


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input raw places CSV")
    parser.add_argument("--output", required=True, help="Output enriched JSONL")
    parser.add_argument("--screenshots-dir", default="screenshots", help="Screenshot directory")
    parser.add_argument("--limit", type=int, default=0, help="Optional max leads to enrich")
    parser.add_argument("--no-screenshots", action="store_true", help="Skip screenshots")
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

    input_path = Path(args.input)
    output_path = Path(args.output)
    screenshots_dir = Path(args.screenshots_dir)

    if not input_path.exists():
        print(f"[ERROR] Missing input file: {input_path}", file=sys.stderr)
        return 1

    max_websites = getenv_int("MAX_WEBSITES_PER_RUN", 40)

    rows = read_csv(input_path)

    if args.limit > 0:
        rows = rows[: args.limit]

    rows = rows[:max_websites]

    print(f"[INFO] Loaded {len(rows)} leads from {input_path}")
    print(f"[INFO] MAX_WEBSITES_PER_RUN={max_websites}")
    print(f"[INFO] Screenshots={'off' if args.no_screenshots else 'on'}")
    print(f"[INFO] Rendered form check={'on' if args.rendered_form_check else 'off'}")
    print(f"[INFO] Rendered site check={'on' if args.rendered_site_check else 'off'}")

    records: List[Dict[str, Any]] = []

    for i, row in enumerate(rows, start=1):
        name = row.get("business_name", "")
        website = row.get("website_url", "")

        print(f"[{i}/{len(rows)}] {name} | {website or 'NO WEBSITE'}")

        try:
            enriched = enrich_row(
                row,
                screenshots_dir,
                take_shots=not args.no_screenshots,
                rendered_form_check=args.rendered_form_check,
                rendered_site_check=args.rendered_site_check,
            )
            records.append(enriched)

            status = enriched["website"].get("site_status")
            flags = ",".join(enriched["website"].get("site_issue_flags", []))
            print(f"    status={status} flags={flags or 'none'}")

        except Exception as exc:
            print(f"[ERROR] Enrichment failed for {name}: {exc}", file=sys.stderr)
            error_record = {
                "google": row,
                "website": {
                    "site_status": "Unknown",
                    "site_issue_flags": ["enrichment_exception"],
                    "error": str(exc),
                },
                "screenshots": {},
                "derived": {},
            }
            error_record["evidence"] = build_evidence_profile(error_record)
            records.append(error_record)

        time.sleep(0.2)

    write_jsonl(output_path, records)

    print(f"[DONE] Enriched leads: {len(records)}")
    print(f"[DONE] Wrote: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
