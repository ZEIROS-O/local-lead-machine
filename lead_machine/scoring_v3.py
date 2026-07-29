from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .compliance import suppression_disqualifiers
from .review_velocity import ReviewSnapshotStore, build_review_velocity, mine_review_pain
from .scoring_profiles import (
    SCORING_POLICY_VERSION,
    ScoringProfile,
    ScoringProfileError,
    infer_market,
    infer_vertical_key,
    load_scoring_profile,
    supported_scoring_cells,
    validate_scoring_profile_directory,
)


@dataclass(frozen=True)
class ScoreV3Result:
    policy_version: str
    profile_id: str
    profile_version: str
    launch_mode: str
    vertical: str
    market: str
    component_scores: Mapping[str, int]
    rank_score: int | None
    disposition: str
    offer_lane: str
    named_leak: str
    reason_codes: tuple[str, ...]
    blocking_gates: tuple[str, ...]
    hard_disqualifiers: tuple[str, ...]
    reputation_risks: tuple[str, ...]
    review_velocity: Mapping[str, Any]
    review_pain: Mapping[str, Any]
    contributing_observations: tuple[Mapping[str, Any], ...]
    unknown_observations: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["component_scores"] = dict(self.component_scores)
        value["reason_codes"] = list(self.reason_codes)
        value["blocking_gates"] = list(self.blocking_gates)
        value["hard_disqualifiers"] = list(self.hard_disqualifiers)
        value["reputation_risks"] = list(self.reputation_risks)
        value["review_velocity"] = dict(self.review_velocity)
        value["review_pain"] = dict(self.review_pain)
        value["contributing_observations"] = [dict(item) for item in self.contributing_observations]
        value["unknown_observations"] = [dict(item) for item in self.unknown_observations]
        return value


