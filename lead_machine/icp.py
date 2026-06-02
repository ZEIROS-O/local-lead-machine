from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .schema import CanonicalLead


FRANCHISE_DENYLIST = [
    "storm guard",
    "mr. roof",
    "mr roof",
    "erie home",
    "erie construction",
    "power home",
    "leaffilter",
    "leaf filter",
    "renewal by andersen",
    "west shore",
    "long roofing",
    "groundworks",
    "window world",
]

MEDICAL_AESTHETIC_TERMS = [
    "med spa",
    "medical spa",
    "aesthetic",
    "botox",
    "dermatology",
    "clinic",
]

LOW_TICKET_TERMS = [
    "lawn mowing",
    "snow removal",
    "maid",
    "cleaning",
    "pressure washing",
    "junk removal",
]

TARGET_GEO_TERMS = [
    "oh",
    "ohio",
    "cleveland",
    "akron",
    "chagrin",
    "kirtland",
    "solon",
    "mentor",
    "willoughby",
    "lakewood",
    "middlefield",
    "east cleveland",
    "brecksville",
]


@dataclass(frozen=True)
class GateResult:
    should_skip: bool
    reasons: List[str]


def hard_icp_gate(lead: CanonicalLead, *, enforce_target_geo: bool = False) -> GateResult:
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

    if any(term in text for term in FRANCHISE_DENYLIST):
        reasons.append("franchise_or_chain")

    if any(term in text for term in MEDICAL_AESTHETIC_TERMS):
        reasons.append("medical_aesthetic_v1_excluded")

    if any(term in text for term in LOW_TICKET_TERMS):
        reasons.append("low_ticket_economics")

    if lead.google_rating is not None and lead.google_rating < 4.3:
        reasons.append("weak_reputation")

    if enforce_target_geo and not any(term in text for term in TARGET_GEO_TERMS):
        reasons.append("outside_target_geo")

    return GateResult(should_skip=bool(reasons), reasons=reasons)

