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

Response requirements:
- Response must be one valid JSON object only.
- First character must be {{ and last character must be }}.
- No markdown fences.
- No commentary.
- No reasoning outside JSON.
- No "Thought for X seconds" text.
- The JSON should be saveable directly to review_queue_<batch>/scores/{safe_slug(lead.place_id_or_unique_key or lead.business_name)}.json.

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
- total_score must equal the sum of all subscores.
- grade rules: A = 80-100, B = 65-79, C = 50-64, Skip = below 50 or hard disqualifier.
- confidence must be one of low, medium, high.
- Every scored claim must reference evidence IDs from the packet.
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
- Each safe_outreach_claims object must have claim as a string and evidence_refs as an array of valid evidence IDs from the packet.
- Every safe_outreach_claims object must have at least one evidence ref.
- unsafe_claims must be an array of strings.
- unsafe_claims should list clean-sounding claims that should NOT be used because the packet evidence does not prove them.
- recommended_action must be operational. Bad: "A lead - pursue." Good: "Build/send a no-site launch mockup focused on an owned homepage with review proof, phone CTA, and estimate request path."
- opener: Must be safe to send or lightly edit.
- opener: Must mention at least one specific evidence-backed observation.
- opener: Every opener must reference a real observable issue.
- opener: Must not make fake money claims.
- opener: Must not guarantee results.
- opener: Must not make unsupported competitor claims.
- opener: Must not say "you are losing jobs" unless directly proven by packet evidence.

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
  "safe_outreach_claims": [
    {{
      "claim": "",
      "evidence_refs": []
    }}
  ],
  "unsafe_claims": [],
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