def score_v3(
    enriched: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    fallback_vertical: str = "",
    cohort_rates: Sequence[float] = (),
) -> ScoreV3Result:
    vertical = infer_vertical_key(enriched, fallback=fallback_vertical)
    market = infer_market(enriched) or infer_market(row)
    cohort_key = f"{vertical}__{market}" if vertical and market else ""
    try:
        validate_scoring_profile_directory()
        profile = load_scoring_profile(vertical, market)
    except ScoringProfileError as exc:
        review_pain = mine_review_pain(enriched)
        review_velocity = build_review_velocity(enriched, cohort_key=cohort_key, cohort_rates=cohort_rates)
        supported = set(supported_scoring_cells())
        supported_verticals = {cell.split("__", 1)[0] for cell in supported}
        outside_target = bool(vertical in supported_verticals and market and cohort_key not in supported)
        # A compliance disqualifier has to survive a missing profile. This
        # branch used to report no disqualifiers at all, so a suppressed lead in
        # a vertical/market with no scoring profile came back clean.
        suppressed = tuple(suppression_disqualifiers(enriched))
        fallback_disqualifiers = tuple(
            sorted(({"outside_target_geography"} if outside_target else set()) | set(suppressed))
        )
        # A hard disqualifier disqualifies whether or not a profile loaded. The
        # missing profile only costs us a rank, and manual-review is a queue a
        # human can still send from, so a suppressed lead parked there is the
        # same leak this branch was added to close.
        fallback_reasons = ["outside_target_geography" if outside_target else "missing_exact_profile"]
        if suppressed:
            fallback_reasons.append("clear_profile_or_compliance_disqualifier")
        # Blockers is operator-facing text. The suppressed lead is newly rejected
        # here and its only other signal is the generic
        # `clear_profile_or_compliance_disqualifier` reason, so lead with the
        # disqualifier. The outside-market reject predates this branch and already
        # names itself in `reason_codes`, so it keeps the exact profile error it
        # has always reported rather than churning for a redundant prefix.
        fallback_blockers = ("hard_disqualifier", str(exc)) if suppressed else (str(exc),)
        return ScoreV3Result(
            policy_version=SCORING_POLICY_VERSION,
            profile_id="",
            profile_version="",
            launch_mode="shadow",
            vertical=vertical,
            market=market,
            component_scores={},
            rank_score=None,
            disposition="reject" if fallback_disqualifiers else "manual-review",
            offer_lane=_offer_lane(row, review_pain),
            named_leak=_named_leak(row, review_pain),
            reason_codes=tuple(fallback_reasons),
            blocking_gates=fallback_blockers,
            hard_disqualifiers=fallback_disqualifiers,
            reputation_risks=tuple(review_pain.get("reputation_risk_categories") or ()),
            review_velocity=review_velocity,
            review_pain=review_pain,
            contributing_observations=(),
            unknown_observations=(),
        )

    resolved_cohort_rates = list(cohort_rates)
    if not resolved_cohort_rates and cohort_key:
        snapshot_path = Path(os.getenv("REVIEW_SNAPSHOT_DB", "run_state/review_velocity.sqlite3"))
        if snapshot_path.exists():
            resolved_cohort_rates = ReviewSnapshotStore(snapshot_path).cohort_rates(
                vertical=vertical,
                market=market,
            )
    review_velocity = build_review_velocity(
        enriched,
        cohort_key=cohort_key,
        cohort_rates=resolved_cohort_rates,
        minimum_cohort_size=profile.minimum_cohort_size,
        max_age_days=profile.review_observation_max_age_days,
    )

    review_pain = mine_review_pain(
        enriched,
        recency_days=profile.review_pain_recency_days,
        max_observation_age_days=profile.review_observation_max_age_days,
    )
    observations, unknown_observations = _normalized_external_observations(
        enriched,
        max_age_days=profile.external_signal_max_age_days,
    )
    contributing_observations: List[Mapping[str, Any]] = list(observations)
    if review_velocity.get("state") == "present":
        contributing_observations.append(
            {"signal_id": "review_velocity", "kind": "review_velocity", **dict(review_velocity)}
        )
    else:
        unknown_observations.append(
            {"signal_id": "review_velocity", "kind": "review_velocity", **dict(review_velocity)}
        )
    if review_pain.get("state") == "present":
        contributing_observations.append(
            {"signal_id": "review_pain", "kind": "review_pain", **dict(review_pain)}
        )
    else:
        unknown_observations.append(
            {"signal_id": "review_pain", "kind": "review_pain", **dict(review_pain)}
        )

    hard_disqualifiers = _hard_disqualifiers(enriched, row, profile)
    reputation_risks = _reputation_risks(enriched, row, review_pain)
    named_leak = _named_leak(row, review_pain, observations)
    offer_lane = _offer_lane(row, review_pain, profile, observations)

    components = {
        "icp_fit": _score_icp_fit(enriched, hard_disqualifiers, profile),
        "business_demand_capacity": _score_business_demand(enriched, row, profile),
        "review_velocity": int(review_velocity.get("score") or 0),
        "problem_evidence": _score_problem_evidence(row, review_pain, observations),
        "offer_solution_fit": _score_offer_fit(offer_lane, named_leak, profile),
        "reachability": _score_reachability(row),
        "evidence_confidence": _score_evidence_confidence(
            enriched, row, review_velocity, review_pain, observations
        ),
    }
    components = {
        name: min(int(profile.weights[name]), max(0, int(value)))
        for name, value in components.items()
    }
    total = sum(components.values())
    blockers: List[str] = []
    reasons: List[str] = []
    if hard_disqualifiers:
        blockers.append("hard_disqualifier")
    if not named_leak:
        blockers.append("missing_verified_named_leak")
    if components["reachability"] <= 0:
        blockers.append("missing_eligible_contact_channel")
    contact_state = str(row.get("Contact Eligibility") or row.get("contact_eligibility") or "").strip().lower()
    if contact_state and contact_state != "contact-now":
        blockers.append("contact_eligibility_not_contact_now")
    governed_sendable = str(row.get("Sendable") or row.get("sendable") or "").strip().lower()
    if governed_sendable != "yes":
        blockers.append("independent_sendability_gate_not_passed")
    if components["evidence_confidence"] < profile.minimum_evidence_confidence:
        blockers.append("insufficient_evidence_confidence")
    if not offer_lane or offer_lane not in profile.offer_lanes:
        blockers.append("missing_matching_offer_lane")
    if reputation_risks:
        blockers.append("reputation_risk_requires_review")
    if total < profile.auto_eligible_min_score:
        blockers.append("rank_below_auto_eligible_threshold")

    if hard_disqualifiers:
        disposition = "reject"
        reasons.append("clear_profile_or_compliance_disqualifier")
    elif not blockers:
        disposition = "auto-eligible"
        reasons.append("high_confidence_fit_with_verified_offer_matching_leak")
    else:
        disposition = "manual-review"
        reasons.append("uncertain_or_incomplete_case_held_for_human_review")

    if review_velocity.get("velocity_band") in {"surging", "active"}:
        reasons.append("review_momentum_increases_commercial_materiality")
    if review_velocity.get("velocity_band") == "unknown":
        reasons.append("review_velocity_unknown_is_neutral")
    if review_pain.get("verified_categories"):
        reasons.append("recurring_review_pain_verified")

    return ScoreV3Result(
        policy_version=profile.policy_version,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        launch_mode=profile.launch_mode,
        vertical=profile.vertical,
        market=profile.market,
        component_scores=components,
        rank_score=total,
        disposition=disposition,
        offer_lane=offer_lane,
        named_leak=named_leak,
        reason_codes=tuple(reasons),
        blocking_gates=tuple(blockers),
        hard_disqualifiers=tuple(hard_disqualifiers),
        reputation_risks=tuple(reputation_risks),
        review_velocity=review_velocity,
        review_pain=review_pain,
        contributing_observations=tuple(contributing_observations),
        unknown_observations=tuple(unknown_observations),
    )


