from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .schema import normalize_lead


def safe_slug(text: str, fallback: str = "lead") -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:90] or fallback


def build_llm_prompt(enriched: Dict[str, Any], *, fallback_vertical: str) -> str:
    google = enriched.get("google", {}) or {}
    evidence = enriched.get("evidence", {}) or {}
    lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
    screenshots = evidence.get("screenshots", {}) or {}

    packet = {
        "lead": lead.to_dict(),
        "evidence": evidence,
        "website_text": (enriched.get("website", {}) or {}).get("combined_text", "")[:18000],
    }

    return f"""# Manual LLM Review - {lead.business_name}

Use this prompt in Claude or ChatGPT on the web. Attach screenshots if available:
- Desktop: {screenshots.get("desktop", "") or "none"}
- Mobile: {screenshots.get("mobile", "") or "none"}

Return JSON only. Do not include markdown fences or commentary.

Rubric:
- ICP Fit / 20
- Reputation + Demand Proof / 15
- Web Conversion Pain / 25
- Mockup Leverage / 15
- Reachability / 10
- Offer Angle Clarity / 10
- Evidence Confidence / 5

Rules:
- Score and confidence are separate.
- Every scored claim must reference evidence IDs from the packet.
- If there is no evidence for a claim, lower evidence confidence instead of inventing.
- Missing site plus strong reviews can be an A lead.
- Hard disqualifier or score below 50 means Skip.

Allowed best_angle values:
- Website Rebuild
- Estimate Funnel
- Website Cleanup
- Tracking
- Advanced Optimization
- No-Site Launch
- Social/Jobber Escape

Output schema:
{{
  "subscores": {{
    "icp_fit": 0,
    "reputation_demand_proof": 0,
    "web_conversion_pain": 0,
    "mockup_leverage": 0,
    "reachability": 0,
    "offer_angle_clarity": 0,
    "evidence_confidence": 0
  }},
  "confidence": "low|medium|high",
  "hard_disqualifiers": [],
  "best_angle": "",
  "recommended_action": "",
  "why_this_lead": [],
  "why_this_might_be_wrong": [],
  "opener": "",
  "evidence_refs": [],
  "airtable_fields": {{}}
}}

Lead packet:
{json.dumps(packet, ensure_ascii=False, indent=2)}
"""


def write_review_queue(
    records: Iterable[Dict[str, Any]],
    output_dir: Path,
    *,
    fallback_vertical: str,
) -> List[Path]:
    pending = output_dir / "pending"
    scores = output_dir / "scores"
    pending.mkdir(parents=True, exist_ok=True)
    scores.mkdir(parents=True, exist_ok=True)

    paths: List[Path] = []
    for record in records:
        google = record.get("google", {}) or {}
        lead = normalize_lead(google, fallback_vertical=fallback_vertical, source_batch=google.get("source_query", ""))
        filename = f"{safe_slug(lead.place_id_or_unique_key or lead.business_name)}.md"
        path = pending / filename
        path.write_text(build_llm_prompt(record, fallback_vertical=fallback_vertical), encoding="utf-8")
        paths.append(path)

    return paths

