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
    aliases: tuple[str, ...] = ()
    min_reviews: int = 10
    max_reviews: int | None = None


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

WATER_RESTORATION_TERMS = (
    "water damage restoration",
    "water restoration",
    "flood damage restoration",
    "emergency water extraction",
    "water mitigation",
    "flood cleanup",
    "storm damage cleanup",
    "sewage cleanup",
    "mold remediation",
    "fire and water restoration",
    "disaster restoration",
)

WATER_RESTORATION_DENY_TERMS = (
    "carpet cleaning",
    "air duct cleaning",
    "dryer vent cleaning",
    "pressure washing",
    "janitorial",
    "maid service",
    "house cleaning",
    "plumber",
    "plumbing",
    "basement waterproofing",
    "waterproofing",
    "pool service",
    "bottled water",
    "water softener",
)

WATER_RESTORATION_FRANCHISE_DENYLIST = (
    "servpro",
    "servicemaster",
    "service master",
    "paul davis",
    "puroclean",
    "rainbow restoration",
    "1-800 water damage",
    "1800 water damage",
    "advantaclean",
    "belfor",
)

GARAGE_DOOR_TERMS = (
    "garage door repair",
    "garage door installation",
    "garage door service",
    "overhead door repair",
    "garage door opener repair",
    "garage door opener installation",
    "garage door spring repair",
    "broken garage door spring",
    "emergency garage door repair",
    "commercial overhead door",
    "rolling steel door",
)

GARAGE_DOOR_DENY_TERMS = (
    "garage floor epoxy",
    "garage floor coating",
    "garage cabinets",
    "garage storage",
    "garage organization",
    "garage builder",
    "pole barn",
    "shed",
    "parking garage",
    "self storage",
    "home depot",
    "lowe's",
    "lowes",
    "menards",
    "ace hardware",
)

GARAGE_DOOR_FRANCHISE_DENYLIST = (
    "precision garage door",
    "precision door",
    "a1 garage door service",
    "overhead door company",
    "wayne dalton",
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
        min_reviews=10,
        max_reviews=None,
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
        aliases=("car detailing", "auto detailing", "mobile detailing", "detailing", "detailer"),
        min_reviews=10,
        max_reviews=130,
    ),
    "water_restoration": VerticalProfile(
        key="water_restoration",
        label="Water Restoration",
        family="water_restoration",
        batch_terms=WATER_RESTORATION_TERMS,
        service_terms=WATER_RESTORATION_TERMS,
        high_ticket_terms=WATER_RESTORATION_TERMS,
        deny_terms=WATER_RESTORATION_DENY_TERMS,
        low_ticket_terms=WATER_RESTORATION_DENY_TERMS,
        franchise_denylist=WATER_RESTORATION_FRANCHISE_DENYLIST,
        exclude_terms=(),
        geo_terms=CLEVELAND_METRO_TERMS,
        aliases=(
            "water restoration",
            "water damage restoration",
            "water damage",
            "water mitigation",
            "flood restoration",
            "flood damage restoration",
            "flood cleanup",
            "restoration",
        ),
        min_reviews=10,
        max_reviews=None,
    ),
    "garage_doors": VerticalProfile(
        key="garage_doors",
        label="Garage Doors",
        family="garage_doors",
        batch_terms=GARAGE_DOOR_TERMS,
        service_terms=GARAGE_DOOR_TERMS,
        high_ticket_terms=GARAGE_DOOR_TERMS,
        deny_terms=GARAGE_DOOR_DENY_TERMS,
        low_ticket_terms=GARAGE_DOOR_DENY_TERMS,
        franchise_denylist=GARAGE_DOOR_FRANCHISE_DENYLIST,
        exclude_terms=(),
        geo_terms=CLEVELAND_METRO_TERMS,
        aliases=(
            "garage doors",
            "garage door",
            "garage door repair",
            "garage door installation",
            "garage door service",
            "overhead doors",
            "overhead door",
        ),
        min_reviews=10,
        max_reviews=None,
    ),
}


def clean_vertical_label(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


PROFILE_ALIAS_TO_KEY: Mapping[str, str] = {
    alias: profile.key
    for profile in VERTICAL_PROFILES.values()
    for alias in {clean_vertical_label(profile.key), clean_vertical_label(profile.label), *profile.aliases}
    if alias
}


def resolve_profile(vertical: str, *, source_query: str = "", fallback: str = "hardscaping") -> VerticalProfile:
    for candidate in (_normalize_key(vertical), _infer_profile_key(source_query), _normalize_key(fallback)):
        if candidate in VERTICAL_PROFILES:
            return VERTICAL_PROFILES[candidate]
    return VERTICAL_PROFILES["hardscaping"]


def _normalize_key(value: str) -> str:
    cleaned = clean_vertical_label(value)
    if not cleaned:
        return ""
    resolved = PROFILE_ALIAS_TO_KEY.get(cleaned)
    if resolved:
        return resolved
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
    if any(term in text for term in WATER_RESTORATION_TERMS):
        return "water_restoration"
    if any(term in text for term in GARAGE_DOOR_TERMS):
        return "garage_doors"
    if any(term in text for term in HARDSCAPE_BATCH_TERMS + HARDSCAPE_SERVICE_TERMS):
        return "hardscaping"
    return ""
