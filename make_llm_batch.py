#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from lead_machine.evidence import build_evidence_profile
from lead_machine.icp import hard_icp_gate
from lead_machine.llm_queue import safe_slug
from lead_machine.schema import normalize_lead


WEBSITE_TEXT_LIMIT = 12000

MANUAL_SCORE_SCHEMA: Dict[str, Any] = {
    "subscores": {
        "icp_fit": 0,
        "reputation_demand_proof": 0,
        "web_conversion_pain": 0,
        "mockup_leverage": 0,
        "reachability": 0,
        "offer_angle_clarity": 0,
        "evidence_confidence": 0,
    },
    "total_score": 0,
    "grade": "A|B|C|Skip",
    "confidence": "low|medium|high",
    "hard_disqualifiers": [],
    "best_angle": "",
    "visible_pain_score": 0,
    "visible_pain_reasons": [],
    "visible_pain_categories": [],
    "send_trigger": "",
    "recommended_action": "",
    "why_this_lead": [],
    "why_this_might_be_wrong": [],
    "opener": "",
    "evidence_refs": [],
    "safe_outreach_claims": [{"claim": "", "evidence_refs": []}],
    "unsafe_claims": [],
    "airtable_fields": {},
}


@dataclass(frozen=True)
class BatchWrite:
    batch_id: str
    prompt_path: Path
    expected_files_path: Path
    score_files: List[str]


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        records.append(ensure_evidence(record))
    return records


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


def score_file_for_record(record: Dict[str, Any], *, fallback_vertical: str) -> str:
    google = record.get("google", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    return f"{safe_slug(lead.place_id_or_unique_key or lead.business_name)}.json"


def lead_packet_for_record(record: Dict[str, Any], *, fallback_vertical: str) -> Dict[str, Any]:
    record = ensure_evidence(record)
    google = record.get("google", {}) or {}
    evidence = record.get("evidence", {}) or {}
    website = record.get("website", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))

    return {
        "score_file": score_file_for_record(record, fallback_vertical=fallback_vertical),
        "lead": lead.to_dict(),
        "screenshots": (evidence.get("screenshots") or website.get("screenshots") or record.get("screenshots") or {}),
        "evidence": evidence,
        "website_text": str(website.get("combined_text") or "")[:WEBSITE_TEXT_LIMIT],
    }


