from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .verticals import clean_vertical_label, resolve_profile


SCORING_POLICY_VERSION = "v3-shadow.1"
REQUIRED_WEIGHTS = {
    "icp_fit",
    "business_demand_capacity",
    "review_velocity",
    "problem_evidence",
    "offer_solution_fit",
    "reachability",
    "evidence_confidence",
}


class ScoringProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ScoringProfile:
    profile_id: str
    profile_version: str
    policy_version: str
    vertical: str
    market: str
    weights: Mapping[str, int]
    auto_eligible_min_score: int
    minimum_cohort_size: int
    minimum_evidence_confidence: int
    offer_lanes: tuple[str, ...]
    launch_mode: str
    business_review_thresholds: Mapping[str, int]
    review_pain_recency_days: int
    review_observation_max_age_days: int
    external_signal_max_age_days: int
    vertical_terms: tuple[str, ...]
    exclusions: tuple[str, ...]
    high_value_services: tuple[str, ...]
    offer_mappings: Mapping[str, str]
    supported_cells: tuple[str, ...]


def load_scoring_profile(
    vertical: str,
    market: str,
    *,
    profiles_dir: Path | None = None,
) -> ScoringProfile:
    root = profiles_dir or Path(__file__).resolve().parents[1] / "scoring_profiles"
    canonical_vertical = resolve_profile(vertical, fallback="").key
    canonical_market = _slug(market)
    if not canonical_vertical or not canonical_market:
        raise ScoringProfileError("Both exact vertical and market are required for scoring v3")
    exact_path = root / f"{canonical_vertical}__{canonical_market}.json"
    if not exact_path.exists():
        raise ScoringProfileError(
            f"Missing exact scoring profile for {canonical_vertical} x {canonical_market}: {exact_path}"
        )
    raw: Dict[str, Any] = {}
    for path in (
        root / "global.json",
        root / f"vertical__{canonical_vertical}.json",
        root / f"metro__{canonical_market}.json",
        exact_path,
    ):
        if path.exists():
            raw = _deep_merge(raw, _read_profile_file(path))
    return validate_scoring_profile(raw, expected_vertical=canonical_vertical, expected_market=canonical_market)


def validate_scoring_profile_directory(profiles_dir: Path | None = None) -> tuple[str, ...]:
    root = profiles_dir or Path(__file__).resolve().parents[1] / "scoring_profiles"
    global_raw = _read_profile_file(root / "global.json")
    supported = _string_tuple(global_raw.get("supported_cells"), "supported_cells", required=True)
    seen: set[str] = set()
    for cell in supported:
        if cell in seen:
            raise ScoringProfileError(f"duplicate supported cell: {cell}")
        seen.add(cell)
        if "__" not in cell:
            raise ScoringProfileError(f"invalid supported cell: {cell}")
        vertical, market = cell.split("__", 1)
        load_scoring_profile(vertical, market, profiles_dir=root)
    exact_files = {
        path.stem
        for path in root.glob("*.json")
        if not path.name.startswith(("global.", "vertical__", "metro__"))
    }
    unexpected = sorted(exact_files - seen)
    if unexpected:
        raise ScoringProfileError(f"exact profiles not declared in supported_cells: {unexpected}")
    return tuple(supported)


def supported_scoring_cells(profiles_dir: Path | None = None) -> tuple[str, ...]:
    root = profiles_dir or Path(__file__).resolve().parents[1] / "scoring_profiles"
    raw = _read_profile_file(root / "global.json")
    return _string_tuple(raw.get("supported_cells"), "supported_cells", required=True)


