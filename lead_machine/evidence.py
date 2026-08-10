from __future__ import annotations

import re
from typing import Any, Dict, List

from lead_machine.verticals import matches_term


TRACKING_TERMS = {
    "Google Analytics": ["google-analytics", "gtag(", "gtm.js", "googletagmanager"],
    "Meta Pixel": ["fbq(", "facebook pixel", "connect.facebook.net"],
    "CallRail": ["callrail"],
}

THIRD_PARTY_TOOLS = {
    "Jobber": ["getjobber.com", "client hub", "jobber"],
    "HubSpot": ["hubspot"],
    "Jotform": ["jotform"],
    "Typeform": ["typeform"],
    "Gravity Forms": ["gravity forms", "gravityforms", "gform_wrapper"],
    "WPForms": ["wpforms"],
    "Formstack": ["formstack"],
    "Squarespace Form": ["squarespace form", "sqs-block-form"],
    "Wix Form": ["wix form", "wixforms"],
    "Angi": ["angi.com", "homeadvisor"],
    "GHL": ["gohighlevel", "leadconnector"],
    "Housecall Pro": ["housecallpro"],
    "ServiceTitan": ["servicetitan", "service titan"],
    "Thryv": ["thryv"],
    "Urable": ["urable"],
    "Booksy": ["booksy"],
    "Square Appointments": ["square appointments", "squareup.com/appointments", "square.site/appointments"],
    "Mobile Tech RX": ["mobile tech rx", "mobiletechrx", "mobiletechrx.com"],
}

TRUST_TERMS = [
    "google guaranteed",
    "licensed",
    "insured",
    "bbb",
    "award",
    "years of experience",
    "family owned",
]

SERVICE_AREA_TERMS = [
    "service area",
    "areas we serve",
    "serving",
    "northeast ohio",
    "cleveland",
    "akron",
    "lake county",
]

FORM_ABSENCE_FLAGS = {
    "no_form_detected",
    "missing_contact_path",
    "missing_estimate_path",
}

# Flags about *our* crawl rather than about their site. `_status_from_site`
# reads any other flag as evidence the site is weak, and "weak" is worth pain
# points that move a lead toward sendable — so a marker meaning "we stopped
# looking" must live here, or it grades the business on our own budget.
# Kept as literals rather than imported from `enrich_leads`: this module is a
# leaf that `enrich_leads` itself imports, and the strings are a wire format
# read back off scored artifacts long after the enricher that wrote them.
EMPTY_BODY_FLAG = "empty_body_on_200"
NO_BODY_FORM_METHOD = "no_body"

# The crawl never reached a body at all. `EMPTY_BODY_FLAG` is only the
# 200-with-blank-body spelling; these are the ways a fetch dies before there is
# a body to be blank. Kept apart from `STATIC_FETCH_REVIEW_FLAGS` because that
# set includes `rendered_check_needed`, which means "look again", not "we never
# read the page".
HARD_FETCH_FAILURE_FLAGS = {
    "static_fetch_failed",
    "rendered_load_failed",
    "dns_domain_failure",
    "ssl_cert_hard_failure",
    "homepage_http_hard_failure",
}

STATIC_FETCH_REVIEW_FLAGS = {
    "static_fetch_failed",
    "homepage_fetch_failed",
    "rendered_check_needed",
    "manual_visual_review_needed",
    "candidate_page_budget_exhausted",
    # Same class as the budget flag: a statement about what our crawl could
    # read, not about the prospect's business. Grading a site "weak" for it
    # would recreate exactly the bug it exists to expose.
    EMPTY_BODY_FLAG,
}

HARD_SITE_FAILURE_FLAGS = {
    "dns_domain_failure",
    "homepage_http_hard_failure",
    "ssl_cert_hard_failure",
    "broken_redirect",
    "rendered_load_failed",
    "parked_dead_domain",
}

VISUAL_AUDIT_SCORE_FIELDS = [
    "visual_quality_score",
    "premium_perception_score",
    "mobile_visual_score",
    "hero_clarity_score",
    "cta_visibility_score",
    "gallery_visibility_score",
    "trust_proof_visibility_score",
]

