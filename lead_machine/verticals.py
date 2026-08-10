from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping


@dataclass(frozen=True)
class AdjacencyExclusion:
    """A neighbouring trade that looks like this vertical but does not buy like it.

    Scoped per profile because the terms are only exclusions in context: a
    plumber or a water-restoration firm advertising "property maintenance" is
    describing its actual customer, while a hardscaper doing so is a commercial
    maintenance contractor and a different sale.
    """

    reason: str
    terms: tuple[str, ...]


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
    aliases: tuple[str, ...] = ()
    # Google Places categories that can never be this vertical, however the
    # lead was found. Deliberately tiny: the Places taxonomy is noisy enough
    # that a broad category gate causes false skips — `supplier` is 166 of 239
    # garage-door leads and `general_contractor` is 286 of 386 epoxy ones — so
    # only a category naming a different trade outright belongs here.
    category_denylist: tuple[str, ...] = ()
    # One floor for every vertical, and it must equal the matching
    # `vertical_profiles/<key>.json` min_reviews: that file is the search-time
    # floor, this is the score-time floor, and two numbers means a lead can be
    # paid for and then skipped. There is deliberately no ceiling — see
    # `icp.hard_icp_gate`.
    min_reviews: int = 8
    adjacency_exclusions: tuple[AdjacencyExclusion, ...] = ()


# The green-industry neighbours of hardscaping. These were applied to every
# vertical by `make_search_batch.prefilter_row`, so a plumbing or water
# restoration lead could be dropped for saying "property maintenance" or
# "facility maintenance" — a phrase in its own market's vocabulary.
GREEN_INDUSTRY_ADJACENCY_EXCLUSIONS = (
    AdjacencyExclusion(
        "nursery_or_garden_center",
        ("nursery", "garden center", "landscape supply", "mulch supply", "stone yard"),
    ),
    AdjacencyExclusion(
        "tree_service_only",
        ("tree service", "tree removal", "arborist", "stump grinding"),
    ),
    AdjacencyExclusion(
        "snow_only",
        ("snow removal only", "snow-only", "snow plowing only", "snow removal"),
    ),
    AdjacencyExclusion(
        "lawn_fertilization_only",
        ("lawn fertilization", "fertilization", "weed control", "turf care"),
    ),
    AdjacencyExclusion(
        "commercial_maintenance_only",
        (
            "commercial maintenance only",
            "commercial maintenance",
            "facility maintenance",
            "property maintenance",
        ),
    ),
)


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
    "garage door replacement",
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
    # National exterior-remodel franchises the "commercial overhead door"
    # queries pull in. Window World of Akron scored A and cleared for contact
    # while the structurally identical Pella showroom was correctly rejected —
    # the gate caught the brands it had been told about and nothing else.
    "window world",
    "pella",
    "renewal by andersen",
    "champion windows",
)

# `commercial overhead door <metro>` seeded six non-garage-door leads into the
# vertical. Only categories that are a different trade outright — a roofer is
# not a garage-door company under any labelling — and never `supplier` or
# `general_contractor`, which are how most real garage-door leads are tagged.
GARAGE_DOOR_CATEGORY_DENYLIST = (
    "roofing_contractor",
    "locksmith",
)

PLUMBING_BATCH_TERMS = (
    "plumber",
    "plumbing",
    "plumbing contractor",
    "emergency plumber",
    "sewer line repair",
    "sewer line replacement",
    "trenchless sewer repair",
    "whole house repiping",
    "water heater installation",
    "tankless water heater installation",
    "sump pump installation",
)

PLUMBING_SERVICE_TERMS = (
    *PLUMBING_BATCH_TERMS,
    "residential plumbing",
    "commercial plumbing",
    "drain cleaning",
    "leak detection",
    "gas line repair",
    "backflow prevention",
)

PLUMBING_HIGH_TICKET_TERMS = (
    "sewer line repair",
    "sewer line replacement",
    "trenchless sewer repair",
    "whole house repiping",
    "repiping",
    "water heater installation",
    "tankless water heater",
    "gas line repair",
    "commercial plumbing",
)

