from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .schema import CanonicalLead
from .verticals import (
    HARDSCAPE_DENY_TERMS,
    HARDSCAPE_FRANCHISE_DENYLIST,
    HARDSCAPE_LOW_TICKET_TERMS,
    CLEVELAND_METRO_TERMS,
    resolve_profile,
)


FRANCHISE_DENYLIST = HARDSCAPE_FRANCHISE_DENYLIST
MEDICAL_AESTHETIC_TERMS = HARDSCAPE_DENY_TERMS
LOW_TICKET_TERMS = HARDSCAPE_LOW_TICKET_TERMS
TARGET_GEO_TERMS = CLEVELAND_METRO_TERMS


@dataclass(frozen=True)
class GateResult:
    should_skip: bool
    reasons: List[str]


def hard_icp_gate(lead: CanonicalLead, *, enforce_target_geo: bool = False) -> GateResult:
    profile = resolve_profile(lead.vertical, source_query=lead.source_query)
    text = " ".join(
        [
            lead.business_name,
            lead.vertical,
            lead.category,
            lead.city,
            lead.address,
            lead.source_query,
        ]
    ).lower()
    reasons: List[str] = []

    if any(term in text for term in profile.franchise_denylist):
        reasons.append("franchise_or_chain")

    if any(term in text for term in profile.deny_terms):
        if profile.key == "hardscaping":
            reasons.append("medical_aesthetic_v1_excluded")
        else:
            reasons.append("low_ticket_economics")

    if any(term in text for term in profile.low_ticket_terms) and "low_ticket_economics" not in reasons:
        reasons.append("low_ticket_economics")

    if lead.google_rating is not None and lead.google_rating < 4.3:
        reasons.append("weak_reputation")

    if (
        lead.website
        and lead.google_review_count is not None
        and lead.google_review_count < profile.min_reviews
    ):
        reasons.append("too_few_reviews")

    if (
        lead.website
        and profile.max_reviews is not None
        and lead.google_review_count is not None
        and lead.google_review_count > profile.max_reviews
    ):
        reasons.append("high_review_count")

    if enforce_target_geo and not any(term in text for term in profile.geo_terms):
        reasons.append("outside_target_geo")

    return GateResult(should_skip=bool(reasons), reasons=reasons)