# The visual scores reuse the ladder the crawl already publishes for
# form_strength and cta_strength. The LLM packet is one JSON blob, so a second
# private scale next to those keys would just be noise to decode.
STRENGTH_RANK = {"None": 0, "Weak": 1, "Basic": 2, "Strong": 3}
VISUAL_UNKNOWN = "Unknown"
# "None" and "Unknown" are not the same claim. "None" means the page was read
# and the thing is absent, which is a leak worth selling against. "Unknown"
# means the evidence was never collected, which is worth nothing and must never
# be dressed up as a finding.
VISUAL_SCORE_SCALE = ("None", "Weak", "Basic", "Strong")
# Overall look and premium feel are aesthetic calls. Markup can prove a page is
# dated, thin, or unresponsive; nothing short of eyes on a screenshot can prove
# it looks good, so these two are capped below Strong instead of manufacturing
# praise the pipeline cannot back.
VISUAL_AESTHETIC_CEILING = {
    "visual_quality_score": "Basic",
    "premium_perception_score": "Basic",
}
# Only these two site states are worth auditing. "social_only"/"jobber_only"
# would grade Facebook's or Jobber's design rather than the prospect's, and
# missing/broken/unknown means there was no page to read.
AUDITABLE_SITE_STATES = {"okay", "weak"}
# The crawl keeps visible text in DOM order, so the head of the homepage text
# is the closest thing to an above-the-fold read available without a screenshot.
HERO_TEXT_CHARS = 400

GALLERY_TERMS = [
    "gallery",
    "portfolio",
    "our work",
    "our projects",
    "recent projects",
    "project photos",
    "before and after",
    "before & after",
]

REVIEW_PROOF_TERMS = [
    "review",
    "testimonial",
    "5-star",
    "5 star",
    "five star",
    "what our customers say",
]

# Canonical for both the crawl's CTA strength and the visual CTA score, so the
# two cannot disagree about what counts as asking for the job.
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

