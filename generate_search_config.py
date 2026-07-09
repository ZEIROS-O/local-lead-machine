#!/usr/bin/env python3
"""Generate a search config from reusable vertical + market profiles.

A new niche/city/size combo (e.g. ``/run epoxy cleveland medium``) should be a
data lookup, not a hand-edited ``search_configs/*.json``. This script combines:

  * ``vertical_profiles/<vertical>.json`` -- search-query phrasing, high-intent
    terms, display name, Airtable value (the things ``verticals.py`` does not
    hold).
  * ``lead_machine/verticals.py`` (``VERTICAL_PROFILES``) -- the canonical
    boost/exclude term layer for verticals already wired into scoring. When the
    vertical lives there, its terms win, so there is ONE source of truth and no
    drift. New verticals (not yet in ``verticals.py``) fall back to the JSON
    profile's own ``boost_terms`` / ``exclude_terms``.
  * ``market_profiles/<market>.json`` -- state, display name, and sized
    (small/medium/full) geo sets.

It writes ``search_configs/<vertical>_<market>_<size>_001.json`` and refreshes
``search_configs/geo_slices/<market>.json`` (the full geo list) so the
``geo_slices_ref`` pattern keeps working for hand-authored configs.

No paid API calls are made -- this is pure file I/O.

Examples:
    python generate_search_config.py --vertical epoxy --market cleveland --size medium
    python generate_search_config.py --vertical roofing --market akron --size small --overwrite
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from lead_machine.verticals import VERTICAL_PROFILES, _normalize_key

VERTICAL_PROFILES_DIR = Path("vertical_profiles")
MARKET_PROFILES_DIR = Path("market_profiles")
CONFIGS_DIR = Path("search_configs")

VALID_SIZES = ("small", "medium", "full")
DEFAULT_LIMIT_PER_QUERY = 10
DEFAULT_MIN_RATING = 4.3
DEFAULT_MIN_REVIEWS = 10
# Per-call result page size (API hard-caps at 20). Pagination depth carries volume.
DEFAULT_PAGE_SIZE = 20
# Size-based search depth: deeper paging where appropriate, single page for small.
SIZE_MAX_PAGES = {"small": 1, "medium": 2, "full": 3}
# Fallback geo-set sizes when a market profile omits an explicit "sizes" entry.
# medium is trimmed to ~6 dense slices because pagination (2 pages) now carries depth
# instead of geo spam.
FALLBACK_SIZE_COUNTS = {"small": 5, "medium": 6}


class ConfigGenError(Exception):
    """Raised to abort generation with a clear (often ``[REFUSE]``) message."""


def _status(message: str, *, quiet: bool) -> None:
    if not quiet:
        print(message, flush=True)


def _dedup(items) -> list[str]:
    """Order-preserving de-duplication of a string list."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _string_list(value) -> list[str]:
    if not value:
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def load_vertical_profile(vertical: str, *, vertical_dir: Path) -> dict:
    path = vertical_dir / f"{vertical}.json"
    if not path.exists():
        raise ConfigGenError(f"[REFUSE] Missing vertical profile: {vertical_dir}/{vertical}.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigGenError(f"[REFUSE] Invalid vertical profile {path}: {exc}") from exc


def load_market_profile(market: str, *, market_dir: Path) -> dict:
    path = market_dir / f"{market}.json"
    if not path.exists():
        raise ConfigGenError(f"[REFUSE] Missing market profile: {market_dir}/{market}.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigGenError(f"[REFUSE] Invalid market profile {path}: {exc}") from exc


def resolve_boost_exclude(vertical: str, profile: dict) -> tuple[list[str], list[str], str]:
    """Return (boost_terms, exclude_terms, source_label).

    Canonical verticals (present in ``verticals.py``) source their boost terms
    from ``high_ticket_terms`` and merge canonical deny/low-ticket terms with the
    profile's extra noise terms. New verticals fall back to the JSON profile.
    """
    canonical = VERTICAL_PROFILES.get(_normalize_key(vertical))
    profile_excludes = _string_list(profile.get("exclude_terms"))
    if canonical is not None:
        boost = list(canonical.high_ticket_terms)
        exclude = _dedup(
            list(canonical.deny_terms) + list(canonical.low_ticket_terms) + profile_excludes
        )
        return boost, exclude, f"verticals.py:{canonical.key}"
    boost = _string_list(profile.get("boost_terms"))
    if not boost:
        boost = _dedup(
            _string_list(profile.get("search_terms")) + _string_list(profile.get("high_intent_terms"))
        )
    return boost, profile_excludes, f"vertical_profiles/{vertical}.json"


def select_geos(market_profile: dict, size: str) -> list[str]:
    full = _string_list(market_profile.get("geos"))
    if not full:
        raise ConfigGenError(
            f"[REFUSE] Market profile '{market_profile.get('market')}' defines no geos"
        )
    if size == "full":
        return full
    sizes = market_profile.get("sizes") or {}
    explicit = _string_list(sizes.get(size))
    if explicit:
        return explicit
    return full[: FALLBACK_SIZE_COUNTS.get(size, len(full))]


def build_queries(profile: dict, location: str) -> list[str]:
    terms = _dedup(
        _string_list(profile.get("search_terms")) + _string_list(profile.get("high_intent_terms"))
    )
    if not terms:
        raise ConfigGenError(
            f"[REFUSE] Vertical profile '{profile.get('vertical')}' defines no search_terms"
        )
    return [f"{term} {location}".strip() for term in terms]


def generate_config(
    *,
    vertical: str,
    market: str,
    size: str,
    batch: Optional[str] = None,
    overwrite: bool = False,
    vertical_dir: Path = VERTICAL_PROFILES_DIR,
    market_dir: Path = MARKET_PROFILES_DIR,
    configs_dir: Path = CONFIGS_DIR,
    geo_dir: Optional[Path] = None,
    quiet: bool = False,
) -> Path:
    """Generate and write a search config; return its path.

    Raises ``ConfigGenError`` (carrying a ``[REFUSE]`` message) on a missing
    profile, an unknown size, or an existing config when ``overwrite`` is False.
    """
    vertical = str(vertical).strip().lower()
    market = str(market).strip().lower()
    size = str(size).strip().lower()
    if size not in VALID_SIZES:
        raise ConfigGenError(f"[REFUSE] Invalid size '{size}'; expected one of {', '.join(VALID_SIZES)}")

    geo_dir = geo_dir if geo_dir is not None else configs_dir / "geo_slices"
    batch_name = (batch or f"{vertical}_{market}_{size}_001").strip()
    config_path = configs_dir / f"{batch_name}.json"
    if config_path.exists() and not overwrite:
        raise ConfigGenError(
            f"[REFUSE] Config already exists: {config_path} (pass --overwrite to replace it)"
        )

    vertical_profile = load_vertical_profile(vertical, vertical_dir=vertical_dir)
    market_profile = load_market_profile(market, market_dir=market_dir)

    state = str(market_profile.get("state") or "").strip()
    display = str(market_profile.get("display_name") or market.title()).strip()
    location = f"{display} {state}".strip()

    geos = select_geos(market_profile, size)
    queries = build_queries(vertical_profile, location)
    boost, exclude, boost_source = resolve_boost_exclude(vertical, vertical_profile)

    # page_size defaults to 20 (or a profile override / legacy limit_per_query); depth
    # comes from size. limit_per_query mirrors page_size so legacy readers stay valid.
    page_size = min(
        int(vertical_profile.get("page_size", vertical_profile.get("limit_per_query", DEFAULT_PAGE_SIZE))),
        DEFAULT_PAGE_SIZE,
    )
    max_pages = int(vertical_profile.get("max_pages_per_query", SIZE_MAX_PAGES.get(size, 1)))

    config = {
        "batch": batch_name,
        "vertical": vertical,
        "location": location,
        "limit_per_query": page_size,
        "page_size": page_size,
        "max_pages_per_query": max_pages,
        "queries": queries,
        "geo_slices": geos,
        "geo_slices_ref": market,
        "min_rating": float(vertical_profile.get("min_rating", DEFAULT_MIN_RATING)),
        "min_reviews": int(vertical_profile.get("min_reviews", DEFAULT_MIN_REVIEWS)),
        "allow_missing_website": bool(vertical_profile.get("allow_missing_website", True)),
        "exclude_terms": exclude,
        "boost_terms": boost,
    }
    airtable_vertical = str(vertical_profile.get("airtable_vertical") or "").strip()
    if airtable_vertical:
        config["airtable_vertical"] = airtable_vertical

    # Refresh the shared geo_slices file (full market list) so geo_slices_ref
    # keeps resolving for hand-authored configs and the add-metro pattern.
    geo_dir.mkdir(parents=True, exist_ok=True)
    full_geos = _string_list(market_profile.get("geos"))
    geo_file = geo_dir / f"{market}.json"
    geo_file.write_text(json.dumps(full_geos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    configs_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    _status(f"[GEN] vertical={vertical} market={market} size={size} batch={batch_name}", quiet=quiet)
    _status(f"[GEN] location={location} queries={len(queries)} geo_slices={len(geos)}", quiet=quiet)
    _status(f"[GEN] page_size={page_size} max_pages_per_query={max_pages} (size={size})", quiet=quiet)
    _status(f"[GEN] boost_terms={len(boost)} (source={boost_source}) exclude_terms={len(exclude)}", quiet=quiet)
    _status(f"[GEN] wrote {config_path}", quiet=quiet)
    _status(f"[GEN] refreshed {geo_file} ({len(full_geos)} geos)", quiet=quiet)
    return config_path


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a search config from vertical + market profiles")
    parser.add_argument("--vertical", required=True, help="e.g. epoxy, detailing, roofing")
    parser.add_argument("--market", required=True, help="e.g. cleveland, akron, columbus")
    parser.add_argument("--size", required=True, choices=list(VALID_SIZES))
    parser.add_argument("--batch", default="", help="Explicit batch name (default <vertical>_<market>_<size>_001)")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing config")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        generate_config(
            vertical=args.vertical,
            market=args.market,
            size=args.size,
            batch=args.batch or None,
            overwrite=args.overwrite,
        )
    except ConfigGenError as exc:
        print(str(exc), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
