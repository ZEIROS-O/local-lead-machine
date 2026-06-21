from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class VerticalProfile:
    key: str
    label: str
    family: str
    batch_terms: tuple[str, ...]
    service_terms: tuple[str, ...]
    high_ticket_terms: tuple[str, ...]
    deny_terms: tuple[str, ...]
    low_ticket_terms: tuple[str, ...]
    franchise_denylist: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    geo_terms: tuple[str, ...]


HARDSCAPE_BATCH_TERMS = (
    "hardscape",
    "hardscaping",
    "paver",
    "pavers",
    "patio",
    "retaining wall",
    "outdoor living",
)

HARDSCAPE_SERVICE_TERMS = (
    "hardscape",
    "hardscaping",
    "patio",
    "paver",
    "pavers",
    "retaining wall",
    "concrete",
    "stamped concrete",
    "decorative concrete",
    "masonry",
    "outdoor living",
    "outdoor kitchen",
    "fire pit",
    "landscape design",
    "landscape construction",
    "design-build",
)

HARDSCAPE_HIGH_TICKET_TERMS = (
    "hardscape",
    "hardscaping",
    "patio",
    "paver",
    "retaining wall",
    "drainage",
    "outdoor living",
    "outdoor kitchen",
    "fire pit",
    "fire feature",
    "pergola",
    "pool",
    "landscape design",
    "design build",
    "hydroseeding",
    "roof replacement",
    "roof repair",
    "storm damage",
    "siding",
    "gutter",
    "insurance claim",
    "commercial roofing",
)

HARDSCAPE_FRANCHISE_DENYLIST = (
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
)

HARDSCAPE_DENY_TERMS = (
    "med spa",
    "medical spa",
    "aesthetic",
    "botox",
    "dermatology",
    "clinic",
)

HARDSCAPE_LOW_TICKET_TERMS = (
    "lawn mowing",
    "snow removal",
    "maid",
    "cleaning",
    "pressure washing",
    "junk removal",
)

CLEVELAND_METRO_TERMS = (
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
)

DETAILING_TERMS = (
    "ceramic coating",
    "paint correction",
    "ppf",
    "paint protection film",
    "auto detailing",
    "car detailing",
    "mobile detailing",
    "ceramic pro",
    "xpel",
    "gtechniq",
    "window tint",
    "vehicle wrap",
    "paint enhancement",
)

DETAILING_DENY_TERMS = (
    "express wash",
    "car wash tunnel",
    "automatic car wash",
    "self-serve car wash",
    "oil change",
    "quick lube",
    "gas station",
    "auto parts",
    "tire shop",
    "body shop",
    "collision",
    "dealership",
)

DETAILING_FRANCHISE_DENYLIST = (
    "mister car wash",
    "take 5",
    "tommy's express",
    "zips",
    "quick quack",
    "delta sonic",
)


VERTICAL_PROFILES: Mapping[str, VerticalProfile] = {
    "hardscaping": VerticalProfile(
        key="hardscaping",
        label="Hardscaping",
        family="hardscaping",
        batch_terms=HARDSCAPE_BATCH_TERMS,
        service_terms=HARDSCAPE_SERVICE_TERMS,
        high_ticket_terms=HARDSCAPE_HIGH_TICKET_TERMS,
        deny_terms=HARDSCAPE_DENY_TERMS,
        low_ticket_terms=HARDSCAPE_LOW_TICKET_TERMS,
        franchise_denylist=HARDSCAPE_FRANCHISE_DENYLIST,
        exclude_terms=(),
        geo_terms=CLEVELAND_METRO_TERMS,
    ),
    "detailing": VerticalProfile(
        key="detailing",
        label="Car Detailing",
        family="detailing",
        batch_terms=DETAILING_TERMS,
        service_terms=DETAILING_TERMS,
        high_ticket_terms=DETAILING_TERMS,
        deny_terms=DETAILING_DENY_TERMS,
        low_ticket_terms=DETAILING_DENY_TERMS,
        franchise_denylist=DETAILING_FRANCHISE_DENYLIST,
        exclude_terms=(),
        geo_terms=CLEVELAND_METRO_TERMS,
    ),
}


def resolve_profile(vertical: str, *, source_query: str = "", fallback: str = "hardscaping") -> VerticalProfile:
    for candidate in (_normalize_key(vertical), _infer_profile_key(source_query), _normalize_key(fallback)):
        if candidate in VERTICAL_PROFILES:
            return VERTICAL_PROFILES[candidate]
    return VERTICAL_PROFILES["hardscaping"]


def _normalize_key(value: str) -> str:
    cleaned = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    if cleaned in {"car detailing", "auto detailing", "mobile detailing", "detailing", "detailer"}:
        return "detailing"
    if any(
        term in cleaned
        for term in (
            "hardscape",
            "hardscaping",
            "paver",
            "patio",
            "retaining wall",
            "outdoor living",
            "masonry",
            "landscape",
            "landscaping",
            "landscaper",
        )
    ):
        return "hardscaping"
    return cleaned


def _infer_profile_key(source_query: str) -> str:
    text = str(source_query or "").strip().lower().replace("_", " ")
    if not text:
        return ""
    if any(term in text for term in DETAILING_TERMS):
        return "detailing"
    if any(term in text for term in HARDSCAPE_BATCH_TERMS + HARDSCAPE_SERVICE_TERMS):
        return "hardscaping"
    return ""
