from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from upload_airtable import (
    fetch_existing_records,
    payload_field_names,
    send_airtable_plan,
    validate_airtable_fields,
)

from .airtable_sync import (
    AIRTABLE_ALLOWED_FIELDS,
    TYPED_AIRTABLE_FIELDS,
    VERTICAL_LABELS,
    normalize_airtable_value,
    plan_airtable_sync,
)
from .email_hygiene import clean_airtable_email
from .queue_state import PROSPECT_STATUS_CHOICES
from .schema import normalize_phone
from .sendability import MANUAL_CARD_UNVERIFIED_BLOCKER, admit
from .verticals import clean_vertical_label


MANUAL_FIELDS = [
    "Business Name",
    "Owner Name",
    "Vertical",
    "City",
    "Phone",
    "Email",
    "Website URL",
    "Google Maps URL",
    "Reviews",
    "Rating",
    "Site Status",
    "Specific Observation",
    "Packet Eligible",
    "Sendable",
    "Packet Tier",
    "Lead Priority",
    "Offer Fit",
    "Visible Pain Score",
    "Visible Pain Reasons",
    "Send Trigger",
    "Buyer Intent Signals",
    "AI Confidence",
    "Score Total",
    "Opener",
    "Send Blockers",
    "Status",
    "Outreach Channel",
    "Place Key",
    "Manual Notes",
]

LLM_FIELD_SET = set(MANUAL_FIELDS) - {
    "Vertical",
    "Packet Eligible",
    "Sendable",
    "Packet Tier",
    "Status",
    "Place Key",
}
DEFAULT_OUTPUT_DIR = Path("data/manual_leads")
URL_FIELDS = {"Website URL", "Google Maps URL", "Mockup URL", "Video URL"}
HTTP_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)
NO_STANDALONE_SITE_RE = re.compile(
    r"\b(no standalone website|no website|facebook only|google business profile links to facebook)\b",
    re.IGNORECASE,
)
EMAIL_FIELDS = {"Email"}
CONTROLLED_SELECT_LABELS = {
    "Packet Eligible": {"Yes", "No"},
    "Sendable": {"Yes", "No"},
    "Packet Tier": {"Sendable Packet", "Manual Review Packet", "Watchlist", "No Packet"},
    "Lead Priority": {"A", "B", "C"},
    "Status": set(PROSPECT_STATUS_CHOICES),
    "Outreach Channel": {"Email", "Phone", "Instagram DM", "SMS"},
}
# Vertical is validated through the shared registry (VERTICAL_LABELS), not a
# hardcoded list, so new lanes work here the moment they exist in verticals.py
# or the identity aliases.


def resolve_manual_vertical(value: str) -> str:
    resolved = VERTICAL_LABELS.get(clean_vertical_label(value))
    if not resolved:
        known = ", ".join(sorted(set(VERTICAL_LABELS.values())))
        raise ValueError(f"Unknown vertical '{value}'. Known verticals: {known}")
    return resolved
PLACEHOLDER_VALUES = {
    "not found",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "not available",
    "not listed",
    "missing",
    "-",
}


def clean_cell(value: Any) -> str:
    return str(value or "").strip()


def clean_placeholder(value: Any) -> str:
    cleaned = clean_cell(value)
    normalized = re.sub(r"\s+", " ", cleaned).strip().casefold()
    return "" if normalized in PLACEHOLDER_VALUES else cleaned


