from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse


EMAIL_SPLIT_RE = re.compile(r"[,;\s]+")
PLACEHOLDER_EMAILS = {
    "user@domain.com",
    "test@example.com",
    "example@example.com",
    "info@example.com",
}
SUPPRESSED_LOCAL_PARTS = {"noreply", "no-reply", "donotreply"}
# Domains that are platform/SaaS tools, never a real business contact address.
PLATFORM_DOMAINS = {"sentry.io", "webador.com", "wix.com", "squarespace.com"}


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
        text = parsed.netloc or parsed.path
    text = text.split("/", 1)[0].split(":", 1)[0].strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def split_emails(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        candidates = [str(item or "").strip() for item in value]
    else:
        candidates = [part.strip() for part in EMAIL_SPLIT_RE.split(str(value))]
    return [email for email in candidates if email]


def is_placeholder_email(email: Any) -> bool:
    normalized = str(email or "").strip().lower()
    if normalized in PLACEHOLDER_EMAILS:
        return True
    local, separator, _domain = normalized.partition("@")
    if not separator:
        return True
    return local in SUPPRESSED_LOCAL_PARTS


def _domain_matches(email_domain: str, business_domain: str) -> bool:
    if not email_domain or not business_domain:
        return False
    return email_domain == business_domain or email_domain.endswith(f".{business_domain}")


def clean_emails(emails: Any, *, business_domain: Any = "") -> list[str]:
    domain = normalize_domain(business_domain)
    seen: set[str] = set()
    matched: list[str] = []
    fallback: list[str] = []

    for email in split_emails(emails):
        normalized = email.strip().lower()
        if normalized in seen or is_placeholder_email(normalized):
            continue
        seen.add(normalized)
        email_domain = normalize_domain(normalized.rsplit("@", 1)[-1])
        if _domain_matches(email_domain, domain):
            matched.append(normalized)
        else:
            fallback.append(normalized)

    return matched + fallback


def emails_text(emails: Any, *, business_domain: Any = "") -> str:
    return ", ".join(clean_emails(emails, business_domain=business_domain))


def is_noisy_airtable_email(email: Any) -> bool:
    """Return True if the email is a platform/system address that should not sync to Airtable."""
    normalized = str(email or "").strip().lower()
    if is_placeholder_email(normalized):
        return True
    _local, sep, domain = normalized.partition("@")
    if not sep:
        return True
    base_domain = domain.split(".", 1)[-1] if domain.count(".") > 1 else domain
    if domain in PLATFORM_DOMAINS or base_domain in PLATFORM_DOMAINS:
        return True
    return False


def clean_airtable_email(value: Any) -> str:
    """Filter noisy/platform emails; return comma-joined real addresses or empty string."""
    real = [e for e in split_emails(value) if not is_noisy_airtable_email(e)]
    return ", ".join(real)


def sanitize_record_emails(record: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(record)
    google = updated.get("google", {}) or {}
    website = updated.get("website", {}) or {}
    business_domain = website.get("domain") or website.get("final_url") or google.get("website_url", "")
    if "emails_found" in website:
        website["emails_found"] = clean_emails(website.get("emails_found"), business_domain=business_domain)
        updated["website"] = website
    return updated