PLUMBING_DENY_TERMS = (
    "plumbing supply",
    "plumbing wholesale",
    "hardware store",
    "bathroom showroom",
    "kitchen showroom",
    "appliance repair",
    "portable toilet",
    "septic pumping",
    "home depot",
    "lowe's",
    "lowes",
    "menards",
)

PLUMBING_FRANCHISE_DENYLIST = (
    "roto-rooter",
    "roto rooter",
    "mr. rooter",
    "mr rooter",
    "benjamin franklin plumbing",
    "ars rescue rooter",
    "rooter-man",
    "rooter man",
    "zoom drain",
)

# Every term here names a coating *job*, never the bare material. "epoxy" and
# "concrete" on their own belong to resin hobbyists and to hardscapers
# respectively, and `_infer_profile_key`/`infer_vertical` match by substring —
# so a one-word term would pull both into this vertical.
EPOXY_BATCH_TERMS = (
    "epoxy flooring",
    "epoxy floor",
    "garage floor epoxy",
    "garage floor coating",
    "concrete floor coating",
    "concrete coating",
    "polyaspartic floor coating",
    "polyurea floor coating",
    "metallic epoxy",
    "industrial floor coating",
)

EPOXY_SERVICE_TERMS = (
    *EPOXY_BATCH_TERMS,
    "epoxy floor contractor",
    "concrete coating contractor",
    "commercial epoxy flooring",
    "shop floor coating",
    "warehouse floor coating",
    "basement floor coating",
    "floor resurfacing",
    "concrete polishing",
    "flake floor",
)

# Mirrors `vertical_profiles/epoxy.json`'s boost list, which the live epoxy
# batches were built from — generate_search_config now sources boost terms from
# here, so drifting from it would silently re-cut the queries that are already
# running.
EPOXY_HIGH_TICKET_TERMS = (
    "epoxy flooring",
    "garage floor epoxy",
    "garage floor coating",
    "concrete floor coating",
    "polyaspartic floor coating",
    "polyurea floor coating",
    "epoxy floor contractor",
    "concrete coating contractor",
    "commercial epoxy flooring",
    "industrial floor coating",
    "decorative concrete coatings",
    "metallic epoxy flooring",
)

# Resin retail, countertop/craft work and the big boxes. They rank for the same
# queries as a floor-coating contractor and buy nothing.
EPOXY_DENY_TERMS = (
    "resin art",
    "epoxy supplies",
    "epoxy supply",
    "epoxy resin store",
    "countertop epoxy",
    "epoxy countertops",
    "diy kit",
    "powder coating",
    "auto ceramic coating",
    "flooring store",
    "home depot",
    "lowe's",
    "lowes",
    "menards",
)

EPOXY_LOW_TICKET_TERMS = (
    "floor stripping",
    "floor waxing",
    "carpet cleaning",
    "janitorial",
    "pressure washing",
    "junk removal",
    "handyman",
)

EPOXY_FRANCHISE_DENYLIST = (
    "garageexperts",
    "garage experts",
    "garage force",
    "premiergarage",
    "premier garage",
    "tailored living",
    "granite garage floors",
    "concrete craft",
    "footprints floors",
)

ROOFING_BATCH_TERMS = (
    "roofing",
    "roofing contractor",
    "roof replacement",
    "roof repair",
    "metal roofing",
    "storm damage roof",
    "commercial roofing",
    "shingle roof",
    "flat roof",
)

ROOFING_SERVICE_TERMS = (
    *ROOFING_BATCH_TERMS,
    "roof inspection",
    "siding",
    "gutters",
    "gutter installation",
    "soffit",
    "fascia",
    "skylight",
    "storm damage restoration",
    "insurance claim",
    "exterior remodeling",
)

# Mirrors `vertical_profiles/roofing.json`'s boost list, and matches the roofing
# terms that were living inside HARDSCAPE_HIGH_TICKET_TERMS back when roofing
# resolved to the hardscaping profile.
ROOFING_HIGH_TICKET_TERMS = (
    "roofing",
    "roof replacement",
    "roof repair",
    "metal roofing",
    "storm damage",
    "insurance claim",
    "commercial roofing",
    "siding",
    "gutter",
)