def candidate_records(records: Iterable[Dict[str, Any]], *, fallback_vertical: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for record in records:
        google = record.get("google", {}) or {}
        lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
        if not hard_icp_gate(lead).should_skip:
            candidates.append(record)
    return candidates


def batch_sort_key(record: Dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    record = ensure_evidence(record)
    evidence = record.get("evidence", {}) or {}
    website = record.get("website", {}) or {}
    google = record.get("google", {}) or {}

    site_status = str(evidence.get("site_status") or website.get("site_status") or "").strip().lower().replace(" ", "_")
    structural_rank = {
        "missing": 0,
        "no_website": 0,
        "broken": 0,
        "broken_website": 0,
        "social_only": 0,
        "jobber_only": 0,
        "weak": 1,
    }.get(site_status, 2)

    trigger_rank = 0 if str(evidence.get("send_trigger") or "").strip() else 1
    visible_pain_score = int(evidence.get("visible_pain_score") or 0)

    raw_categories = evidence.get("visible_pain_categories") or []
    if isinstance(raw_categories, str):
        raw_categories = raw_categories.replace(",", ";").split(";")
    if not isinstance(raw_categories, list):
        raw_categories = []
    categories = {str(category).strip() for category in raw_categories if str(category).strip()}
    flags = {
        str(flag).strip().lower()
        for flag in (website.get("site_issue_flags") or evidence.get("raw_flags") or [])
        if str(flag).strip()
    }
    pain_rank = 8
    if categories & {"no_site_or_social_only_pain", "broken_site_pain"}:
        pain_rank = 0
    elif categories & {"portfolio_presentation_pain"} or flags & {
        "weak_project_gallery",
        "weak_gallery",
        "buried_project_photos",
        "project_photos_buried",
        "portfolio_presentation_pain",
    }:
        pain_rank = 1
    elif categories & {"premium_perception_gap"} or flags & {
        "premium_perception_gap",
        "premium_brand_mismatch",
        "owned_site_undersells_google_proof",
        "cheap_website",
        "site_looks_cheap",
        "outdated_site",
        "lawn_maintenance_positioning",
    }:
        pain_rank = 2
    elif categories & {"mobile_usability_pain"} or flags & {"weak_mobile_path", "bad_mobile_status", "mobile_usability_pain"}:
        pain_rank = 3
    elif categories & {"trust_proof_pain", "service_clarity_pain"} or flags & {
        "low_visible_trust",
        "reviews_not_near_cta",
        "trust_proof_pain",
        "weak_services_page",
        "unclear_service_pages",
        "high_ticket_services_unclear",
        "service_clarity_pain",
    }:
        pain_rank = 4
    elif categories & {"estimate_path_pain"}:
        pain_rank = 5

    estimate_form = str(evidence.get("estimate_form_detected") or website.get("estimate_form_detected") or "").lower()
    form_strength = str(evidence.get("form_strength") or website.get("form_strength") or "").lower()
    strong_form_no_pain_rank = (
        1
        if estimate_form == "yes"
        and form_strength == "strong"
        and visible_pain_score <= 0
        and not categories
        else 0
    )

    try:
        reviews = int(float(google.get("reviews") or 0))
    except (TypeError, ValueError):
        reviews = 0
    if 5 <= reviews <= 20:
        review_rank = 0
    elif 21 <= reviews <= 75:
        review_rank = 1
    elif reviews < 5:
        review_rank = 2
    else:
        review_rank = 3

    return (structural_rank, trigger_rank, pain_rank, -visible_pain_score, review_rank, strong_form_no_pain_rank)


def chunk_records(records: List[Dict[str, Any]], batch_size: int) -> List[List[Dict[str, Any]]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")
    return [records[index : index + batch_size] for index in range(0, len(records), batch_size)]


def build_batch_prompt(
    packets: List[Dict[str, Any]],
    *,
    batch_id: str,
) -> str:
    response_example = {
        "batch_id": batch_id,
        "scores": [
            {
                "score_file": packets[0]["score_file"] if packets else "phone-xxxxxxxxxx.json",
                "score": MANUAL_SCORE_SCHEMA,
            }
        ],
    }

    return f"""# Manual LLM Batch Review - {batch_id}

Use this prompt in Claude or ChatGPT on the web. Attach screenshots when useful.

JSON-only response requirements:
- Response must be one valid JSON object only.
- First character must be {{ and last character must be }}.
- No markdown fences.
- No commentary.
- No reasoning outside JSON.
- No "Thought for X seconds" text.
- Use one scores item per lead packet.
- Every score_file must exactly match a score_file listed in lead_packets below.
- score_file may look like "phone-xxxxxxxxxx.json" when the lead key is phone-derived.

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
- Every scored claim must reference evidence IDs from that lead packet.
- If there is no evidence for a claim, lower evidence confidence instead of inventing.
- Missing site plus strong reviews can be an A lead.
- Do not score A/sendable just because the business is reputable.
- Do not score A/sendable just because the business fits hardscaping.
- The best lead is one where the website is visibly worse than the actual business.
- No specific observation = no send.
- More reviews require stronger proof of visible pain.
- Do not infer missing forms if evidence is uncertain.
- Static fetch failure alone does not prove the website is broken.
- If rendered_site_status is Loaded, do not claim the website is broken.
- If a rendered site loads but looks weak, classify the issue as outdated, poor, Website Cleanup, Website Rebuild, or Premium Perception Gap, not broken.
- Visual claims require screenshot paths, rendered browser evidence, DOM evidence, or explicit manual review notes.
- No screenshot/rendered evidence means no strong visual claims.
- A strong estimate form does not globally disqualify a lead.
- It only blocks missing-form, no-contact-path, and no-estimate-path claims.
- If another concrete issue exists, score based on that issue: premium perception gap, weak portfolio presentation, outdated site, bad mobile path, weak review/trust placement, or unclear service positioning.
- Estimate Funnel A/sendable requires a concrete estimate-path issue: no contact/estimate form, weak/basic form, phone/email-only path, broken/social-only site, missing or buried CTA, bad mobile status, low visible trust/proof despite demand, or a form that is buried/generic/disconnected from premium services.
- Do not use Estimate Funnel if the form is strong unless the actual estimate path is still visibly weak.
- If a strong estimate form exists, do not use missing-form, no-contact-path, or no-estimate-path pain.
- CTA terms alone are not proof of funnel pain.
- Advanced Optimization is not enough for sendability unless tied to a concrete visible issue.
- Hard disqualifier or score below 50 means Skip.
- safe_outreach_claims must be an array of objects.
- Each safe_outreach_claims object must have claim as a string and evidence_refs as an array of valid evidence IDs from that lead packet.
- Every safe_outreach_claims object must have at least one evidence ref.
- unsafe_claims must be an array of strings.
- unsafe_claims should list clean-sounding claims that should NOT be used because the packet evidence does not prove them.
- recommended_action must be operational. Bad: "A lead - pursue." Good: "Build/send a no-site launch mockup focused on an owned homepage with review proof, phone CTA, and estimate request path."
- opener must be safe to send or lightly edit.
- opener must mention at least one specific evidence-backed observation.
- Every opener must reference a real observable issue.
- opener must not make fake money claims, guarantee results, or make unsupported competitor claims.
- opener must not say "you are losing jobs" unless directly proven by packet evidence.

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

lead_packets:
{json.dumps(packets, ensure_ascii=False, indent=2)}
"""


def write_llm_batches(
    records: Iterable[Dict[str, Any]],
    output_dir: Path,
    *,
    fallback_vertical: str = "service",
    batch_size: int = 5,
    limit: int = 0,
    only_candidates: bool = False,
) -> List[BatchWrite]:
    selected = [ensure_evidence(record) for record in records]
    if only_candidates:
        selected = candidate_records(selected, fallback_vertical=fallback_vertical)
        selected.sort(key=batch_sort_key)
    if limit > 0:
        selected = selected[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)

    written: List[BatchWrite] = []
    for batch_index, chunk in enumerate(chunk_records(selected, batch_size), start=1):
        batch_id = f"batch_{batch_index:03d}"
        packets = [lead_packet_for_record(record, fallback_vertical=fallback_vertical) for record in chunk]
        score_files = [packet["score_file"] for packet in packets]
        prompt_path = output_dir / f"{batch_id}.md"
        expected_path = output_dir / f"{batch_id}_expected_files.txt"
        prompt_path.write_text(build_batch_prompt(packets, batch_id=batch_id), encoding="utf-8")
        expected_path.write_text("\n".join(score_files) + ("\n" if score_files else ""), encoding="utf-8")
        written.append(
            BatchWrite(
                batch_id=batch_id,
                prompt_path=prompt_path,
                expected_files_path=expected_path,
                score_files=score_files,
            )
        )

    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input enriched JSONL")
    parser.add_argument("--output-dir", required=True, help="Batch output directory, e.g. review_queue_<batch>/batches")
    parser.add_argument("--batch-size", type=int, default=5, help="Leads per manual LLM batch")
    parser.add_argument("--limit", type=int, default=0, help="Optional max lead count after filtering")
    parser.add_argument("--only-candidates", action="store_true", help="Only include leads that pass the hard ICP gate")
    parser.add_argument("--vertical", default="service", help="Fallback vertical")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    records = read_jsonl(input_path)
    written = write_llm_batches(
        records,
        Path(args.output_dir),
        fallback_vertical=args.vertical,
        batch_size=args.batch_size,
        limit=args.limit,
        only_candidates=args.only_candidates,
    )

    print(f"[DONE] Input: {input_path}")
    print(f"[DONE] Batch dir: {args.output_dir}")
    print(f"[DONE] Batch files written: {len(written)}")
    print(f"[DONE] Expected score files: {sum(len(batch.score_files) for batch in written)}")
    for batch in written[:10]:
        print(f"[BATCH] {batch.prompt_path}")
        print(f"[EXPECT] {batch.expected_files_path}")
        response_path = Path(args.output_dir) / f"{batch.batch_id}_response.json"
        if response_path.exists():
            print(
                f"[WARN] Existing response bundle may be stale after regenerated evidence: {response_path}. "
                f"Do not import or reuse it; copy a fresh model response from {batch.prompt_path}."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
