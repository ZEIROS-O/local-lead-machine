#!/usr/bin/env python3
"""
Phase 8: make manual review packets.

Reads scored CSV files from score_leads.py.
Creates markdown packets for leads where Packet Eligible = Yes by default.

Run:
    python make_packets.py --inputs output/scored_hardscaping.csv output/scored_roofing.csv --output-dir packets
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

from lead_machine.email_hygiene import emails_text, sanitize_record_emails
from lead_machine.sendability import is_contact_ready


CORE_AIRTABLE_FIELDS = [
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
    "Current Website Quality",
    "Best Service Angle",
    "Specific Observation",
    "Mockup URL",
    "Video URL",
    "Outreach Channel",
    "Status",
    "Last Contacted",
    "Next Follow-Up",
]

EXTRA_FIELDS = [
    "Lead Priority",
    "Offer Fit",
    "Site Status",
    "Evidence Notes",
    "AI Confidence",
    "Manual Reviewed",
    "Skip Reason",
    "Rendered Site Status",
    "Rendered Status Reason",
    "Visual Review Needed",
    "Visual Quality Score",
    "Premium Perception Score",
    "Mobile Visual Score",
    "Hero Clarity Score",
    "CTA Visibility Score",
    "Gallery Visibility Score",
    "Trust Proof Visibility Score",
    "Visual Pain Reasons",
    "Desktop Screenshot Path",
    "Mobile Screenshot Path",
    "Score Total",
    "Score Strength",
    "Score Weakness",
    "Score Offer",
    "Score ICP Fit",
    "Score Reputation",
    "Score Web Pain",
    "Score Mockup Leverage",
    "Score Reachability",
    "Score Offer Clarity",
    "Score Evidence Confidence",
    "Recommended Action",
    "Why This Lead",
    "Why This Might Be Wrong",
    "Opener",
    "Evidence Refs",
    "Safe Outreach Claims",
    "Unsafe Claims",
    "Place Key",
    "Fit Ceiling",
    "Fit Flags",
    "Review Fit Band",
    "Review Demand Band",
    "Packet Tier",
    "Manual Review Reason",
    "Packet Eligible",
    "Packet Eligibility Reason",
    "Sendable",
    "Minimum Evidence Passed",
    "Send Blockers",
    "Sendability Blockers",
]

REVIEW_PROMPT_SUPPRESSING_FIELDS = [
    "Score ICP Fit",
    "Score Reputation",
    "Score Web Pain",
    "Score Mockup Leverage",
    "Score Reachability",
    "Score Offer Clarity",
    "Score Evidence Confidence",
    "Recommended Action",
    "Why This Lead",
    "Why This Might Be Wrong",
    "Evidence Refs",
    "Safe Outreach Claims",
    "Unsafe Claims",
    "total_score",
    "grade",
    "best_angle",
    "confidence",
    "Total Score",
    "Grade",
    "Best Angle",
    "Confidence",
    "Sendable",
    "Minimum Evidence Passed",
    "Send Blockers",
    "Sendability Blockers",
]


def raise_csv_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_slug(text: str, fallback: str = "lead") -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:90] or fallback


def md_escape(text: Any) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def clip(text: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def read_rows(paths: List[Path]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)

        with path.open(newline="", encoding="utf-8") as f:
            for index, row in enumerate(csv.DictReader(f)):
                row["_source_file"] = str(path)
                row["_source_index"] = str(index)
                rows.append(sanitize_packet_row(row))

    return rows


def sanitize_packet_row(row: Dict[str, str]) -> Dict[str, str]:
    cleaned = dict(row)
    raw_evidence = parse_raw_evidence(cleaned)
    website = raw_evidence.get("website", {}) if raw_evidence else {}
    business_domain = (
        website.get("domain")
        or website.get("final_url")
        or cleaned.get("Website URL", "")
    )
    cleaned["Email"] = emails_text(cleaned.get("Email", ""), business_domain=business_domain)
    return cleaned


def parse_raw_evidence(row: Dict[str, str]) -> Dict[str, Any]:
    raw = row.get("Raw Evidence", "")
    if not raw:
        return {}

    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return sanitize_record_emails(loaded)
        return {}
    except Exception:
        return {}


# Tiers that produce an actual packet on disk. Sendable packets are ready for
# outreach; manual review packets are valid prospects that still need a
# human visual/form review before they are safe to cold-send.
PACKET_TIERS_WITH_PACKET = {"Sendable Packet", "Manual Review Packet"}


def is_eligible(row: Dict[str, str], include_all: bool, priority_filter: Optional[str]) -> bool:
    if priority_filter and row.get("Lead Priority") != priority_filter:
        return False

    if include_all:
        return True

    tier = row.get("Packet Tier", "").strip()
    if tier:
        if tier == "Sendable Packet" and not is_contact_ready(row):
            identity = row.get("Place Key") or f"{row.get('Business Name', '')}|{row.get('City', '')}"
            raise ValueError(f"Sendable Packet row {identity!r} fails the contact-readiness contract")
        return tier in PACKET_TIERS_WITH_PACKET

    # Fallback for rows scored before packet tiers existed.
    return is_contact_ready(row)


def core_tsv_draft(row: Dict[str, str]) -> str:
    values = []

    for field in CORE_AIRTABLE_FIELDS:
        value = row.get(field, "")
        value = str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")
        values.append(value)

    return "\t".join(values)


def field_table(row: Dict[str, str], fields: List[str]) -> str:
    lines = ["| Field | Value |", "|---|---|"]

    for field in fields:
        value = md_escape(row.get(field, ""))
        value = value.replace("\n", "<br>")
        lines.append(f"| {field} | {value} |")

    return "\n".join(lines)


def field_populated(value: Any) -> bool:
    cleaned = str(value or "").strip()
    return cleaned not in {"", "[]", "{}", "null", "None"}


def should_include_review_prompt(row: Dict[str, str]) -> bool:
    return not any(field_populated(row.get(field, "")) for field in REVIEW_PROMPT_SUPPRESSING_FIELDS)


def evidence_summary(row: Dict[str, str], evidence: Dict[str, Any]) -> str:
    website = evidence.get("website", {}) if evidence else {}
    google = evidence.get("google", {}) if evidence else {}
    derived = evidence.get("derived", {}) if evidence else {}
    screenshots = evidence.get("screenshots", {}) if evidence else {}

    homepage = website.get("homepage", {}) or {}
    pages = website.get("pages", {}) or {}

    contact = pages.get("contact", {}) or {}
    services = pages.get("services", {}) or {}
    about = pages.get("about", {}) or {}
    gallery = pages.get("gallery", {}) or {}

    lines: List[str] = []

    lines.append("## Extracted Evidence")
    lines.append("")
    lines.append(f"- **Source file:** `{row.get('_source_file', '')}`")
    lines.append(f"- **Site status:** {row.get('Site Status', '')}")
    lines.append(f"- **Website quality:** {row.get('Current Website Quality', '')}")
    lines.append(f"- **Offer fit:** {row.get('Offer Fit', '')}")
    lines.append(f"- **Lead priority:** {row.get('Lead Priority', '')}")
    lines.append(f"- **Review demand band:** {row.get('Review Demand Band', '') or row.get('Review Fit Band', '')}")
    lines.append(f"- **Fit flags:** {row.get('Fit Flags', '') or 'none'}")
    lines.append(f"- **Score:** {row.get('Score Total', '')} total / {row.get('Score Strength', '')} strength / {row.get('Score Weakness', '')} weakness / {row.get('Score Offer', '')} offer")
    lines.append("")

    lines.append("### Google / Maps Data")
    lines.append("")
    lines.append(f"- **Business:** {row.get('Business Name', '')}")
    lines.append(f"- **Vertical:** {row.get('Vertical', '')}")
    lines.append(f"- **City:** {row.get('City', '')}")
    lines.append(f"- **Phone:** {row.get('Phone', '')}")
    lines.append(f"- **Rating / Reviews:** {row.get('Rating', '')} / {row.get('Reviews', '')}")
    lines.append(f"- **Website:** {row.get('Website URL', '') or 'blank'}")
    lines.append(f"- **Google Maps:** {row.get('Google Maps URL', '') or 'blank'}")
    lines.append(f"- **Google category:** {google.get('category', '')}")
    lines.append(f"- **Address:** {google.get('address', '')}")
    lines.append("")

    lines.append("### Website Signals")
    lines.append("")
    lines.append(f"- **Final URL:** {website.get('final_url', '') or row.get('Website URL', '') or 'blank'}")
    lines.append(f"- **Domain:** {website.get('domain', '')}")
    lines.append(f"- **Flags:** {', '.join(website.get('site_issue_flags', []) or []) or 'none'}")
    lines.append(f"- **Emails found:** {', '.join(website.get('emails_found', []) or []) or 'none'}")
    lines.append(f"- **Phones found:** {', '.join(website.get('phones_found', []) or []) or 'none'}")
    lines.append(f"- **Phone consistency:** {derived.get('phone_consistency', '')}")
    lines.append(f"- **Form found:** {derived.get('has_form', '')}")
    lines.append(f"- **Contact form detected:** {website.get('contact_form_detected', '') or derived.get('contact_form_detected', '')}")
    lines.append(f"- **Estimate form detected:** {website.get('estimate_form_detected', '') or derived.get('estimate_form_detected', '')}")
    lines.append(f"- **Form strength:** {website.get('form_strength', '') or derived.get('form_strength', '')}")
    lines.append(f"- **Form detection method:** {website.get('form_detection_method', '') or derived.get('form_detection_method', '')}")
    lines.append(f"- **CTA found:** {derived.get('has_cta', '')}")
    lines.append(f"- **CTA strength:** {website.get('cta_strength', '') or derived.get('cta_strength', '')}")
    lines.append(f"- **Rendered site status:** {row.get('Rendered Site Status', '') or website.get('rendered_site_status', '') or evidence.get('rendered_site_status', '')}")
    lines.append(f"- **Rendered status reason:** {row.get('Rendered Status Reason', '') or website.get('rendered_status_reason', '') or evidence.get('rendered_status_reason', '')}")
    lines.append(f"- **Visual review needed:** {row.get('Visual Review Needed', '') or website.get('visual_review_needed', '') or evidence.get('visual_review_needed', '')}")
    lines.append(f"- **Visual quality score:** {row.get('Visual Quality Score', '') or evidence.get('visual_quality_score', '')}")
    lines.append(f"- **Premium perception score:** {row.get('Premium Perception Score', '') or evidence.get('premium_perception_score', '')}")
    lines.append(f"- **Mobile visual score:** {row.get('Mobile Visual Score', '') or evidence.get('mobile_visual_score', '')}")
    lines.append(f"- **Hero clarity score:** {row.get('Hero Clarity Score', '') or evidence.get('hero_clarity_score', '')}")
    lines.append(f"- **CTA visibility score:** {row.get('CTA Visibility Score', '') or evidence.get('cta_visibility_score', '')}")
    lines.append(f"- **Gallery visibility score:** {row.get('Gallery Visibility Score', '') or evidence.get('gallery_visibility_score', '')}")
    lines.append(f"- **Trust proof visibility score:** {row.get('Trust Proof Visibility Score', '') or evidence.get('trust_proof_visibility_score', '')}")
    lines.append(f"- **Visual pain reasons:** {row.get('Visual Pain Reasons', '') or '; '.join(evidence.get('visual_pain_reasons', []) or [])}")
    lines.append(f"- **Visible pain score:** {row.get('Visible Pain Score', '') or evidence.get('visible_pain_score', '')}")
    lines.append(f"- **Visible pain reasons:** {row.get('Visible Pain Reasons', '') or '; '.join(evidence.get('visible_pain_reasons', []) or [])}")
    lines.append(f"- **Send trigger:** {row.get('Send Trigger', '') or evidence.get('send_trigger', '')}")
    lines.append(f"- **High-ticket terms:** {', '.join(website.get('high_ticket_terms', []) or []) or 'none'}")
    lines.append(f"- **Generic copy terms:** {', '.join(website.get('generic_copy_terms', []) or []) or 'none'}")
    lines.append(f"- **Social links:** {', '.join(website.get('social_links', []) or []) or 'none'}")
    lines.append("")

    lines.append("### Screenshot Paths")
    lines.append("")
    lines.append(f"- **Desktop:** `{row.get('Desktop Screenshot Path', '') or evidence.get('desktop_screenshot_path', '') or screenshots.get('desktop', '')}`")
    lines.append(f"- **Mobile:** `{row.get('Mobile Screenshot Path', '') or evidence.get('mobile_screenshot_path', '') or screenshots.get('mobile', '')}`")
    lines.append("")

    lines.append("### Homepage")
    lines.append("")
    lines.append(f"- **Title:** {homepage.get('title', '')}")
    lines.append(f"- **Meta:** {homepage.get('meta', '')}")
    lines.append("")
    lines.append("> " + clip(homepage.get("text", ""), 1500))
    lines.append("")

    if contact:
        lines.append("### Contact Page")
        lines.append("")
        lines.append(f"- **URL:** {contact.get('url', '')}")
        lines.append(f"- **OK:** {contact.get('ok', '')}")
        lines.append("> " + clip(contact.get("text", ""), 1000))
        lines.append("")

    if services:
        lines.append("### Services Page")
        lines.append("")
        lines.append(f"- **URL:** {services.get('url', '')}")
        lines.append(f"- **OK:** {services.get('ok', '')}")
        lines.append("> " + clip(services.get("text", ""), 1200))
        lines.append("")

    if about:
        lines.append("### About Page")
        lines.append("")
        lines.append(f"- **URL:** {about.get('url', '')}")
        lines.append(f"- **OK:** {about.get('ok', '')}")
        lines.append("> " + clip(about.get("text", ""), 900))
        lines.append("")

    if gallery:
        lines.append("### Gallery / Portfolio Page")
        lines.append("")
        lines.append(f"- **URL:** {gallery.get('url', '')}")
        lines.append(f"- **OK:** {gallery.get('ok', '')}")
        lines.append("> " + clip(gallery.get("text", ""), 700))
        lines.append("")

    return "\n".join(lines)


def suggested_opener(row: Dict[str, str]) -> str:
    if row.get("Opener"):
        return row.get("Opener", "")

    business = row.get("Business Name", "your business")
    vertical = row.get("Vertical", "service business")
    offer = row.get("Offer Fit", "")
    quality = row.get("Current Website Quality", "")
    reviews = row.get("Reviews", "")
    rating = row.get("Rating", "")

    if offer == "Website Rebuild":
        if quality == "N/A":
            return (
                f"Hey [Name] — I came across {business}. Your Google profile looks strong "
                f"({rating} stars / {reviews} reviews), but I couldn’t find a real website/estimate path. "
                f"I put together a quick homepage direction showing how your work could look online. Want me to send it over?"
            )
        return (
            f"Hey [Name] — I came across {business}. Your reviews look strong "
            f"({rating} stars / {reviews} reviews), but the website looks like it’s underselling the quality of the work. "
            f"I mocked up a cleaner version with a better estimate path. Want the link?"
        )

    if offer == "Estimate Funnel":
        return (
            f"Hey [Name] — quick note. {business} looks credible from the outside "
            f"({rating} stars / {reviews} reviews), but the site looks like it could make estimate requests easier. "
            f"I mocked up a tighter quote path for your {vertical.lower()} leads. Want me to send it?"
        )

    if offer == "Tracking / Missed Call":
        return (
            f"Hey [Name] — I noticed {business} has strong review proof, but there may be some lead-routing/phone consistency issues online. "
            f"That usually means calls/forms can get lost or misattributed. Want me to send over what I noticed?"
        )

    if offer == "Website Cleanup":
        return (
            f"Hey [Name] — {business} looks like a solid local company, but I noticed a few places where the website could be sharper for estimate requests. "
            f"I put together a quick visual note. Want me to send it?"
        )

    return (
        f"Hey [Name] — I came across {business}. You look like a credible local company, and I noticed a few online conversion gaps. "
        f"Want me to send over what I found?"
    )


def review_prompt(row: Dict[str, str]) -> str:
    draft = core_tsv_draft(row)

    return f"""## ChatGPT/Claude Review Prompt