def validate_scoring_profile(
    raw: Mapping[str, Any],
    *,
    expected_vertical: str = "",
    expected_market: str = "",
) -> ScoringProfile:
    profile_id = str(raw.get("profile_id") or "").strip()
    profile_version = str(raw.get("profile_version") or "").strip()
    policy_version = str(raw.get("policy_version") or "").strip()
    vertical = _slug(str(raw.get("vertical") or ""))
    market = _slug(str(raw.get("market") or ""))
    weights = raw.get("weights")
    if not isinstance(weights, Mapping):
        raise ScoringProfileError("weights must be an object")
    normalized_weights: Dict[str, int] = {}
    for key, value in weights.items():
        if type(value) is not int or value < 0:
            raise ScoringProfileError(f"weight {key} must be a non-negative integer")
        normalized_weights[str(key)] = value
    if set(normalized_weights) != REQUIRED_WEIGHTS:
        missing = sorted(REQUIRED_WEIGHTS - set(normalized_weights))
        extra = sorted(set(normalized_weights) - REQUIRED_WEIGHTS)
        raise ScoringProfileError(f"invalid weight keys; missing={missing}, extra={extra}")
    if sum(normalized_weights.values()) != 100:
        raise ScoringProfileError("weights must sum to 100")
    if not profile_id or not profile_version:
        raise ScoringProfileError("profile_id and profile_version are required")
    if policy_version != SCORING_POLICY_VERSION:
        raise ScoringProfileError(
            f"policy_version must be {SCORING_POLICY_VERSION}, got {policy_version or '<missing>'}"
        )
    if expected_vertical and vertical != expected_vertical:
        raise ScoringProfileError(f"profile vertical {vertical} does not match {expected_vertical}")
    if expected_market and market != expected_market:
        raise ScoringProfileError(f"profile market {market} does not match {expected_market}")
    threshold = _bounded_int(raw.get("auto_eligible_min_score"), 0, 100, "auto_eligible_min_score")
    cohort_size = _bounded_int(raw.get("minimum_cohort_size"), 1, 10_000, "minimum_cohort_size")
    confidence = _bounded_int(raw.get("minimum_evidence_confidence"), 0, 5, "minimum_evidence_confidence")
    lanes = raw.get("offer_lanes")
    if not isinstance(lanes, list) or not lanes or any(not str(value).strip() for value in lanes):
        raise ScoringProfileError("offer_lanes must be a non-empty list")
    launch_mode = str(raw.get("launch_mode") or "shadow").strip().lower()
    if launch_mode not in {"shadow", "active"}:
        raise ScoringProfileError("launch_mode must be shadow or active")
    raw_thresholds = raw.get("business_review_thresholds")
    if not isinstance(raw_thresholds, Mapping):
        raise ScoringProfileError("business_review_thresholds must be an object")
    review_thresholds = {
        key: _bounded_int(raw_thresholds.get(key), 1, 100_000, f"business_review_thresholds.{key}")
        for key in ("some_proof", "established", "mature")
    }
    if not (
        review_thresholds["some_proof"]
        < review_thresholds["established"]
        < review_thresholds["mature"]
    ):
        raise ScoringProfileError("business_review_thresholds must increase from some_proof to mature")
    review_pain_recency_days = _bounded_int(
        raw.get("review_pain_recency_days"), 30, 730, "review_pain_recency_days"
    )
    review_observation_max_age_days = _bounded_int(
        raw.get("review_observation_max_age_days"), 1, 365, "review_observation_max_age_days"
    )
    external_signal_max_age_days = _bounded_int(
        raw.get("external_signal_max_age_days"), 1, 365, "external_signal_max_age_days"
    )
    vertical_terms = _string_tuple(raw.get("vertical_terms"), "vertical_terms", required=True)
    exclusions = _string_tuple(raw.get("exclusions"), "exclusions")
    high_value_services = _string_tuple(
        raw.get("high_value_services"), "high_value_services", required=True
    )
    _validate_term_contract(vertical_terms, exclusions, high_value_services)
    raw_offer_mappings = raw.get("offer_mappings")
    if not isinstance(raw_offer_mappings, Mapping) or not raw_offer_mappings:
        raise ScoringProfileError("offer_mappings must be a non-empty object")
    offer_mappings = {
        str(key).strip().casefold(): str(value).strip()
        for key, value in raw_offer_mappings.items()
        if str(key).strip() and str(value).strip()
    }
    invalid_lanes = sorted(set(offer_mappings.values()) - {str(value).strip() for value in lanes})
    if invalid_lanes:
        raise ScoringProfileError(f"offer_mappings reference unsupported lanes: {invalid_lanes}")
    supported_cells = _string_tuple(raw.get("supported_cells"), "supported_cells", required=True)
    return ScoringProfile(
        profile_id=profile_id,
        profile_version=profile_version,
        policy_version=policy_version,
        vertical=vertical,
        market=market,
        weights=normalized_weights,
        auto_eligible_min_score=threshold,
        minimum_cohort_size=cohort_size,
        minimum_evidence_confidence=confidence,
        offer_lanes=tuple(str(value).strip() for value in lanes),
        launch_mode=launch_mode,
        business_review_thresholds=review_thresholds,
        review_pain_recency_days=review_pain_recency_days,
        review_observation_max_age_days=review_observation_max_age_days,
        external_signal_max_age_days=external_signal_max_age_days,
        vertical_terms=vertical_terms,
        exclusions=exclusions,
        high_value_services=high_value_services,
        offer_mappings=offer_mappings,
        supported_cells=supported_cells,
    )