# Supply houses and lumber yards, not contractors. Each entry qualifies
# "roofing" with a retail word, so `hard_icp_gate` — which matches this list
# against text that already contains the lead's own vertical name — does not
# skip every roofer for saying "roofing".
ROOFING_DENY_TERMS = (
    "roofing supplies",
    "roofing supply",
    "roofing wholesale",
    "diy roofing",
    "home depot",
    "lowe's",
    "lowes",
    "menards",
    "84 lumber",
    "abc supply",
    "beacon building products",
)

ROOFING_LOW_TICKET_TERMS = (
    "roof cleaning",
    "moss removal",
    "gutter cleaning",
    "pressure washing",
    "handyman",
    "junk removal",
    "snow removal",
    "christmas lights",
)

# Written out rather than reused from HARDSCAPE_FRANCHISE_DENYLIST even though
# they overlap: that list is only exterior-remodel franchises because roofing
# used to resolve to the hardscaping profile, and pruning it is a separate change
# that must not silently empty this one.
ROOFING_FRANCHISE_DENYLIST = (
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
    "roof maxx",
    "1-800-hansons",
    "1800 hansons",
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
        min_reviews=8,
        adjacency_exclusions=GREEN_INDUSTRY_ADJACENCY_EXCLUSIONS,
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
        aliases=("car detailing", "auto detailing", "mobile detailing", "detailing", "detailer"),
        min_reviews=8,
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
        min_reviews=8,
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
        category_denylist=GARAGE_DOOR_CATEGORY_DENYLIST,
        exclude_terms=(),
        aliases=(
            "garage doors",
            "garage door",
            "garage door repair",
            "garage door installation",
            "garage door service",
            "overhead doors",
            "overhead door",
        ),
        min_reviews=8,
    ),
    "plumbing": VerticalProfile(
        key="plumbing",
        label="Plumbing",
        family="plumbing",
        batch_terms=PLUMBING_BATCH_TERMS,
        service_terms=PLUMBING_SERVICE_TERMS,
        high_ticket_terms=PLUMBING_HIGH_TICKET_TERMS,
        deny_terms=PLUMBING_DENY_TERMS,
        low_ticket_terms=PLUMBING_DENY_TERMS,
        franchise_denylist=PLUMBING_FRANCHISE_DENYLIST,
        exclude_terms=(),
        aliases=("plumber", "plumbers", "plumbing contractor", "plumbing company"),
        min_reviews=8,
    ),
    "epoxy": VerticalProfile(
        key="epoxy",
        label="Epoxy Flooring",
        family="epoxy",
        batch_terms=EPOXY_BATCH_TERMS,
        service_terms=EPOXY_SERVICE_TERMS,
        high_ticket_terms=EPOXY_HIGH_TICKET_TERMS,
        deny_terms=EPOXY_DENY_TERMS,
        low_ticket_terms=EPOXY_LOW_TICKET_TERMS,
        franchise_denylist=EPOXY_FRANCHISE_DENYLIST,
        exclude_terms=(),
        aliases=(
            "epoxy",
            "epoxy flooring",
            "epoxy floors",
            "epoxy floor coating",
            "garage floor coating",
            "garage floor coatings",
            "concrete coating",
            "concrete coatings",
            "floor coating",
            "floor coatings",
        ),
        # Must equal `vertical_profiles/epoxy.json`'s min_reviews: that file is
        # what generate_search_config writes into the search config (the
        # search-time floor) while `icp.hard_icp_gate` reads this one (the
        # score-time floor). Two numbers here means a lead can be searched for
        # and then skipped, or skipped before it is ever searched.
        min_reviews=8,
    ),
    "roofing": VerticalProfile(
        key="roofing",
        label="Roofing",
        family="roofing",
        batch_terms=ROOFING_BATCH_TERMS,
        service_terms=ROOFING_SERVICE_TERMS,
        high_ticket_terms=ROOFING_HIGH_TICKET_TERMS,
        deny_terms=ROOFING_DENY_TERMS,
        low_ticket_terms=ROOFING_LOW_TICKET_TERMS,
        franchise_denylist=ROOFING_FRANCHISE_DENYLIST,
        exclude_terms=(),
        aliases=(
            "roofing",
            "roofer",
            "roofers",
            "roofing contractor",
            "roofing company",
            "roof replacement",
            "roof repair",
        ),
        # Kept in step with `vertical_profiles/roofing.json` for the same reason
        # as epoxy above: search-time and score-time read different files.
        min_reviews=8,
    ),
}