Use the evidence above and the screenshots attached/referenced.

Task:
Produce one Airtable-ready tab-separated row.

Column order must be exactly:

Business Name, Owner Name, Vertical, City, Phone, Email, Website URL, Google Maps URL, Reviews, Rating, Current Website Quality, Best Service Angle, Specific Observation, Mockup URL, Video URL, Outreach Channel, Status, Last Contacted, Next Follow-Up

Rules:
- Output one TSV row only.
- No header.
- No commentary.
- Status must be New.
- Outreach Channel must be Google Maps.
- Leave Mockup URL, Video URL, Last Contacted, and Next Follow-Up blank unless provided.
- Do not invent owner name, email, website, phone, or city.
- Current Website Quality must be one of: N/A, Poor, Okay, Good.
- Specific Observation must be detailed, direct, evidence-based, and useful for outreach.
- Do not write generic lines like “could improve SEO.”
- Focus on whether this is a mockup-first opportunity: visual delta, weak/no site, weak estimate path, broken/non-standard URL, missing contact path, phone mismatch, or strong business proof trapped behind a weak online presence.

Existing draft row:
```tsv
{draft}
```
"""


def packet_markdown(row: Dict[str, str]) -> str:
    row = sanitize_packet_row(row)
    evidence = parse_raw_evidence(row)

    tier = row.get("Packet Tier", "").strip()

    lines: List[str] = []
    lines.append(f"# Lead Packet — {row.get('Business Name', '')}")
    lines.append("")
    if tier == "Manual Review Packet":
        lines.append("> ⚠️ **Not sendable yet — needs visual/form review before outreach.**")
        lines.append("")
    lines.append("## Quick Verdict")
    lines.append("")
    lines.append(f"- **Packet Tier:** {tier or 'No Packet'}")
    lines.append(f"- **Manual Review Reason:** {row.get('Manual Review Reason', '') or 'none'}")
    lines.append(f"- **Priority:** {row.get('Lead Priority', '')}")
    lines.append(f"- **Packet Eligible:** {row.get('Packet Eligible', '') or 'No'}")
    lines.append(f"- **Packet Eligibility Reason:** {row.get('Packet Eligibility Reason', '') or 'none'}")
    lines.append(f"- **Sendable:** {row.get('Sendable', '') or 'No'}")
    lines.append(f"- **Minimum Evidence Passed:** {row.get('Minimum Evidence Passed', '') or 'No'}")
    lines.append(f"- **Sendability Blockers:** {row.get('Sendability Blockers', '') or row.get('Send Blockers', '') or 'none'}")
    lines.append(f"- **Offer Fit:** {row.get('Offer Fit', '')}")
    lines.append(f"- **Website Quality:** {row.get('Current Website Quality', '')}")
    lines.append(f"- **Site Status:** {row.get('Site Status', '')}")
    lines.append(f"- **Review Demand Band:** {row.get('Review Demand Band', '') or row.get('Review Fit Band', '')}")
    lines.append(f"- **Score Total:** {row.get('Score Total', '')}")
    lines.append("")
    lines.append("## Current Airtable Draft")
    lines.append("")
    lines.append(field_table(row, CORE_AIRTABLE_FIELDS))
    lines.append("")
    lines.append("## Extra Scoring Fields")
    lines.append("")
    lines.append(field_table(row, EXTRA_FIELDS))
    lines.append("")
    lines.append("## Suggested Opener")
    lines.append("")
    lines.append(suggested_opener(row))
    lines.append("")
    lines.append("## Core TSV Draft")
    lines.append("")
    lines.append("```tsv")
    lines.append(core_tsv_draft(row))
    lines.append("```")
    lines.append("")
    lines.append(evidence_summary(row, evidence))
    if should_include_review_prompt(row):
        lines.append("")
        lines.append(review_prompt(row))

    return "\n".join(lines)


def write_packet(row: Dict[str, str], output_dir: Path, index: int) -> Path:
    priority = row.get("Lead Priority", "Unknown") or "Unknown"
    offer = row.get("Offer Fit", "Unknown") or "Unknown"
    business = row.get("Business Name", "lead") or "lead"
    tier = row.get("Packet Tier", "").strip()

    if tier == "Manual Review Packet":
        folder = output_dir / "Manual_Review" / f"{priority}_{safe_slug(offer)}"
    else:
        folder = output_dir / f"{priority}_{safe_slug(offer)}"
    folder.mkdir(parents=True, exist_ok=True)

    filename = f"{index:03d}-{safe_slug(business)}.md"
    path = folder / filename

    path.write_text(packet_markdown(row), encoding="utf-8")
    return path


def write_index(output_dir: Path, packet_rows: List[Dict[str, str]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    path = output_dir / "packet_index.csv"

    fields = [
        "Packet Path",
        "Place Key",
        "Business Name",
        "Lead Priority",
        "Offer Fit",
        "Current Website Quality",
        "Site Status",
        "Rating",
        "Reviews",
        "City",
        "Phone",
        "Email",
        "Website URL",
        "Google Maps URL",
        "Rendered Site Status",
        "Visual Review Needed",
        "Desktop Screenshot Path",
        "Mobile Screenshot Path",
        "Score Total",
        "Packet Tier",
        "Manual Review Reason",
        "Sendable",
        "Minimum Evidence Passed",
        "Packet Eligibility Reason",
        "Sendability Blockers",
        "Send Blockers",
        "Review Fit Band",
        "Review Demand Band",
        "Fit Flags",
        "Source File",
    ]

    _atomic_write_csv(
        path,
        fields,
        [{field: row.get(field, "") for field in fields} for row in packet_rows],
    )


def _atomic_write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            writer = csv.DictWriter(tmp, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def update_input_packet_paths(input_paths: List[Path], packet_rows: List[Dict[str, str]]) -> List[Path]:
    packet_paths_by_source: Dict[tuple[str, str], str] = {}
    for row in packet_rows:
        source_file = row.get("_source_file", "")
        source_index = row.get("_source_index", "")
        packet_path = row.get("Packet Path", "")
        if source_file and source_index:
            packet_paths_by_source[(source_file, source_index)] = packet_path

    updated: List[Path] = []
    for path in input_paths:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

        added_packet_path = False
        if "Packet Path" not in fieldnames:
            fieldnames.append("Packet Path")
            added_packet_path = True

        changed = added_packet_path
        for index, row in enumerate(rows):
            packet_path = packet_paths_by_source.get((str(path), str(index)), "")
            if row.get("Packet Path", "") != packet_path:
                row["Packet Path"] = packet_path
                changed = True

        if not changed:
            continue

        _atomic_write_csv(path, fieldnames, rows)

        updated.append(path)

    return updated


def main() -> int:
    raise_csv_limit()

    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True, help="Scored CSV files")
    parser.add_argument("--output-dir", default="packets", help="Output packet directory")
    parser.add_argument("--include-all", action="store_true", help="Create packets for all rows, not only Packet Eligible = Yes")
    parser.add_argument("--priority", default="", help="Optional priority filter, e.g. A or B")
    parser.add_argument("--max", type=int, default=0, help="Optional max packets to create")
    args = parser.parse_args()

    input_paths = [Path(p) for p in args.inputs]
    output_dir = Path(args.output_dir)

    rows = read_rows(input_paths)

    try:
        filtered = [
            row
            for row in rows
            if is_eligible(row, include_all=args.include_all, priority_filter=args.priority or None)
        ]
    except ValueError as exc:
        print(f"[REFUSE] {exc}", file=sys.stderr)
        return 2

    def sort_key(row: Dict[str, str]):
        priority_rank = {"A": 0, "B": 1, "C": 2, "Skip": 3}
        offer_rank = {
            "Website Rebuild": 0,
            "Estimate Funnel": 1,
            "Premium Perception Gap": 1,
            "Portfolio Rebuild": 1,
            "Mobile Quote Path Cleanup": 1,
            "Service Page Cleanup": 2,
            "Review/Trust Proof Upgrade": 2,
            "Website Cleanup": 2,
            "Tracking / Missed Call": 3,
            "Advanced Optimization": 4,
        }
        return (
            priority_rank.get(row.get("Lead Priority", ""), 9),
            offer_rank.get(row.get("Offer Fit", ""), 9),
            -safe_int(row.get("Score Total")),
            -safe_int(row.get("Reviews")),
        )

    filtered.sort(key=sort_key)

    if args.max > 0:
        filtered = filtered[: args.max]

    output_dir.mkdir(parents=True, exist_ok=True)

    packet_rows: List[Dict[str, str]] = []

    for i, row in enumerate(filtered, start=1):
        packet_path = write_packet(row, output_dir, i)
        row = dict(row)
        row["Packet Path"] = str(packet_path)
        row["Source File"] = row.get("_source_file", "")
        packet_rows.append(row)
        print(f"[PACKET] {packet_path}")

    updated_inputs = update_input_packet_paths(input_paths, packet_rows)
    # The index is the completion marker consumed by the dashboard, so write
    # it only after every source row points at the packet set it describes.
    write_index(output_dir, packet_rows)

    print(f"[DONE] Inputs: {', '.join(str(p) for p in input_paths)}")
    print(f"[DONE] Output dir: {output_dir}")
    print(f"[DONE] Packets created: {len(packet_rows)}")
    print(f"[DONE] Index: {output_dir / 'packet_index.csv'}")
    if updated_inputs:
        print(f"[DONE] Updated packet paths: {', '.join(str(p) for p in updated_inputs)}")

    if packet_rows:
        print("\nTop packets:")
        for row in packet_rows[:10]:
            print(
                f'{row.get("Lead Priority", ""):4} | '
                f'{row.get("Offer Fit", ""):18} | '
                f'{row.get("Business Name", "")[:40]:40} | '
                f'{row.get("Rating", "")}/{row.get("Reviews", "")} | '
                f'{row.get("Packet Path", "")}'
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