def attach_scoring_v3(
    enriched: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    fallback_vertical: str = "",
    cohort_rates: Sequence[float] = (),
) -> Dict[str, Any]:
    updated = dict(row)
    result = score_v3(enriched, updated, fallback_vertical=fallback_vertical, cohort_rates=cohort_rates)
    payload = result.to_dict()
    components = payload["component_scores"]
    velocity = payload["review_velocity"]
    pain = payload["review_pain"]
    updated.update(
        {
            "Scoring Policy Version": result.policy_version,
            "Scoring Profile ID": result.profile_id,
            "Scoring Profile Version": result.profile_version,
            "Scoring V3 Mode": result.launch_mode,
            "Scoring V3 Rank": "" if result.rank_score is None else result.rank_score,
            "Scoring V3 Disposition": result.disposition,
            "Scoring V3 Offer Lane": result.offer_lane,
            "Scoring V3 Named Leak": result.named_leak,
            "Scoring V3 Blockers": "; ".join(result.blocking_gates),
            "Scoring V3 Reasons": "; ".join(result.reason_codes),
            "Score V3 ICP Fit": components.get("icp_fit", ""),
            "Score V3 Business Demand": components.get("business_demand_capacity", ""),
            "Score V3 Review Velocity": components.get("review_velocity", ""),
            "Score V3 Problem Evidence": components.get("problem_evidence", ""),
            "Score V3 Offer Fit": components.get("offer_solution_fit", ""),
            "Score V3 Reachability": components.get("reachability", ""),
            "Score V3 Evidence Confidence": components.get("evidence_confidence", ""),
            "Review Velocity Band": velocity.get("velocity_band", "unknown"),
            "Review Velocity Per 30d": velocity.get("reviews_per_30d", ""),
            "Review Velocity Percentile": velocity.get("cohort_percentile", ""),
            "Review Velocity Source": velocity.get("source", ""),
            "Review Velocity Confidence": velocity.get("confidence", ""),
            "Review Velocity Bound": velocity.get("bound", ""),
            "Review Pain Categories": "; ".join(pain.get("verified_categories") or []),
            "Review Reputation Risks": "; ".join(pain.get("reputation_risk_categories") or []),
            "Scoring V3 Observation Count": len(result.contributing_observations),
            "Scoring V3 Unknown Observations": "; ".join(
                str(item.get("signal_id") or item.get("kind") or "unknown")
                for item in result.unknown_observations
            ),
            "Legacy Scoring": "No",
            "Revalidation Recommended": "No",
            "Scoring V3 JSON": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "scoring_v3": payload,
        }
    )
    return updated


def _score_icp_fit(
    enriched: Mapping[str, Any],
    hard_disqualifiers: Sequence[str],
    profile: ScoringProfile,
) -> int:
    if hard_disqualifiers:
        return 0
    google = enriched.get("google") if isinstance(enriched.get("google"), Mapping) else {}
    website = enriched.get("website") if isinstance(enriched.get("website"), Mapping) else {}
    text = " ".join(
        str(value or "").lower()
        for value in (
            google.get("business_name"),
            google.get("category"),
            google.get("source_query"),
            website.get("combined_text"),
        )
    )
    service_matches = sum(1 for term in profile.vertical_terms if term.lower() in text)
    high_ticket_matches = sum(1 for term in profile.high_value_services if term.lower() in text)
    return min(profile.weights["icp_fit"], 12 + min(4, service_matches * 2) + min(4, high_ticket_matches * 2))


