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


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 15

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
    parsed = tldextract.extract(url)
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
    soup = BeautifulSoup(html or "", "lxml")
    return bool(soup.find("form") or soup.find("input") or soup.find("textarea"))


def detect_terms(text: str, terms: List[str]) -> List[str]:
    lowered = (text or "").lower()
    return sorted({term for term in terms if term in lowered})


def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "final_url": resp.url,
            "html": resp.text or "",
            "error": "",
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": "",
            "final_url": "",
            "html": "",
            "error": str(exc),
        }


def find_candidate_page(base_url: str, internal_links: List[str], keywords: List[str]) -> str:
    for link in internal_links:
        lowered = link.lower()
        if any(k in lowered for k in keywords):
            return link

    root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    fallback_paths = []
    for keyword in keywords:
        if keyword == "contact":
            fallback_paths += ["/contact", "/contact-us", "/get-estimate", "/request-estimate"]
        elif keyword == "service":
            fallback_paths += ["/services", "/our-services", "/service"]
        elif keyword == "about":
            fallback_paths += ["/about", "/about-us"]
        elif keyword == "gallery":
            fallback_paths += ["/gallery", "/portfolio", "/projects", "/project-pictures", "/work"]

    return urljoin(root, fallback_paths[0]) if fallback_paths else ""


def empty_site_result(status: str, flags: List[str], url: str = "", error: str = "") -> Dict[str, Any]:
    return {
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
        "cta_terms": [],
        "high_ticket_terms": [],
        "generic_copy_terms": [],
        "combined_text": "",
    }


def scrape_site(website_url: str) -> Dict[str, Any]:
    url = normalize_url(website_url)

    if not url:
        return empty_site_result("No Website", ["no_website"])

    flags: List[str] = []

    if is_non_standard_site_or_booking_url(url):
        flags.append("non_standard_site_or_booking_url")

    home = fetch_url(url)

    if not home["ok"]:
        return empty_site_result(
            "Broken Website",
            flags + ["homepage_fetch_failed"],
            home.get("final_url") or url,
            home.get("error") or "",
        )

    final_url = home["final_url"] or url
    html = home["html"]

    if is_non_standard_site_or_booking_url(final_url) and "non_standard_site_or_booking_url" not in flags:
        flags.append("non_standard_site_or_booking_url")

    title, meta = extract_title_meta(html)
    text = extract_visible_text(html)
    links = extract_links(final_url, html)

    emails, phones = extract_emails_phones(text, html)
    forms_found = detect_forms(html)
    ctas = detect_terms(text, CTA_TERMS)
    high_ticket = detect_terms(text, HIGH_TICKET_TERMS)
    generic_copy = detect_terms(text, GENERIC_COPY_TERMS)

    pages: Dict[str, Dict[str, Any]] = {}

    page_targets = {
        "contact": find_candidate_page(final_url, links["internal_links"], ["contact"]),
        "services": find_candidate_page(final_url, links["internal_links"], ["service"]),
        "about": find_candidate_page(final_url, links["internal_links"], ["about"]),
        "gallery": find_candidate_page(final_url, links["internal_links"], ["gallery"]),
    }

    combined_text_parts = [text]

    for page_name, page_url in page_targets.items():
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

        if detect_forms(page_html):
            forms_found = True

        ctas = sorted(set(ctas + detect_terms(page_text, CTA_TERMS)))
        high_ticket = sorted(set(high_ticket + detect_terms(page_text, HIGH_TICKET_TERMS)))
        generic_copy = sorted(set(generic_copy + detect_terms(page_text, GENERIC_COPY_TERMS)))

        pages[page_name] = {
            "url": page_url,
            "ok": result.get("ok"),
            "status_code": result.get("status_code"),
            "title": page_title,
            "meta": page_meta,
            "text": clean_text(page_text, 6000),
        }

        time.sleep(0.1)

    combined_text = clean_text(" ".join(combined_text_parts), 30000)

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
        site_status = "Loads"

    return {
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
        "cta_terms": ctas,
        "high_ticket_terms": high_ticket,
        "generic_copy_terms": generic_copy,
        "combined_text": combined_text,
    }


def screenshot_paths(business_name: str, place_id: str, screenshots_dir: Path) -> Tuple[Path, Path]:
    ident = safe_slug(f"{business_name}-{place_id[:8]}")
    return (
        screenshots_dir / f"{ident}-desktop.png",
        screenshots_dir / f"{ident}-mobile.png",
    )


def take_screenshots(url: str, desktop_path: Path, mobile_path: Path, timeout_ms: int = 25000) -> Dict[str, Any]:
    if not url:
        return {"desktop": "", "mobile": "", "error": "no_url"}

    desktop_path.parent.mkdir(parents=True, exist_ok=True)
    mobile_path.parent.mkdir(parents=True, exist_ok=True)

    result = {"desktop": "", "mobile": "", "error": ""}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            page = browser.new_page(
                viewport={"width": 1440, "height": 1200},
                user_agent=USER_AGENT,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(desktop_path), full_page=True)
            result["desktop"] = str(desktop_path)

            mobile = browser.new_page(
                viewport={"width": 390, "height": 1200},
                is_mobile=True,
                user_agent=USER_AGENT,
            )
            mobile.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            mobile.wait_for_timeout(1500)
            mobile.screenshot(path=str(mobile_path), full_page=True)
            result["mobile"] = str(mobile_path)

            browser.close()

    except PlaywrightTimeoutError as exc:
        result["error"] = f"timeout: {exc}"
    except Exception as exc:
        result["error"] = str(exc)

    return result


def normalize_digits(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def enrich_row(row: Dict[str, str], screenshots_dir: Path, take_shots: bool) -> Dict[str, Any]:
    lead = normalize_lead(row, fallback_vertical=row.get("category", "") or "service")
    gate = hard_icp_gate(lead)

    website_url = row.get("website_url", "") or ""
    site = scrape_site(website_url)

    screenshots = {"desktop": "", "mobile": "", "error": ""}
    screenshot_url = site.get("final_url") or normalize_url(website_url)

    if take_shots and screenshot_url and site.get("site_status") not in {"No Website", "Broken Website"}:
        desktop_path, mobile_path = screenshot_paths(
            row.get("business_name", "lead"),
            row.get("place_id", ""),
            screenshots_dir,
        )
        screenshots = take_screenshots(screenshot_url, desktop_path, mobile_path)

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
            "has_cta": bool(site.get("cta_terms")),
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

    records: List[Dict[str, Any]] = []

    for i, row in enumerate(rows, start=1):
        name = row.get("business_name", "")
        website = row.get("website_url", "")

        print(f"[{i}/{len(rows)}] {name} | {website or 'NO WEBSITE'}")

        try:
            enriched = enrich_row(row, screenshots_dir, take_shots=not args.no_screenshots)
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