# Not a member of VERTICAL_PROFILES: it is the *absence* of a vertical, and the
# registry is iterated (search-config generation, Airtable label maps, display
# inference) as the list of niches we actually prospect.
UNRESOLVED_PROFILE = VerticalProfile(
    key="",
    label="",
    family="",
    batch_terms=(),
    service_terms=(),
    high_ticket_terms=(),
    deny_terms=(),
    low_ticket_terms=(),
    franchise_denylist=(),
    exclude_terms=(),
)


def clean_vertical_label(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


@lru_cache(maxsize=4096)
def _left_boundary_pattern(term: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}")


def matches_term(text: str, term: str) -> bool:
    """Does *text* contain *term* as the start of a word?

    Plain `term in text` reads a term that is merely the TAIL of a longer word
    as a hit. That is not hypothetical: "roofing" is a suffix of
    "waterproofing", so every basement/foundation waterproofing business
    matched ROOFING_BATCH_TERMS and was classified — and written to Airtable —
    as roofing. "sunroofing" and "sunroof repair" land the same way.

    The boundary is deliberately LEFT-ONLY. Requiring a right boundary too
    would be the obvious `\\b...\\b`, but it would silently stop "gutter"
    matching "gutters" and "paver" matching "pavers"; these lists are written
    in the singular and rely on suffix growth. A term that is a prefix of a
    longer word is nearly always the same trade ("roofing" -> "roofings"),
    while a term that is a suffix of one usually is not ("waterproofing").
    """
    if not term:
        return False
    return _left_boundary_pattern(term).search(text) is not None


def matches_any_term(text: str, terms: Iterable[str]) -> bool:
    return any(matches_term(text, term) for term in terms)


PROFILE_ALIAS_TO_KEY: Mapping[str, str] = {
    alias: profile.key
    for profile in VERTICAL_PROFILES.values()
    for alias in {clean_vertical_label(profile.key), clean_vertical_label(profile.label), *profile.aliases}
    if alias
}


def resolve_profile(vertical: str, *, source_query: str = "", fallback: str = "hardscaping") -> VerticalProfile:
    """Resolve a lead to its profile; `fallback=""` means "do not guess".

    The last line used to hand back hardscaping unconditionally, which ignored
    the one thing an empty fallback is for. Scoring v3 asks with `fallback=""`
    because it needs the exact vertical it was given or nothing — its
    "exact vertical and market are required" guard was unreachable while an
    unknown niche silently arrived as hardscaping and got scored against
    hardscaping's terms and thresholds.

    Refusing returns `UNRESOLVED_PROFILE` rather than None so the return type
    stays a VerticalProfile: callers read `.key` straight off the result, and a
    keyless profile surfaces the refusal as an empty key instead of an
    AttributeError. Any non-empty fallback still ends at hardscaping exactly as
    before.
    """
    fallback_key = _normalize_key(fallback)
    for candidate in (_normalize_key(vertical), _infer_profile_key(source_query), fallback_key):
        if candidate in VERTICAL_PROFILES:
            return VERTICAL_PROFILES[candidate]
    return VERTICAL_PROFILES["hardscaping"] if fallback_key else UNRESOLVED_PROFILE


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
    if matches_any_term(text, DETAILING_TERMS):
        return "detailing"
    if matches_any_term(text, WATER_RESTORATION_TERMS):
        return "water_restoration"
    if matches_any_term(text, GARAGE_DOOR_TERMS):
        return "garage_doors"
    # Both of these must be tested before hardscaping: "concrete floor coating"
    # contains "concrete" and hardscaping claims that word, so an epoxy query
    # would resolve to hardscaping if the order were reversed.
    if matches_any_term(text, EPOXY_BATCH_TERMS):
        return "epoxy"
    if matches_any_term(text, ROOFING_BATCH_TERMS):
        return "roofing"
    if matches_any_term(text, HARDSCAPE_BATCH_TERMS + HARDSCAPE_SERVICE_TERMS):
        return "hardscaping"
    return ""
