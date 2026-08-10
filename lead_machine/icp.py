from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .schema import CanonicalLead
from .verticals import resolve_profile

# The HARDSCAPE_*/CLEVELAND_METRO re-export aliases that used to sit here had no
# callers. They dated from when hardscaping was the only vertical and named it
# as the default for every gate; `resolve_profile` supplies the right terms per
# lead now.


@dataclass(frozen=True)
class GateResult:
    should_skip: bool
    reasons: List[str]


def hard_icp_gate(lead: CanonicalLead) -> GateResult:
    """Vertical-level disqualifiers only; geography is not one of them.

    An `enforce_target_geo` flag used to live here, checking the lead against a
    Cleveland-only term list carried on every profile. No caller ever passed
    True, and had one done so it would have rejected every lead in the other ten
    metros. Geography is decided upstream by the search configs' geo slices and
    assessed downstream by `scoring_v3`, which resolves the market properly and
    raises `outside_target_geography` itself.
    """
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

    # Matched against the category alone, not the joined `text` above: the
    # denylist entries are trade names that appear in ordinary copy, and a
    # garage-door company whose source query mentioned a roof would otherwise
    # be skipped for a word rather than for what it is.
    category = str(lead.category or "").strip().lower()
    if category and category in profile.category_denylist:
        reasons.append("category_is_a_different_trade")

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

    # There is no upper bound. `detailing` carried max_reviews=130 and raised a
    # `high_review_count` skip above it, which read a busy business as a bad fit.
    # It is the opposite: reviews are the demand signal this offer is sold on,
    # and the one booked call to date (2026-07-28) was a 222-review garage-door
    # business pitched on missed-call text-back — a lead the ceiling would have
    # thrown away. Maturity is banded, not gated: `buyer_signals`'
    # REVIEW_DEMAND_THRESHOLDS is the separate axis that decides how much visible
    # pain an established business has to show before it is worth contacting.
    return GateResult(should_skip=bool(reasons), reasons=reasons)