def _read_profile_file(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoringProfileError(f"Invalid scoring profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScoringProfileError(f"Scoring profile {path} must contain an object")
    return raw


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _string_tuple(value: Any, name: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        if required:
            raise ScoringProfileError(f"{name} must be a non-empty list")
        return ()
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    if required and not normalized:
        raise ScoringProfileError(f"{name} must be a non-empty list")
    folded = [item.casefold() for item in normalized]
    duplicates = sorted({item for item in folded if folded.count(item) > 1})
    if duplicates:
        raise ScoringProfileError(f"{name} contains duplicate terms: {duplicates}")
    return normalized


def _validate_term_contract(
    vertical_terms: Iterable[str], exclusions: Iterable[str], high_value_services: Iterable[str]
) -> None:
    positive = {value.casefold() for value in (*vertical_terms, *high_value_services)}
    negative = {value.casefold() for value in exclusions}
    conflicts = sorted(positive & negative)
    if conflicts:
        raise ScoringProfileError(f"terms conflict between inclusion and exclusion rules: {conflicts}")


def infer_market(record: Mapping[str, Any], *, market_dir: Path | None = None) -> str:
    google = record.get("google") if isinstance(record.get("google"), Mapping) else {}
    explicit_values = [
        str(value or "").strip()
        for value in (record.get("Metro Context"), record.get("market"), google.get("market"))
        if str(value or "").strip()
    ]
    explicit_slugs = {_slug(value) for value in explicit_values if _slug(value)}
    if len(explicit_slugs) == 1:
        return next(iter(explicit_slugs))
    explicit = " ".join(explicit_values)
    source = " ".join(
        str(value or "")
        for value in (
            record.get("source_batch"),
            record.get("Source Batch"),
            record.get("source_geo"),
            record.get("Source Geo"),
            record.get("source_query"),
            record.get("Source Query"),
            google.get("source_batch"),
            google.get("source_geo"),
            google.get("source_query"),
        )
    )
    root = market_dir or Path(__file__).resolve().parents[1] / "market_profiles"
    profiles = _market_profiles(root)
    for text in (explicit, source):
        matches = [market for market in profiles if _contains_token(text, market)]
        if len(matches) == 1:
            return matches[0]

    city = str(record.get("City") or record.get("city") or google.get("city") or "").strip().casefold()
    if not city:
        return ""
    city_matches = []
    for market, raw in profiles.items():
        geo_cities = {
            _geo_city(value).casefold()
            for value in raw.get("geos", [])
            if isinstance(value, str)
        }
        if city in geo_cities:
            city_matches.append(market)
    return city_matches[0] if len(city_matches) == 1 else ""


def infer_vertical_key(record: Mapping[str, Any], *, fallback: str = "") -> str:
    google = record.get("google") if isinstance(record.get("google"), Mapping) else {}
    value = (
        record.get("Vertical")
        or record.get("vertical")
        or google.get("vertical")
        or fallback
    )
    source_query = str(record.get("Source Query") or record.get("source_query") or google.get("source_query") or "")
    return resolve_profile(str(value or ""), source_query=source_query, fallback=fallback or "").key


def _market_profiles(root: Path) -> Dict[str, Dict[str, Any]]:
    profiles: Dict[str, Dict[str, Any]] = {}
    for path in root.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            profiles[path.stem] = value
    return profiles


def _contains_token(text: str, token: str) -> bool:
    normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
    candidate = token.lower().replace("_", " ")
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", normalized))


def _geo_city(value: str) -> str:
    return re.sub(r"\s+[A-Z]{2}(?:\s+\d{5})?$", "", value.strip(), flags=re.IGNORECASE)


def _slug(value: str) -> str:
    cleaned = clean_vertical_label(value)
    return cleaned.replace(" ", "_")


def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ScoringProfileError(f"{name} must be an integer from {minimum} to {maximum}")
    return value