PAIN_FLAG_RULES = {
    "premium_perception_gap": (22, "Premium work is undersold by the owned website", ["premium_perception_gap"]),
    "premium_brand_mismatch": (22, "Premium branding is not matched by the website", ["premium_perception_gap"]),
    "owned_site_undersells_google_proof": (
        20,
        "Owned site undersells stronger Google/social proof",
        ["premium_perception_gap"],
    ),
    "cheap_website": (18, "Website looks cheap relative to the work", ["premium_perception_gap"]),
    "site_looks_cheap": (18, "Website looks cheap relative to the work", ["premium_perception_gap"]),
    "outdated_site": (18, "Website looks outdated", ["premium_perception_gap"]),
    "lawn_maintenance_positioning": (
        18,
        "Website positioning looks like low-ticket lawn maintenance",
        ["premium_perception_gap", "service_clarity_pain"],
    ),
    "weak_project_gallery": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "weak_gallery": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "buried_project_photos": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "project_photos_buried": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "portfolio_presentation_pain": (
        18,
        "Project photos are buried or weak for high-ticket work",
        ["portfolio_presentation_pain"],
    ),
    "weak_mobile_path": (16, "Weak mobile quote path", ["mobile_usability_pain", "estimate_path_pain"]),
    "bad_mobile_status": (16, "Weak mobile quote path", ["mobile_usability_pain", "estimate_path_pain"]),
    "mobile_usability_pain": (16, "Weak mobile quote path", ["mobile_usability_pain", "estimate_path_pain"]),
    "low_visible_trust": (12, "No visible trust proof near CTA", ["trust_proof_pain"]),
    "reviews_not_near_cta": (12, "Reviews are not used near the conversion point", ["trust_proof_pain"]),
    "trust_proof_pain": (12, "No visible trust proof near CTA", ["trust_proof_pain"]),
    "weak_services_page": (12, "Unclear service presentation", ["service_clarity_pain"]),
    "unclear_service_pages": (12, "Unclear service presentation", ["service_clarity_pain"]),
    "high_ticket_services_unclear": (
        14,
        "High-ticket services are not shown clearly",
        ["service_clarity_pain"],
    ),
    "service_clarity_pain": (12, "Unclear service presentation", ["service_clarity_pain"]),
    "followup_or_missed_call_pain": (
        12,
        "Follow-up or missed-call path is visibly weak",
        ["followup_or_missed_call_pain"],
    ),
    "missed_call_path": (
        12,
        "Follow-up or missed-call path is visibly weak",
        ["followup_or_missed_call_pain"],
    ),
    "followup_gap": (
        12,
        "Follow-up or missed-call path is visibly weak",
        ["followup_or_missed_call_pain"],
    ),
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _evidence_item(
    items: List[Dict[str, Any]],
    *,
    field: str,
    value: Any,
    source_url: str = "",
    snippet: str = "",
    kind: str = "fact",
    confidence: str = "high",
) -> None:
    if value in ("", None, [], {}):
        return
    item_id = f"ev-{field}-{len(items) + 1}"
    item_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", item_id).strip("-").lower()
    items.append(
        {
            "id": item_id,
            "kind": kind,
            "source_url": source_url,
            "field": field,
            "value": value,
            "snippet": snippet,
            "confidence": confidence,
        }
    )


def _detect_from_text(text: str, mapping: Dict[str, List[str]]) -> List[str]:
    """Detect labelled terms in *text*, requiring a left word boundary.

    Bare `term in text` matched a term that is merely the TAIL of a longer
    word: `urable` is a suffix of `durable`, so "durable, high-performance
    coatings" read as the Urable booking platform on 42% of a scored corpus and
    shipped as a claim about the prospect's software stack. Same shape as the
    `roofing` ⊂ `waterproofing` bug `matches_term` was written for, so reuse it
    rather than growing a second matcher — including the left-only boundary,
    which keeps `thryv` matching `thryv's`.
    """
    lowered = text.lower()
    found = []
    for label, terms in mapping.items():
        if any(matches_term(lowered, term.lower()) for term in terms):
            found.append(label)
    return sorted(found)


def _status_from_site(site: Dict[str, Any]) -> str:
    status = _clean(site.get("site_status")).lower()
    flags = set(site.get("site_issue_flags") or [])
    normalized_flags = {str(flag).strip().lower() for flag in flags}
    rendered_status = _clean(site.get("rendered_site_status")).lower()
    final_url = _clean(site.get("final_url")).lower()
    text = _clean(site.get("combined_text") or site.get("homepage", {}).get("text"))

    if status in {"no website", "missing"}:
        return "missing"
    if rendered_status == "broken" or normalized_flags & HARD_SITE_FAILURE_FLAGS:
        return "broken"
    if rendered_status == "loaded" and status not in {"social only", "social_only", "facebook only", "jobber only", "jobber_only"}:
        review_flags = normalized_flags - STATIC_FETCH_REVIEW_FLAGS
        return "weak" if review_flags else "okay"
    if status in {"broken website", "broken"}:
        return "broken"
    if status in {"social only", "social_only", "facebook only"}:
        return "social_only"
    if status in {"jobber only", "jobber_only"}:
        return "jobber_only"
    if "facebook.com" in final_url or "instagram.com" in final_url:
        return "social_only"
    if "getjobber.com" in final_url or "jobber" in text.lower():
        return "jobber_only"
    if normalized_flags & STATIC_FETCH_REVIEW_FLAGS and not text:
        return "unknown"
    if not text and status and status not in {"unknown", "loaded", "loads", "okay", "strong"}:
        return "broken"
    if normalized_flags:
        return "weak"
    if status in {"loaded", "loads", "okay", "strong"}:
        return "okay"
    return "unknown"


def _hero_text(site: Dict[str, Any], structure: Dict[str, Any]) -> str:
    homepage = site.get("homepage", {}) or {}
    parts = [
        _clean(homepage.get("title")),
        _clean(homepage.get("meta")),
        " ".join(_clean(text) for text in structure.get("h1_texts") or []),
        _clean(homepage.get("text"))[:HERO_TEXT_CHARS],
    ]
    return " ".join(part for part in parts if part).lower()


def _mentions(text: str, terms: List[str]) -> bool:
    return any(term in text for term in terms)


def _gallery_page_images(pages: Dict[str, Any]) -> int:
    """Images on a crawled gallery/portfolio page, -1 when no such page exists."""
    best = -1
    for name, page in pages.items():
        if not isinstance(page, dict):
            continue
        haystack = f"{name} {_clean(page.get('url'))}".lower()
        if not _mentions(haystack, ["gallery", "portfolio", "project", "our-work", "/work"]):
            continue
        if page.get("ok") is False:
            continue
        best = max(best, int(page.get("image_count") or 0))
    return best


def _derive_visual_quality(structure: Dict[str, Any]) -> str:
    if not structure:
        return VISUAL_UNKNOWN
    if structure.get("legacy_markup"):
        return "Weak"
    if not structure.get("responsive_viewport"):
        # A homepage with no device-width viewport in 2026 is served to phones
        # zoomed out. That is a dated site whether or not anyone looks at it.
        return "Weak"
    if int(structure.get("image_count") or 0) < 3 or int(structure.get("heading_count") or 0) < 3:
        return "Weak"
    return "Basic"


def _derive_premium_perception(
    *,
    high_ticket: List[Any],
    generic_copy: List[Any],
    trust_terms: List[str],
    gallery_images: int,
) -> str:
    if not high_ticket and generic_copy:
        return "None"
    if generic_copy or len(high_ticket) < 2:
        return "Weak"
    # Premium services named with nothing to back them is the premium-perception
    # gap this pipeline sells against, so proof is what separates Weak from Basic.
    if trust_terms or gallery_images >= 0:
        return "Basic"
    return "Weak"


def _derive_mobile_visual(structure: Dict[str, Any], mobile_screenshot: str) -> str:
    if not structure and not mobile_screenshot:
        return VISUAL_UNKNOWN
    if structure and not structure.get("has_viewport_meta"):
        return "None"
    if structure and not structure.get("responsive_viewport"):
        return "Weak"
    if not structure:
        # A mobile render succeeded but the markup was never parsed, so all we
        # can honestly say is that the page serves a 390px viewport.
        return "Basic"
    return "Strong" if mobile_screenshot else "Basic"


def _derive_hero_clarity(hero: str, *, structure: Dict[str, Any], high_ticket: List[Any], service_area: List[str]) -> str:
    if not hero:
        return "None"
    names_service = bool(high_ticket) and _mentions(hero, [str(term).lower() for term in high_ticket])
    names_market = bool(service_area) and _mentions(hero, service_area)
    has_headline = bool(structure.get("h1_texts")) if structure else False
    if not names_service and not names_market:
        return "Weak"
    if names_service and names_market and has_headline:
        return "Strong"
    return "Basic"


def _derive_cta_visibility(
    *,
    hero: str,
    cta_terms: List[str],
    phones: List[Any],
    form_rank: int,
    has_contact_page: bool,
) -> str:
    if not cta_terms and not phones and form_rank <= 0:
        return "None"
    estimate_cta = _mentions(" ".join(cta_terms), ESTIMATE_CTA_TERMS)
    if not estimate_cta and not has_contact_page:
        return "Weak"
    if _mentions(hero, ESTIMATE_CTA_TERMS) and (phones or form_rank > 0):
        return "Strong"
    return "Basic"


def _derive_gallery_visibility(*, gallery_images: int, homepage_images: int, text: str) -> str:
    """Both counts are -1 when nothing was counted, which is not the same as zero."""
    if gallery_images >= 8 or homepage_images >= 12:
        return "Strong"
    if gallery_images >= 0:
        return "Basic"
    if _mentions(text, GALLERY_TERMS) or homepage_images >= 3:
        return "Weak"
    if homepage_images < 0:
        # No gallery page and no gallery wording, but the homepage images were
        # never counted, so "no project photos anywhere" is not provable.
        return VISUAL_UNKNOWN
    return "None"


def _derive_trust_proof_visibility(*, homepage_text: str, deep_text: str) -> str:
    homepage_trust = [term for term in TRUST_TERMS if term in homepage_text]
    deep_trust = [term for term in TRUST_TERMS if term in deep_text]
    if not homepage_trust and not deep_trust:
        return "None"
    if not homepage_trust:
        # Proof exists but a visitor has to leave the homepage to find it.
        return "Weak"
    if len(homepage_trust) >= 2 and _mentions(homepage_text, REVIEW_PROOF_TERMS):
        return "Strong"
    return "Basic"


def has_parsed_page_evidence(site: Dict[str, Any]) -> bool:
    """Did the crawl come back with any parsed page content for this site?

    Lifted out of `derive_visual_audit_scores`, where it has always decided
    whether the seven visual fields may be derived at all. Nothing else asked
    the question, which is how the form/CTA half of the profile ended up
    charging pain against leads that have no website.
    """
    homepage_candidate = site.get("homepage", {}) or {}
    pages_candidate = site.get("pages", {}) or {}
    return bool(
        site.get("page_structure")
        or _clean(site.get("combined_text"))
        or (
            isinstance(homepage_candidate, dict)
            and _clean(homepage_candidate.get("text"))
        )
        or any(
            _clean(page.get("text")) or page.get("image_count") is not None
            for page in pages_candidate.values()
            if isinstance(page, dict)
        )
    )


def has_parsed_body_evidence(site: Dict[str, Any]) -> bool:
    """`has_parsed_page_evidence`, plus: was any of it inside `<body>`?

    The generic guard reads `combined_text` and `homepage.text`, both of which
    come from `enrich_leads.extract_visible_text` — a whole-document reading
    that keeps `<title>`. A React shell therefore satisfies it with the 56
    characters of its title bar. The enricher is the only place that can tell
    the difference, and it says so with `empty_body_on_200` /
    `form_detection_method: "no_body"`.
    """
    flags = {str(flag).strip().lower() for flag in (site.get("site_issue_flags") or [])}
    if EMPTY_BODY_FLAG in flags:
        return False
    if _clean(site.get("form_detection_method")).lower() == NO_BODY_FORM_METHOD:
        return False
    return has_parsed_page_evidence(site)


def derive_visual_audit_scores(site: Dict[str, Any]) -> Dict[str, str]:
    """Score the seven visual-audit fields from evidence the crawl already holds.

    These shipped as a hardcoded "Unknown" on every lead, so the review packet
    carried seven dead keys straight into the prompt. Nothing here opens a
    screenshot: the grounding is the markup and copy the enricher already
    fetched, which the prompt already accepts as DOM evidence for visual
    claims. Any field without that grounding stays Unknown, because a derived
    score reads to the model as a fact and an Unknown does not.
    """
    scores = {field: VISUAL_UNKNOWN for field in VISUAL_AUDIT_SCORE_FIELDS}

    # `has_parsed_body_evidence`, not `has_parsed_page_evidence`: the weaker
    # guard reads `combined_text` / `homepage.text`, both produced by
    # `extract_visible_text`, which keeps `<title>`. A React shell satisfies it
    # with its 56-character title bar and then gets graded "cta_visibility:
    # None", "trust_proof_visibility: None" — and per this module's own
    # contract above, "None" means we read the page and the thing was absent.
    # Those land in the LLM prompt as evidence refs and in the packet as lines
    # a rep reads out, so they are the same fabrication as `no_form_detected`,
    # in a different vocabulary that the form-absence blocker cannot see.
    if _status_from_site(site) in AUDITABLE_SITE_STATES and has_parsed_body_evidence(site):
        homepage = site.get("homepage", {}) or {}
        pages = site.get("pages", {}) or {}
        structure = site.get("page_structure") or {}
        homepage_text = _clean(homepage.get("text")).lower()
        deep_text = " ".join(
            _clean(page.get("text")) for page in pages.values() if isinstance(page, dict)
        ).lower()
        combined_text = _clean(site.get("combined_text")).lower() or f"{homepage_text} {deep_text}"
        hero = _hero_text(site, structure)

        high_ticket = list(site.get("high_ticket_terms") or [])
        generic_copy = list(site.get("generic_copy_terms") or [])
        cta_terms = [_clean(term).lower() for term in site.get("cta_terms") or []]
        phones = list(site.get("phones_found") or [])
        form_rank = STRENGTH_RANK.get(_clean(site.get("form_strength")), 0)
        gallery_images = _gallery_page_images(pages)
        homepage_images = int(structure.get("image_count") or 0) if structure else -1
        trust_terms = [term for term in TRUST_TERMS if term in combined_text]
        service_area = [term for term in SERVICE_AREA_TERMS if term in combined_text]
        has_contact_page = any(
            str(name).startswith(("contact", "estimate")) for name in pages
        )

        scores["visual_quality_score"] = _derive_visual_quality(structure)
        scores["premium_perception_score"] = _derive_premium_perception(
            high_ticket=high_ticket,
            generic_copy=generic_copy,
            trust_terms=trust_terms,
            gallery_images=gallery_images,
        )
        scores["mobile_visual_score"] = _derive_mobile_visual(
            structure, _clean(site.get("mobile_screenshot_path"))
        )
        scores["hero_clarity_score"] = _derive_hero_clarity(
            hero, structure=structure, high_ticket=high_ticket, service_area=service_area
        )
        scores["cta_visibility_score"] = _derive_cta_visibility(
            hero=hero,
            cta_terms=cta_terms,
            phones=phones,
            form_rank=form_rank,
            has_contact_page=has_contact_page,
        )
        scores["gallery_visibility_score"] = _derive_gallery_visibility(
            gallery_images=gallery_images,
            homepage_images=homepage_images,
            text=combined_text,
        )
        scores["trust_proof_visibility_score"] = _derive_trust_proof_visibility(
            homepage_text=homepage_text,
            deep_text=deep_text,
        )

        for field, ceiling in VISUAL_AESTHETIC_CEILING.items():
            if STRENGTH_RANK.get(scores[field], 0) > STRENGTH_RANK[ceiling]:
                scores[field] = ceiling

    # A value already on the record outranks anything derived here: it came
    # from a human review or a future vision pass, both of which saw the page.
    for field in VISUAL_AUDIT_SCORE_FIELDS:
        existing = _clean(site.get(field))
        if existing and existing != VISUAL_UNKNOWN:
            scores[field] = existing

    return scores


def apply_visual_audit_scores(site: Dict[str, Any]) -> Dict[str, Any]:
    """Write the derived visual scores onto a crawl result."""
    updated = dict(site)
    updated.update(derive_visual_audit_scores(site))
    return updated


def _derive_visible_pain_profile(
    *,
    site: Dict[str, Any],
    site_status: str,
    contact_form_detected: str,
    estimate_form_detected: str,
    form_strength: str,
    cta_strength: str,
    phones: List[Any],
    tools: List[str],
    phone_consistency: str = "unknown",
) -> Dict[str, Any]:
    flags = {str(flag).strip().lower() for flag in (site.get("site_issue_flags") or [])}
    parsed_body = has_parsed_body_evidence(site)
    score = 0
    reasons: List[str] = []
    categories: List[str] = []
    # Points are tracked per reason so the send trigger can be the strongest
    # leak rather than whichever rule happened to run first. The reason list
    # keeps insertion order for display; only the trigger is re-ranked.
    reason_points: Dict[str, int] = {}

    def add(points: int, reason: str, pain_categories: List[str] | None = None) -> None:
        nonlocal score
        if reason in reasons:
            for category in pain_categories or []:
                if category not in categories:
                    categories.append(category)
            return
        score += points
        reasons.append(reason)
        reason_points[reason] = points
        for category in pain_categories or []:
            if category not in categories:
                categories.append(category)

    normalized_status = _clean(site_status).lower()
    strong_estimate_form = (
        _clean(estimate_form_detected).lower() == "yes" and _clean(form_strength).lower() == "strong"
    )

    if normalized_status == "missing":
        add(40, "No owned website found", ["no_site_or_social_only_pain"])
    elif normalized_status == "broken":
        add(40, "Website appears broken", ["broken_site_pain"])
    elif normalized_status == "social_only":
        add(35, "Social-only web presence", ["no_site_or_social_only_pain"])
    elif normalized_status == "jobber_only":
        add(32, "Jobber-only web presence", ["no_site_or_social_only_pain"])

    if flags & HARD_SITE_FAILURE_FLAGS and normalized_status != "broken":
        add(40, "Website appears broken", ["broken_site_pain"])
    if "non_standard_site_or_booking_url" in flags or "non_standard_site_or_booking" in flags:
        add(28, "Non-standard booking or social URL", ["no_site_or_social_only_pain"])
    if "broken_form" in flags or "form_error" in flags:
        add(30, "Estimate form appears broken", ["estimate_path_pain"])

    if not strong_estimate_form:
        if _clean(estimate_form_detected).lower() == "no" or "missing_estimate_path" in flags:
            add(20, "No estimate form detected", ["estimate_path_pain"])
        if _clean(contact_form_detected).lower() == "no" or "missing_contact_path" in flags:
            add(14, "No contact form detected", ["estimate_path_pain"])
        normalized_form_strength = _clean(form_strength).lower()
        if normalized_form_strength == "weak":
            add(18, "Weak estimate form", ["estimate_path_pain"])
        elif normalized_form_strength == "basic":
            add(12, "Basic estimate form only", ["estimate_path_pain"])
        # `cta_strength` defaults to "None", so this clause fires on its own
        # for any lead the crawl never read — a no-website lead included, where
        # there is no CTA to be unclear about. The visual half of this module
        # has always refused to derive a score without parsed page evidence;
        # so does this now.
        if parsed_body and (
            _clean(cta_strength).lower() in {"none", "weak"} or "no_clear_cta_detected" in flags
        ):
            add(14, "No clear estimate CTA", ["estimate_path_pain"])
        if "buried_cta" in flags:
            add(12, "Estimate CTA appears buried", ["estimate_path_pain"])
        if "only_phone_email_path" in flags:
            add(14, "Only phone/email estimate path", ["estimate_path_pain"])

    # `phones_found` is website-scraped phones only — the Google Business
    # Profile phone is never consulted — so an empty list on a lead with no
    # website (or no readable one) says nothing about whether the number is
    # hard to find. It is usually the first line of their Google listing. The
    # two explicit flags are somebody's positive finding and still count.
    if (not phones and parsed_body) or "phone_not_visible" in flags or "buried_phone" in flags:
        add(8, "Phone number hard to find", ["estimate_path_pain"])

    for flag, (points, reason, pain_categories) in PAIN_FLAG_RULES.items():
        if flag in flags:
            add(points, reason, pain_categories)

    if "generic_copy_signals" in flags:
        if site.get("high_ticket_terms"):
            add(
                14,
                "Premium work is undersold by generic/template website copy",
                ["premium_perception_gap"],
            )
        else:
            add(7, "Generic/template website copy")

    if tools and normalized_status in {"jobber_only", "social_only"}:
        add(6, "Owned website path is not primary", ["no_site_or_social_only_pain"])

    if _clean(phone_consistency).lower() == "different":
        add(12, "Phone/routing signal differs across Google and site", ["followup_or_missed_call_pain"])

    # The send trigger is the line the packet leads with and the one
    # `sendability` requires, so it should be the worst leak found, not the
    # first rule to fire. Ties keep the earlier reason, which preserves the
    # existing ordering for the status-derived triggers that all score 40.
    send_trigger = max(reasons, key=lambda reason: reason_points[reason]) if reasons else ""

    return {
        "visible_pain_score": min(score, 100),
        "visible_pain_reasons": reasons,
        "visible_pain_categories": categories,
        "send_trigger": send_trigger,
    }


def build_evidence_profile(enriched: Dict[str, Any]) -> Dict[str, Any]:
    site = enriched.get("website", {}) or {}
    google = enriched.get("google", {}) or {}
    screenshots = enriched.get("screenshots", {}) or {}
    derived = enriched.get("derived", {}) or {}
    desktop_screenshot_path = _clean(screenshots.get("desktop") or site.get("desktop_screenshot_path"))
    mobile_screenshot_path = _clean(screenshots.get("mobile") or site.get("mobile_screenshot_path"))
    rendered_site_status = _clean(site.get("rendered_site_status")) or "Unknown"
    rendered_status_reason = _clean(site.get("rendered_status_reason"))
    visual_review_needed = _clean(site.get("visual_review_needed")) or _clean(derived.get("visual_review_needed")) or "No"
    visual_pain_reasons = site.get("visual_pain_reasons") or []
    if isinstance(visual_pain_reasons, str):
        visual_pain_reasons = [part.strip() for part in re.split(r"[;\n]+", visual_pain_reasons) if part.strip()]
    if not isinstance(visual_pain_reasons, list):
        visual_pain_reasons = []
    # Derived here as well as in the enricher so already-enriched JSONL, which
    # carries the old all-Unknown block, still reaches the prompt scored.
    visual_scores = derive_visual_audit_scores(site)

    homepage = site.get("homepage", {}) or {}
    pages = site.get("pages", {}) or {}
    combined_text = " ".join(
        [
            _clean(homepage.get("title")),
            _clean(homepage.get("meta")),
            _clean(site.get("combined_text")),
            " ".join(_clean(page.get("text")) for page in pages.values() if isinstance(page, dict)),
        ]
    )
    lowered = combined_text.lower()
    final_url = _clean(site.get("final_url") or homepage.get("url") or google.get("website_url"))
    items: List[Dict[str, Any]] = []

    site_status = _status_from_site(site)
    tools = _detect_from_text(" ".join([combined_text, final_url]), THIRD_PARTY_TOOLS)
    tracking = _detect_from_text(combined_text, TRACKING_TERMS)
    trust_signals = sorted({term for term in TRUST_TERMS if term in lowered})
    service_area = sorted({term for term in SERVICE_AREA_TERMS if term in lowered})
    services = sorted(set(site.get("high_ticket_terms") or []))

    ctas = site.get("cta_terms") or []
    # The default matters: the enrichment-exception path (`enrich_leads`
    # builds a record whose `website` dict has no form keys at all) landed here
    # as a hard "No", which becomes +14 pain and the *send trigger* — the line
    # the packet leads with — asserting a missing contact form on a website the
    # crawl never opened. `estimate_form_detected` below has always defaulted
    # to "Unknown" for exactly this reason; the two were asymmetric.
    contact_form_detected = _clean(site.get("contact_form_detected")) or (
        "Yes" if site.get("forms_found") else ("No" if has_parsed_body_evidence(site) else "Unknown")
    )
    estimate_form_detected = _clean(site.get("estimate_form_detected")) or "Unknown"
    form_strength = _clean(site.get("form_strength")) or "None"
    form_detection_method = _clean(site.get("form_detection_method")) or "none"
    cta_strength = _clean(site.get("cta_strength")) or ("Basic" if ctas else "None")
    emails = site.get("emails_found") or []
    phones = site.get("phones_found") or []
    social_links = site.get("social_links") or []
    visible_pain = _derive_visible_pain_profile(
        site=site,
        site_status=site_status,
        contact_form_detected=contact_form_detected,
        estimate_form_detected=estimate_form_detected,
        form_strength=form_strength,
        cta_strength=cta_strength,
        phones=phones,
        tools=tools,
        phone_consistency=derived.get("phone_consistency", "unknown"),
    )
    verification_snapshot = dict(site.get("verification_snapshot") or {})
    verification_snapshots = [dict(value) for value in site.get("verification_snapshots") or [] if isinstance(value, dict)]
    if verification_snapshot:
        rendered_key = rendered_site_status.strip().lower().replace(" ", "_")
        if rendered_key == "loaded":
            verification_snapshot["render_outcome"] = "loaded"
        elif rendered_key in {"timeout", "unknown", "broken"}:
            verification_snapshot["render_outcome"] = {
                "timeout": "connection_error",
                "unknown": "blocked",
                "broken": "connection_error",
            }[rendered_key]
        if verification_snapshots:
            verification_snapshots[0] = dict(verification_snapshot)

    _evidence_item(
        items,
        field="google_rating",
        value=f"{google.get('rating', '')} stars / {google.get('reviews', '')} reviews".strip(),
        source_url=google.get("google_maps_url", ""),
    )
    _evidence_item(items, field="site_status", value=site_status, source_url=final_url)
    _evidence_item(
        items,
        field="site_issue_flags",
        value=", ".join(site.get("site_issue_flags") or []) or "none",
        source_url=final_url,
    )
    _evidence_item(items, field="third_party_tools", value=", ".join(tools), source_url=final_url)
    _evidence_item(items, field="contact_form", value=bool(site.get("forms_found")), source_url=final_url)
    _evidence_item(items, field="cta_terms", value=", ".join(ctas), source_url=final_url)
    _evidence_item(items, field="email_visible", value=", ".join(emails), source_url=final_url)
    _evidence_item(items, field="phone_visible", value=", ".join(phones), source_url=final_url)
    _evidence_item(items, field="services_detected", value=", ".join(services), source_url=final_url)
    _evidence_item(items, field="service_area_detected", value=", ".join(service_area), source_url=final_url)
    _evidence_item(items, field="trust_signals", value=", ".join(trust_signals), source_url=final_url)
    _evidence_item(items, field="tracking_detected", value=", ".join(tracking), source_url=final_url)
    _evidence_item(items, field="phone_consistency", value=derived.get("phone_consistency", "unknown"), source_url=final_url)
    _evidence_item(items, field="contact_form_detected", value=contact_form_detected, source_url=final_url)
    _evidence_item(items, field="estimate_form_detected", value=estimate_form_detected, source_url=final_url)
    _evidence_item(items, field="form_strength", value=form_strength, source_url=final_url)
    _evidence_item(items, field="form_detection_method", value=form_detection_method, source_url=final_url)
    _evidence_item(items, field="cta_strength", value=cta_strength, source_url=final_url)
    _evidence_item(items, field="rendered_site_status", value=rendered_site_status, source_url=final_url)
    _evidence_item(items, field="rendered_status_reason", value=rendered_status_reason, source_url=final_url)
    _evidence_item(items, field="desktop_screenshot_path", value=desktop_screenshot_path, source_url=final_url, kind="screenshot", confidence="medium")
    _evidence_item(items, field="mobile_screenshot_path", value=mobile_screenshot_path, source_url=final_url, kind="screenshot", confidence="medium")
    _evidence_item(items, field="visual_review_needed", value=visual_review_needed, source_url=final_url, confidence="medium")
    _evidence_item(items, field="visual_pain_reasons", value=", ".join(str(reason) for reason in visual_pain_reasons if str(reason).strip()), source_url=final_url, confidence="medium")
    _evidence_item(items, field="visible_pain_score", value=visible_pain["visible_pain_score"], source_url=final_url)
    _evidence_item(
        items,
        field="visible_pain_reasons",
        value=", ".join(visible_pain["visible_pain_reasons"]),
        source_url=final_url,
    )
    _evidence_item(
        items,
        field="visible_pain_categories",
        value=", ".join(visible_pain["visible_pain_categories"]),
        source_url=final_url,
    )
    _evidence_item(items, field="send_trigger", value=visible_pain["send_trigger"], source_url=final_url)
    if desktop_screenshot_path or mobile_screenshot_path:
        _evidence_item(
            items,
            field="screenshots",
            value={"desktop": desktop_screenshot_path, "mobile": mobile_screenshot_path},
            kind="screenshot",
            confidence="medium",
        )

    return {
        "site_status": site_status,
        "mobile_status": "unknown",
        # The boolean twins of the graded fields above. They must not read
        # `False` where the graded field reads "Unknown": `sendability`'s
        # concrete-issue gate keys on `is False`, so a hard False on a page we
        # never read answers "yes, we found a real leak" off an empty crawl.
        # `None` is the honest third state and the gate ignores it.
        "has_estimate_cta": (
            any("estimate" in str(cta).lower() or "quote" in str(cta).lower() for cta in ctas)
            or (None if not has_parsed_body_evidence(site) else False)
        ),
        "has_contact_form": (
            contact_form_detected == "Yes"
            or bool(site.get("forms_found"))
            or (None if contact_form_detected == "Unknown" else False)
        ),
        "contact_form_detected": contact_form_detected,
        "estimate_form_detected": estimate_form_detected,
        "form_strength": form_strength,
        "form_detection_method": form_detection_method,
        "cta_strength": cta_strength,
        "rendered_site_status": rendered_site_status,
        "rendered_status_reason": rendered_status_reason,
        "desktop_screenshot_path": desktop_screenshot_path,
        "mobile_screenshot_path": mobile_screenshot_path,
        "visual_review_needed": visual_review_needed,
        **visual_scores,
        "visual_pain_reasons": [str(reason).strip() for reason in visual_pain_reasons if str(reason).strip()],
        "visible_pain_score": visible_pain["visible_pain_score"],
        "visible_pain_reasons": visible_pain["visible_pain_reasons"],
        "visible_pain_categories": visible_pain["visible_pain_categories"],
        "send_trigger": visible_pain["send_trigger"],
        "has_online_booking": any(tool in tools for tool in ["Jobber", "Housecall Pro", "GHL"]),
        # `phones` is website-scraped only, so an empty list on a lead whose
        # body we never read says nothing — the number is usually the first
        # line of their Google listing. Same three-state rule as above.
        "phone_visible": bool(phones) or (None if not has_parsed_body_evidence(site) else False),
        "email_visible": bool(emails),
        "services_detected": services,
        "service_area_detected": service_area,
        "trust_signals": trust_signals,
        "social_links": social_links,
        "third_party_tools": tools,
        "tracking_detected": tracking,
        "screenshots": {"desktop": desktop_screenshot_path, "mobile": mobile_screenshot_path},
        "raw_flags": site.get("site_issue_flags") or [],
        "phone_consistency": derived.get("phone_consistency", "unknown"),
        "evidence_items": items,
        "verification_snapshot": verification_snapshot,
        "verification_snapshots": verification_snapshots or ([verification_snapshot] if verification_snapshot else []),
    }