def _score_business_demand(
    enriched: Mapping[str, Any], row: Mapping[str, Any], profile: ScoringProfile
) -> int:
    google = enriched.get("google") if isinstance(enriched.get("google"), Mapping) else {}
    rating = _float(row.get("Rating") or google.get("rating"))
    reviews = _integer(row.get("Reviews") or google.get("reviews"))
    score = 0
    if rating >= 4.8:
        score += 8
    elif rating >= 4.5:
        score += 6
    elif rating >= 4.3:
        score += 3
    elif rating > 0:
        score += 1
    thresholds = profile.business_review_thresholds
    if reviews >= thresholds["mature"]:
        score += 7
    elif reviews >= thresholds["established"]:
        score += 6
    elif reviews >= thresholds["some_proof"]:
        score += 4
    elif reviews >= 5:
        score += 2
    return min(profile.weights["business_demand_capacity"], score)


def _score_problem_evidence(
    row: Mapping[str, Any],
    review_pain: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]] = (),
) -> int:
    pain_score = _integer(row.get("Visible Pain Score") or row.get("visible_pain_score"))
    website_points = min(25, round(pain_score * 25 / 40)) if pain_score > 0 else 0
    verified = len(review_pain.get("verified_categories") or [])
    review_points = min(25, 17 + verified * 3) if verified else 0
    external_points = 18 if _external_named_leak(observations) else 0
    return max(website_points, review_points, external_points)


def _score_offer_fit(offer_lane: str, named_leak: str, profile: ScoringProfile) -> int:
    if not offer_lane or offer_lane not in profile.offer_lanes:
        return 0
    return profile.weights["offer_solution_fit"] if named_leak else round(profile.weights["offer_solution_fit"] / 3)


def _score_reachability(row: Mapping[str, Any]) -> int:
    score = 0
    if str(row.get("Email") or row.get("email") or "").strip():
        score += 6
    if str(row.get("Phone") or row.get("phone") or "").strip():
        score += 4
    return min(10, score)


def _score_evidence_confidence(
    enriched: Mapping[str, Any],
    row: Mapping[str, Any],
    velocity: Mapping[str, Any],
    review_pain: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]] = (),
) -> int:
    score = 0
    evidence = enriched.get("evidence") if isinstance(enriched.get("evidence"), Mapping) else {}
    evidence_items = evidence.get("evidence_items") if isinstance(evidence, Mapping) else []
    if isinstance(evidence_items, list) and _unique_evidence_count(evidence_items) >= 2:
        score += 2
    if str(row.get("Send Trigger") or row.get("send_trigger") or "").strip():
        score += 1
    if review_pain.get("verified_categories"):
        score += 2
    elif float(velocity.get("confidence") or 0) >= 0.75:
        score += 1
    if observations:
        score += 1
    return min(5, score)


def _named_leak(
    row: Mapping[str, Any],
    review_pain: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]] = (),
) -> str:
    trigger = str(row.get("Send Trigger") or row.get("send_trigger") or "").strip()
    if trigger:
        return trigger
    categories = review_pain.get("verified_categories") or []
    if categories:
        return "Recurring review evidence: " + ", ".join(str(value) for value in categories)
    return _external_named_leak(observations)


def _offer_lane(
    row: Mapping[str, Any],
    review_pain: Mapping[str, Any],
    profile: ScoringProfile | None = None,
    observations: Sequence[Mapping[str, Any]] = (),
) -> str:
    lanes = list(review_pain.get("offer_lanes") or [])
    if lanes:
        for preferred in ("Answer It", "Close It", "Keep"):
            if preferred in lanes:
                return preferred
    explicit = str(row.get("LPM Offer Component") or row.get("offer_component") or "").strip()
    if explicit:
        return explicit
    angle = str(row.get("Offer Fit") or row.get("Best Service Angle") or row.get("best_angle") or "").strip().lower()
    if profile and angle in profile.offer_mappings:
        return profile.offer_mappings[angle]
    if _external_named_leak(observations):
        return "Get Found"
    return ""