def slug(text: str) -> str:
    text = clean_cell(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_domain(url: str) -> str:
    value = normalize_url_value(url).lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    domain = parsed.netloc or parsed.path.split("/", 1)[0]
    domain = domain.removeprefix("www.")
    return domain.rstrip("/")


def _first_match(pattern: str, text: str, flags: int = re.IGNORECASE) -> str:
    match = re.search(pattern, text, flags)
    return clean_placeholder(match.group(1)) if match else ""


def _clean_url(url: str) -> str:
    return normalize_url_value(url)


def _label_value(text: str, labels: Iterable[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    return _first_match(rf"^\s*(?:{label_pattern})\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)


def _contains_attempted_http_url(value: str) -> bool:
    return bool(re.search(r"https?://", value, re.IGNORECASE))


def _url_validation_error(field: str, value: str) -> str:
    return validate_url_value(field, value)


def _valid_http_url_from_value(field: str, value: str) -> str:
    raw = clean_placeholder(value)
    if not raw:
        return ""
    markdown_target = _markdown_target(raw)
    candidates = [markdown_target] if markdown_target else []
    candidates.extend(match.group(0) for match in HTTP_URL_RE.finditer(raw))
    for candidate in candidates:
        candidate = normalize_url_value(candidate)
        if candidate and not _url_validation_error(field, candidate):
            return candidate
    return ""


def _normalize_labelled_url_value(field: str, value: str) -> str:
    raw = clean_placeholder(value)
    if not raw:
        return ""
    markdown_target = _markdown_target(raw)
    if markdown_target:
        valid_markdown_url = _valid_http_url_from_value(field, markdown_target)
        if valid_markdown_url:
            return valid_markdown_url
    if re.match(r"\s*https?://", raw, re.IGNORECASE) and _url_validation_error(field, raw):
        return normalize_url_value(raw)
    valid_url = _valid_http_url_from_value(field, raw)
    if valid_url:
        return valid_url
    if _contains_attempted_http_url(raw):
        return normalize_url_value(raw)
    return ""


def _is_google_maps_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    path = parsed.path.casefold()
    query = parsed.query.casefold()
    return (
        host == "maps.app.goo.gl"
        or host.startswith("maps.google.")
        or (host in {"google.com", "www.google.com"} and ("/maps" in path or "cid=" in query or "place_id:" in query))
        or (host.endswith(".google.com") and ("/maps" in path or "cid=" in query or "place_id:" in query))
    )


def _no_standalone_site_fact(text: str) -> str:
    match = NO_STANDALONE_SITE_RE.search(text)
    return match.group(1) if match else ""


def _markdown_target(value: str) -> str:
    match = re.search(r"\[[^\]]*]\(([^)]+)\)", value)
    return clean_cell(match.group(1)) if match else ""


def normalize_url_value(value: Any) -> str:
    raw = clean_placeholder(value)
    if not raw:
        return ""
    markdown_target = _markdown_target(raw)
    if markdown_target:
        raw = markdown_target
    raw = raw.strip().strip("<>\"'")
    if "](" in raw:
        raw = raw.split("](", 1)[0]
    raw = raw.rstrip(").,;")
    if raw.lower().startswith("www."):
        raw = f"https://{raw}"
    return raw


def validate_url_value(field: str, value: Any) -> str:
    url = normalize_url_value(value)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return f"{field} must be a valid http or https URL: {value}"
    if any(char.isspace() for char in url) or any(char in parsed.netloc for char in "[]()"):
        return f"{field} must be a valid http or https URL: {value}"
    return ""


def normalize_email_value(value: Any) -> str:
    raw = clean_placeholder(value)
    if not raw:
        return ""
    markdown_target = _markdown_target(raw)
    if markdown_target:
        raw = markdown_target
    raw = raw.strip().strip("<>\"'")
    if raw.lower().startswith("mailto:"):
        raw = raw.split(":", 1)[1]
    raw = raw.split("?", 1)[0].strip()
    match = re.search(r"\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b", raw, re.IGNORECASE)
    return match.group(1) if match else raw


def validate_email_value(field: str, value: Any) -> str:
    email = normalize_email_value(value)
    if not email:
        return ""
    if not re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", email, re.IGNORECASE):
        return f"{field} must be a valid email address: {value}"
    return ""


def _extract_business_name(text: str) -> str:
    explicit = _first_match(r"^\s*(?:business|business name|name)\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if explicit:
        return explicit
    for line in text.splitlines():
        line = clean_cell(line)
        if line and not re.search(r":|phone|email|website|google|reviews?|stars?", line, re.IGNORECASE):
            return line
    return ""


def _extract_owner_name(text: str) -> str:
    owner = _first_match(r"^\s*owner\s*(?:maybe)?\s*:?\s*([A-Z][A-Za-z .'-]+)\s*$", text, re.IGNORECASE | re.MULTILINE)
    if not owner:
        owner = _first_match(r"\bowner maybe\s+([A-Z][A-Za-z .'-]+)", text, re.IGNORECASE)
    return clean_placeholder(owner)


def _extract_city(text: str) -> str:
    city = _first_match(r"^\s*city\s*:\s*([A-Za-z .'-]+)", text, re.IGNORECASE | re.MULTILINE)
    if city:
        return city
    for line in text.splitlines():
        line = clean_cell(line)
        if not line or ":" in line:
            continue
        if re.search(r"\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+)*\s*/\s*[A-Z][a-z]+", line):
            return clean_cell(line.split("/", 1)[0])
    return ""


def _extract_phone(text: str) -> str:
    return _first_match(
        r"(?:phone\s*:?\s*)?(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})",
        text,
    )


def _extract_email(text: str) -> str:
    value = _first_match(r"^\s*email\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if not value:
        value = _first_match(r"\[[^\]]*]\((mailto:[^)]+)\)", text, re.IGNORECASE)
    if not value:
        value = _first_match(r"\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b", text, re.IGNORECASE)
    return normalize_email_value(value)


def _extract_website(text: str) -> str:
    labelled_website = _label_value(text, ("website", "site"))
    website_url = _normalize_labelled_url_value("Website URL", labelled_website)
    if website_url:
        return website_url

    booking_url = _normalize_labelled_url_value("Website URL", _label_value(text, ("booking link", "booking url", "booking")))
    if booking_url:
        return booking_url

    social_url = _normalize_labelled_url_value(
        "Website URL",
        _label_value(text, ("facebook", "facebook url", "social", "social url", "instagram", "instagram url")),
    )
    if social_url:
        return social_url

    if labelled_website and _contains_attempted_http_url(labelled_website):
        return normalize_url_value(labelled_website)

    for match in HTTP_URL_RE.finditer(text):
        candidate = match.group(0)
        lowered = candidate.lower()
        if "google." not in lowered and "maps." not in lowered and not _url_validation_error("Website URL", candidate):
            return _clean_url(candidate)
    return ""


def _extract_google_maps_url(text: str) -> str:
    labelled_google_maps = _label_value(text, ("google", "maps", "google maps"))
    url = _normalize_labelled_url_value("Google Maps URL", labelled_google_maps)
    if url and _is_google_maps_url(url):
        return url
    if labelled_google_maps and _contains_attempted_http_url(labelled_google_maps) and not url:
        return normalize_url_value(labelled_google_maps)
    for match in HTTP_URL_RE.finditer(text):
        candidate = normalize_url_value(match.group(0))
        if candidate and _is_google_maps_url(candidate) and not _url_validation_error("Google Maps URL", candidate):
            return candidate
    return ""


def _extract_rating_reviews(text: str) -> tuple[str, str]:
    rating = ""
    reviews = ""
    match = None
    for line in text.splitlines():
        if re.search(r"\b(stars?|rating|reviews?)\b", line, re.IGNORECASE):
            match = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:stars?|rating)?\D{0,30}?(\d[\d,]*)\s*reviews?",
                line,
                re.IGNORECASE,
            )
            if match:
                break
    if match:
        rating = match.group(1)
        reviews = match.group(2).replace(",", "")
    else:
        rating = _first_match(r"\brating\s*:?\s*(\d+(?:\.\d+)?)", text)
        reviews = _first_match(r"\breviews?\s*:?\s*(\d[\d,]*)", text).replace(",", "")
    return rating, reviews


def _infer_observation(text: str) -> str:
    lines = [clean_cell(line) for line in text.splitlines() if clean_cell(line)]
    evidence_lines = [
        line for line in lines
        if not re.match(r"^(business|owner|phone|email|website|google|reviews?)\b", line, re.IGNORECASE)
    ]
    no_site_lines = [line for line in lines if NO_STANDALONE_SITE_RE.search(line)]
    for line in no_site_lines:
        if line not in evidence_lines:
            evidence_lines.append(line)
    summary = "; ".join(evidence_lines[-3:] or lines[-3:])
    return summary[:500]


def _infer_site_fields(text: str) -> Dict[str, str]:
    lowered = text.lower()
    fields: Dict[str, str] = {}
    no_site_fact = _no_standalone_site_fact(text)
    if no_site_fact:
        if re.search(r"\b(facebook only|google business profile links to facebook)\b", text, re.IGNORECASE):
            fields["Site Status"] = "Social Only"
        else:
            fields["Site Status"] = "Missing"
    if "no estimate form" in lowered:
        # Keep the actionable evidence in the surviving CRM fields. The raw
        # form diagnostic remains in local artifacts rather than a retired
        # Airtable column.
        fields["Visible Pain Reasons"] = "No estimate form detected"
        fields["Send Trigger"] = "No estimate form detected"
    if any(term in lowered for term in ("ceramic", "paint correction", "ppf", "epoxy", "coating", "garage floor")):
        fields["Buyer Intent Signals"] = "High-ticket service term in manual notes"
    return fields


def parse_raw_lead_regex(text: str) -> Dict[str, str]:
    rating, reviews = _extract_rating_reviews(text)
    fields: Dict[str, str] = {
        "Business Name": _extract_business_name(text),
        "Owner Name": _extract_owner_name(text),
        "City": _extract_city(text),
        "Phone": _extract_phone(text),
        "Email": _extract_email(text),
        "Website URL": _extract_website(text),
        "Google Maps URL": _extract_google_maps_url(text),
        "Reviews": reviews,
        "Rating": rating,
        "Specific Observation": _infer_observation(text),
    }
    fields.update(_infer_site_fields(text))
    return {key: value for key, value in fields.items() if clean_cell(value)}


def validate_llm_fields(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("LLM extraction must be a JSON object")
    cleaned: Dict[str, str] = {}
    for key, value in data.items():
        if key not in LLM_FIELD_SET:
            raise ValueError(f"LLM extraction returned unexpected field: {key}")
        if value in ("", None, [], {}):
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned_value = normalize_manual_field_value(key, str(value).strip())
            if cleaned_value:
                cleaned[key] = cleaned_value
        elif isinstance(value, list):
            cleaned_value = ", ".join(str(item).strip() for item in value if clean_placeholder(item))
            cleaned_value = normalize_manual_field_value(key, cleaned_value)
            if cleaned_value:
                cleaned[key] = cleaned_value
        else:
            raise ValueError(f"LLM extraction field must be scalar or list: {key}")
    return cleaned


def normalize_manual_field_value(field: str, value: Any) -> str:
    cleaned = clean_placeholder(value)
    if not cleaned:
        return ""
    if field in URL_FIELDS:
        normalized_url = _normalize_labelled_url_value(field, cleaned)
        if field == "Google Maps URL" and normalized_url and not _is_google_maps_url(normalized_url):
            if _contains_attempted_http_url(cleaned) and _url_validation_error(field, cleaned):
                return normalize_url_value(cleaned)
            return ""
        return normalized_url
    if field in EMAIL_FIELDS:
        return normalize_email_value(cleaned)
    if field in TYPED_AIRTABLE_FIELDS:
        normalized = normalize_airtable_value(field, cleaned)
        return "" if normalized in ("", None, [], {}) else str(normalized)
    return cleaned


def _business_sections(raw_text: str) -> List[str]:
    sections: List[str] = []
    for match in re.finditer(r"^\s*business\s*:\s*(.+)$", raw_text, re.IGNORECASE | re.MULTILINE):
        business = clean_placeholder(match.group(1))
        if business:
            sections.append(business.casefold())
    return sections


def detect_multiple_businesses(raw_text: str) -> bool:
    sections = _business_sections(raw_text)
    return len(set(sections)) > 1


def extract_with_openai(raw_text: str, api_key: str, *, model: str) -> Dict[str, str]:
    schema_fields = sorted(LLM_FIELD_SET)
    prompt = (
        "Extract only fields explicitly present in this pasted local-business lead. "
        "Return one JSON object only. Do not infer or invent missing values. "
        f"Allowed keys: {', '.join(schema_fields)}.\n\nRAW:\n{raw_text[:6000]}"
    )
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You extract sparse Airtable lead fields as JSON."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI extraction failed {response.status_code}: {response.text[:500]}")
    content = response.json()["choices"][0]["message"]["content"]
    return validate_llm_fields(json.loads(content))


def _google_place_id(fields: Dict[str, str]) -> str:
    url = clean_cell(fields.get("Google Maps URL"))
    if not url:
        return ""
    for pattern in (r"place_id[:=]([A-Za-z0-9_-]+)", r"[?&]q=place_id:([A-Za-z0-9_-]+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def deterministic_place_key(fields: Dict[str, str]) -> str:
    place_id = _google_place_id(fields)
    if place_id:
        return f"place:{place_id}"
    phone = normalize_phone(fields.get("Phone", ""))
    if phone:
        return f"phone:{phone}"
    domain = normalize_domain(fields.get("Website URL", ""))
    if domain:
        return f"domain:{domain}"
    name = slug(fields.get("Business Name", ""))
    city = slug(fields.get("City", ""))
    if name or city:
        return f"name_city:{name}:{city}"
    return ""


def _merge_fields(base: Dict[str, str], overrides: Iterable[Dict[str, str]]) -> Dict[str, str]:
    merged = dict(base)
    for extra in overrides:
        for key, value in extra.items():
            value = normalize_manual_field_value(key, value)
            if value:
                merged[key] = value
    return merged


def _maybe_preserve_manual_notes(fields: Dict[str, str], raw_text: str) -> Dict[str, str]:
    updated = dict(fields)
    if "Manual Notes" in AIRTABLE_ALLOWED_FIELDS:
        updated["Manual Notes"] = raw_text
    else:
        existing = clean_cell(updated.get("Specific Observation"))
        notes = raw_text.strip()
        if notes and notes not in existing:
            updated["Specific Observation"] = f"{existing}\n\nManual notes:\n{notes}" if existing else notes
    return updated


def build_manual_lead_card(
    raw_text: str,
    *,
    vertical: str,
    status: str = "Manual Review",
    sendable: bool = False,
    no_llm: bool = True,
    openai_api_key: str = "",
    openai_model: str = "gpt-4.1-mini",
) -> Dict[str, Any]:
    vertical = resolve_manual_vertical(vertical)
    regex_fields = parse_raw_lead_regex(raw_text)
    llm_fields: Dict[str, str] = {}
    if not no_llm and openai_api_key:
        llm_fields = extract_with_openai(raw_text, openai_api_key, model=openai_model)

    # The operator flag says whether there is anything to admit; `admit` says
    # whether it may be. Hand-entering a lead used to skip sendability entirely
    # and write the verdict itself, which is a second sendable path — and one
    # that never saw the hard ICP gate.
    admission = admit(
        {**regex_fields, **llm_fields},
        () if sendable else (MANUAL_CARD_UNVERIFIED_BLOCKER,),
    )

    defaults = {
        "Vertical": vertical,
        "Status": status or "Manual Review",
        "Packet Eligible": "Yes",
        "Sendable": admission.sendable_flag,
        "Packet Tier": "Sendable Packet" if admission.admitted else "Manual Review Packet",
        "Lead Priority": "C",
        "AI Confidence": "medium" if llm_fields else "low",
    }
    fields = _merge_fields(defaults, [regex_fields, llm_fields])
    if not clean_cell(fields.get("Outreach Channel")):
        fields["Outreach Channel"] = "Phone" if clean_cell(fields.get("Phone")) else "Email" if clean_cell(fields.get("Email")) else ""
    normalized_fields: Dict[str, str] = {}
    for key, value in fields.items():
        normalized_value = normalize_manual_field_value(key, value)
        if normalized_value:
            normalized_fields[key] = normalized_value
    fields = normalized_fields
    email = clean_airtable_email(fields.get("Email", ""))
    if email:
        fields["Email"] = email
    else:
        fields.pop("Email", None)
    place_key = deterministic_place_key(fields)
    if not place_key:
        raise ValueError("Manual lead needs at least a business name, phone, website, or Google place id")
    fields["Place Key"] = place_key
    fields = _maybe_preserve_manual_notes(fields, raw_text)

    return {
        "place_id_or_unique_key": place_key,
        "upsert_key": place_key,
        "business_name": fields.get("Business Name", ""),
        "phone": fields.get("Phone", ""),
        "airtable_fields": fields,
    }


def validate_manual_lead_preflight(raw_text: str, card: Dict[str, Any]) -> List[str]:
    fields = card.get("airtable_fields") or {}
    errors: List[str] = []

    if detect_multiple_businesses(raw_text):
        errors.append("Input contains multiple distinct Business sections; split it into one file per business.")

    if not clean_cell(fields.get("Business Name")):
        errors.append("Business Name is required.")

    for field in URL_FIELDS:
        error = validate_url_value(field, fields.get(field, ""))
        if error:
            errors.append(error)

    for field in EMAIL_FIELDS:
        error = validate_email_value(field, fields.get(field, ""))
        if error:
            errors.append(error)

    for field in TYPED_AIRTABLE_FIELDS:
        value = fields.get(field, "")
        if clean_cell(value) and normalize_airtable_value(field, value) in ("", None, [], {}):
            errors.append(f"{field} has unsupported Airtable select/typed value: {value}")

    for field, allowed in CONTROLLED_SELECT_LABELS.items():
        value = clean_cell(fields.get(field, ""))
        if value and value not in allowed:
            allowed_values = ", ".join(sorted(allowed))
            errors.append(f"{field} has unsupported Airtable single-select value: {value}. Allowed: {allowed_values}")

    return errors


def print_preflight(errors: List[str]) -> None:
    if not errors:
        print("[VALIDATION] OK")
        return
    print(f"[VALIDATION] Errors: {len(errors)}")
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)


def plan_manual_airtable_sync(
    card: Dict[str, Any],
    *,
    existing_records_by_key: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    return plan_airtable_sync([card], existing_records_by_key=existing_records_by_key or {})


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_outputs(output_dir: Path, raw_text: str, card: Dict[str, Any], planned: List[Dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{stamp}.txt").write_text(raw_text, encoding="utf-8")

    csv_path = output_dir / "manual_leads.csv"
    fields = [field for field in MANUAL_FIELDS if field != "Manual Notes"]
    row = {field: clean_cell((card.get("airtable_fields") or {}).get(field)) for field in fields}
    exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

    dry_path = output_dir / f"dry_run_{stamp}.json"
    dry_path.write_text(json.dumps(planned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Insert one raw pasted manual lead into the Airtable lead pipeline.")
    parser.add_argument("--vertical", required=True, help="Lead vertical, e.g. detailing, epoxy, landscaping")
    parser.add_argument("--status", default="Manual Review")
    parser.add_argument("--input", default="", help="Read raw pasted lead text from this file instead of stdin")
    parser.add_argument("--sendable", action="store_true", help="Set Sendable = Yes; default is No")
    parser.add_argument("--llm", action="store_true", help="Opt in to OpenAI extraction when OPENAI_API_KEY is present")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic regex extraction only")
    parser.add_argument("--live", action="store_true", help="Write to Airtable. Default is dry-run only.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    parser.add_argument("--openai-model", default=os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-4.1-mini"))
    return parser


def _read_raw_input(path: str) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return sys.stdin.read()


def _explain_airtable_error(exc: RuntimeError) -> RuntimeError:
    message = str(exc)
    if "INVALID_MULTIPLE_CHOICE_OPTIONS" in message:
        match = re.search(r'"field"\s*:\s*"([^"]+)"|"([^"]+)"\s+does not accept', message)
        field = next((group for group in (match.groups() if match else ()) if group), "")
        suffix = f" Field/option missing: {field}." if field else ""
        return RuntimeError(f"{message} Airtable rejected a single-select option.{suffix}")
    return exc


def main() -> int:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        # A live invocation without its complete Airtable authority must fail before producing
        # local review artifacts. This keeps a rejected live attempt side-effect free and prevents
        # test/operator retries from polluting the append-only manual-lead history.
        if args.live and (not args.base_id or not args.table or not args.token):
            raise ValueError("AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required")

        raw_text = _read_raw_input(args.input)
        if not raw_text.strip():
            raise ValueError("No raw lead text supplied on stdin or --input")

        openai_key = os.getenv("OPENAI_API_KEY", "")
        use_llm = bool(openai_key) and args.llm and not args.no_llm
        card = build_manual_lead_card(
            raw_text,
            vertical=args.vertical,
            status=args.status,
            sendable=args.sendable,
            no_llm=not use_llm,
            openai_api_key=openai_key,
            openai_model=args.openai_model,
        )
        preflight_errors = validate_manual_lead_preflight(raw_text, card)
        print_preflight(preflight_errors)
        if preflight_errors:
            return 1

        dry_plan = plan_manual_airtable_sync(card, existing_records_by_key={})
        _write_outputs(Path(args.output_dir), raw_text, card, dry_plan)

        if not args.live:
            print(f"[DRY-RUN] Records: 1")
            print("[DRY-RUN] Airtable plan follows")
            print(json.dumps(dry_plan, ensure_ascii=False, indent=2))
            return 0

        validate_airtable_fields(args.base_id, args.table, args.token, payload_field_names([card]))
        existing = fetch_existing_records(args.base_id, args.table, args.token)
        planned = plan_manual_airtable_sync(card, existing_records_by_key=existing)
        for item in planned:
            print(f"[LIVE] {item['method']} {item.get('upsert_key') or item.get('place_key')}")
        send_airtable_plan(args.base_id, args.table, args.token, planned)
        print(f"[DONE] Synced records: {len(planned)}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, RuntimeError):
            exc = _explain_airtable_error(exc)
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