def _hard_disqualifiers(
    enriched: Mapping[str, Any], row: Mapping[str, Any], profile: ScoringProfile
) -> List[str]:
    google = enriched.get("google") if isinstance(enriched.get("google"), Mapping) else {}
    identity_text = _normalized_match_text(" ".join(
        str(value or "").lower()
        for value in (
            google.get("business_name"),
            google.get("category"),
            google.get("source_query"),
        )
    ))
    result: List[str] = []
    status = str(google.get("business_status") or google.get("businessStatus") or "").upper()
    if status in {"CLOSED_PERMANENTLY", "PERMANENTLY_CLOSED"}:
        result.append("permanently_closed")
    if any(_normalized_match_text(term) in identity_text for term in profile.exclusions):
        result.append("excluded_business_type_or_service")
    suppression = str(row.get("Suppressed") or row.get("Opt Out") or row.get("Do Not Contact") or "").strip().lower()
    if suppression in {"yes", "true", "1"}:
        result.append("suppressed_or_opted_out")
    result.extend(suppression_disqualifiers(enriched))
    return sorted(set(result))


def _normalized_match_text(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _reputation_risks(
    enriched: Mapping[str, Any], row: Mapping[str, Any], review_pain: Mapping[str, Any]
) -> List[str]:
    google = enriched.get("google") if isinstance(enriched.get("google"), Mapping) else {}
    risks = [str(value) for value in review_pain.get("reputation_risk_categories") or []]
    rating = _float(row.get("Rating") or google.get("rating"))
    if 0 < rating < 4.0:
        risks.append("rating_below_4_0")
    return sorted(set(risks))


def _unique_evidence_count(items: Sequence[Any]) -> int:
    identities: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        evidence_id = str(item.get("id") or item.get("evidence_id") or "").strip()
        if evidence_id:
            identities.add(f"id:{evidence_id}")
        else:
            identities.add(json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str))
    return len(identities)


def _external_named_leak(observations: Sequence[Mapping[str, Any]]) -> str:
    for observation in observations:
        kind = str(observation.get("kind") or "")
        value = observation.get("value") if isinstance(observation.get("value"), Mapping) else {}
        if kind == "website_performance":
            score = _float(value.get("performance_score"))
            if 0 < score < 0.5:
                return f"Verified poor mobile performance ({round(score * 100)} PageSpeed score)"
        if kind == "map_pack_visibility":
            voices = value.get("share_of_voices") if isinstance(value.get("share_of_voices"), Mapping) else {}
            numeric = [_float(voices[key]) for key in sorted(voices)]
            numeric = [item for item in numeric if item >= 0]
            if numeric and numeric[-1] < 5.0:
                query = str((observation.get("metadata") or {}).get("query") or "target query")
                return f"Verified weak map visibility for {query} ({numeric[-1]:.1f}% share of voice)"
    return ""


def _normalized_external_observations(
    enriched: Mapping[str, Any], *, max_age_days: int
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates: List[Mapping[str, Any]] = []
    direct = enriched.get("signal_observations")
    if isinstance(direct, list):
        candidates.extend(item for item in direct if isinstance(item, Mapping))
    external = enriched.get("external_signals")
    if isinstance(external, Mapping):
        for item in external.values():
            if isinstance(item, Mapping) and isinstance(item.get("observation"), Mapping):
                candidates.append(item["observation"])
    now = datetime.now(timezone.utc)
    unique: Dict[str, Dict[str, Any]] = {}
    unknown: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(candidates):
        observation = dict(raw)
        identity = str(observation.get("signal_id") or f"anonymous-{index}")
        state = str(observation.get("state") or "unknown")
        observed_at = _datetime(observation.get("observed_at"))
        expires_at = _datetime(observation.get("expires_at"))
        stale = bool(
            observed_at is None
            or (now - observed_at).total_seconds() < 0
            or (now - observed_at).total_seconds() > max_age_days * 86400
            or (expires_at is not None and expires_at <= now)
        )
        if state == "present" and not stale:
            unique[identity] = observation
            continue
        if stale:
            observation["state"] = "unknown"
            observation["value"] = None
            metadata = observation.get("metadata") if isinstance(observation.get("metadata"), Mapping) else {}
            observation["metadata"] = {**metadata, "unknown_reason": "stale_or_undated_observation"}
        unknown[identity] = observation
    return list(unique.values()), list(unknown.values())


def _datetime(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _integer(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
