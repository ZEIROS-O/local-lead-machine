from __future__ import annotations

import math
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .review_pain_verifier import verify_review_pain


REVIEW_VELOCITY_VERSION = 3
SQLITE_BUSY_TIMEOUT_SECONDS = 10.0
MIN_SNAPSHOT_SPAN_DAYS = 14
MIN_COHORT_SIZE = 20
DEFAULT_REVIEW_OBSERVATION_MAX_AGE_DAYS = 45
VELOCITY_SCORE_BY_BAND = {
    "surging": 10,
    "active": 8,
    "steady": 5,
    "stale": 2,
    "unknown": 5,
}


@dataclass(frozen=True)
class ReviewVelocityObservation:
    version: int = REVIEW_VELOCITY_VERSION
    state: str = "unknown"
    source: str = "insufficient_history"
    observed_at: str = ""
    expires_at: str = ""
    period_days: float | None = None
    count_start: int | None = None
    count_end: int | None = None
    reviews_added: int | None = None
    reviews_added_30d: int | None = None
    reviews_added_90d: int | None = None
    reviews_per_30d: float | None = None
    acceleration: float | None = None
    consistency: float | None = None
    cohort_key: str = ""
    cohort_size: int = 0
    cohort_percentile: float | None = None
    velocity_band: str = "unknown"
    score: int = 5
    bound: str = "unknown"
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    anomalies: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = list(self.evidence)
        value["anomalies"] = list(self.anomalies)
        return value


@dataclass(frozen=True)
class ReviewPainFinding:
    category: str
    review_id: str
    observed_at: str
    rating: float | None
    excerpt: str
    evidence_ref: str
    classifier_result: str
    verifier_result: str
    severe: bool = False
    corroborating_evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["corroborating_evidence_refs"] = list(self.corroborating_evidence_refs)
        return value


@dataclass(frozen=True)
class ReviewPainProfile:
    version: int = 1
    state: str = "unknown"
    observed_at: str = ""
    expires_at: str = ""
    confidence: float = 0.0
    verified_categories: tuple[str, ...] = ()
    reputation_risk_categories: tuple[str, ...] = ()
    offer_lanes: tuple[str, ...] = ()
    findings: tuple[ReviewPainFinding, ...] = ()
    reviews_scanned: int = 0
    owner_response_rate: float | None = None
    evidence_refs: tuple[str, ...] = ()
    anomalies: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "state": self.state,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "confidence": self.confidence,
            "verified_categories": list(self.verified_categories),
            "reputation_risk_categories": list(self.reputation_risk_categories),
            "offer_lanes": list(self.offer_lanes),
            "findings": [finding.to_dict() for finding in self.findings],
            "reviews_scanned": self.reviews_scanned,
            "owner_response_rate": self.owner_response_rate,
            "evidence_refs": list(self.evidence_refs),
            "anomalies": list(self.anomalies),
        }


PAIN_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "unreachable": (
        r"could(?:n't| not) (?:reach|get (?:ahold|a hold) of)",
        r"no one (?:answered|answers)",
        r"never (?:answered|picked up)",
        r"phone (?:just )?(?:rang|rings)",
    ),
    "no_callback": (
        r"never (?:called|got) back",
        r"no call ?back",
        r"did(?:n't| not) return (?:my |the )?call",
        r"waiting for (?:a )?call ?back",
    ),
    "quote_followup": (
        r"never (?:sent|received|got) (?:the |a )?(?:quote|estimate)",
        r"no follow[- ]?up",
        r"did(?:n't| not) follow up",
        r"ghosted (?:me|us)",
    ),
    "scheduling": (
        r"missed (?:the |my |our )?appointment",
        r"never showed|no[- ]show",
        r"rescheduled (?:again|multiple)",
        r"could(?:n't| not) (?:schedule|book)",
    ),
    "logistics": (
        r"(?:hours?|days?) late",
        r"did(?:n't| not) arrive",
        r"wrong (?:address|location|vehicle|job)",
        r"lost (?:my |our )?(?:keys|vehicle|paperwork)",
    ),
    "retention": (
        r"won(?:'t| not) use (?:them|again)",
        r"lost (?:a |my )?customer",
        r"cancelled (?:my |our )?(?:service|appointment)",
    ),
    "service_quality": (
        r"poor quality|terrible job|bad job|damaged",
        r"had to (?:redo|fix) (?:it|their work)",
        r"unfinished|incomplete work",
    ),
    "integrity_or_safety": (
        r"scam|fraud|stole|theft|dishonest",
        r"unsafe|dangerous|threaten",
        r"charged (?:me |us )?(?:twice|without)",
    ),
}

OPERATIONAL_CATEGORIES = {
    "unreachable",
    "no_callback",
    "quote_followup",
    "scheduling",
    "logistics",
    "retention",
}
REPUTATION_RISK_CATEGORIES = {"service_quality", "integrity_or_safety"}
OFFER_BY_CATEGORY = {
    "unreachable": "Answer It",
    "no_callback": "Answer It",
    "quote_followup": "Close It",
    "scheduling": "Keep",
    "logistics": "Keep",
    "retention": "Keep",
}


class ReviewSnapshotStore:
    """SQLite-backed history of Google review-count observations.

    The store contains lead identifiers and aggregate counts only; review text is
    deliberately not persisted here.
    """

    def __init__(self, path: Path | str = "run_state/review_velocity.sqlite3") -> None:
        self.path = Path(path)
        # Enrichment now runs leads concurrently, and a sqlite3 connection
        # cannot be shared across threads — hence a fresh connection per call,
        # a write lock so concurrent workers serialise instead of racing, and a
        # busy timeout so a reader waiting on that write blocks briefly rather
        # than raising "database is locked".
        self._write_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_snapshots (
                place_key TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                review_count INTEGER NOT NULL,
                rating REAL,
                vertical TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                PRIMARY KEY (place_key, observed_at, source)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_snapshots_lookup "
            "ON review_snapshots(place_key, observed_at)"
        )
        return connection

    def record_snapshot(
        self,
        *,
        place_key: str,
        review_count: int,
        rating: float | None = None,
        vertical: str = "",
        market: str = "",
        observed_at: str | datetime | None = None,
        source: str = "google_places",
    ) -> None:
        key = str(place_key or "").strip()
        if not key:
            raise ValueError("place_key is required for a review snapshot")
        if int(review_count) < 0:
            raise ValueError("review_count cannot be negative")
        timestamp = _iso_timestamp(observed_at or datetime.now(timezone.utc))
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO review_snapshots "
                "(place_key, observed_at, review_count, rating, vertical, market, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, timestamp, int(review_count), rating, vertical, market, source),
            )

    def history(self, place_key: str) -> List[Dict[str, Any]]:
        key = str(place_key or "").strip()
        if not key or not self.path.exists():
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT observed_at, review_count, rating, vertical, market, source "
                "FROM review_snapshots WHERE place_key = ? ORDER BY observed_at",
                (key,),
            ).fetchall()
        return [
            {
                "observed_at": row[0],
                "review_count": row[1],
                "rating": row[2],
                "vertical": row[3],
                "market": row[4],
                "source": row[5],
            }
            for row in rows
        ]

    def cohort_rates(self, *, vertical: str, market: str) -> List[float]:
        if not self.path.exists():
            return []
        with self._connect() as connection:
            keys = connection.execute(
                "SELECT DISTINCT place_key FROM review_snapshots "
                "WHERE vertical = ? AND market = ?",
                (vertical, market),
            ).fetchall()
        rates: List[float] = []
        now = datetime.now(timezone.utc)
        for (place_key,) in keys:
            history = [
                item
                for item in self.history(str(place_key))
                if str(item.get("vertical") or "") == vertical
                and str(item.get("market") or "") == market
            ]
            latest = _latest_snapshot_at(history)
            if latest is None or (now - latest).total_seconds() > DEFAULT_REVIEW_OBSERVATION_MAX_AGE_DAYS * 86400:
                continue
            observation = _velocity_from_snapshots(history, now)
            rate = observation.get("reviews_per_30d") if observation else None
            if _finite_number(rate) and float(rate) >= 0:
                rates.append(float(rate))
        return rates


def build_review_velocity(
    record: Mapping[str, Any],
    *,
    cohort_key: str = "",
    cohort_rates: Sequence[float] = (),
    minimum_cohort_size: int = MIN_COHORT_SIZE,
    max_age_days: int = DEFAULT_REVIEW_OBSERVATION_MAX_AGE_DAYS,
    now: datetime | str | None = None,
) -> Dict[str, Any]:
    history = _snapshot_history(record)
    evaluation_now = _as_datetime(now) or datetime.now(timezone.utc)
    observation_at = _latest_snapshot_at(history) or _record_observed_at(record)
    if observation_at is None:
        return ReviewVelocityObservation(
            observed_at="",
            cohort_key=cohort_key,
            cohort_size=len([value for value in cohort_rates if _finite_number(value)]),
            evidence=("Review velocity needs timestamped observations; lifetime review count is not velocity.",),
        ).to_dict()
    observation = _velocity_from_snapshots(history, observation_at)
    if observation is None:
        observation = _velocity_from_review_timestamps(record, observation_at)

    if observation is None:
        return ReviewVelocityObservation(
            observed_at=_iso_timestamp(observation_at),
            expires_at=_iso_timestamp(observation_at + timedelta(days=max_age_days)),
            cohort_key=cohort_key,
            cohort_size=len([value for value in cohort_rates if _finite_number(value)]),
            evidence=("Review velocity needs at least two count snapshots 14 days apart or timestamped reviews.",),
        ).to_dict()

    values = sorted(float(value) for value in cohort_rates if _finite_number(value) and float(value) >= 0)
    rate = observation.get("reviews_per_30d")
    percentile: float | None = None
    band = "unknown"
    state = "present"
    evidence = list(observation.get("evidence") or [])
    anomalies = list(observation.get("anomalies") or [])
    age_days = (evaluation_now - observation_at).total_seconds() / 86400
    stale_observation = age_days < 0 or age_days > max_age_days
    reliably_zero = bool(
        rate == 0
        and (
            (
                observation.get("source") == "snapshot_delta"
                and float(observation.get("period_days") or 0) >= 90
                and observation.get("reviews_added") == 0
            )
            or (
                observation.get("source") == "timestamp_bootstrap"
                and observation.get("reviews_added_90d") == 0
                and observation.get("bound") == "exact"
            )
        )
    )
    if stale_observation:
        state = "unknown"
        anomalies.append("stale_review_observation")
        evidence.append(
            f"Review observation is {age_days:.1f} days old; {max_age_days} days is the configured maximum."
        )
    elif rate is not None and len(values) >= minimum_cohort_size:
        percentile = _percentile(float(rate), values)
        acceleration = observation.get("acceleration")
        if reliably_zero:
            band = "stale"
        elif percentile >= 80 and acceleration is not None and acceleration > 0:
            band = "surging"
        elif percentile >= 50:
            band = "active"
        elif percentile >= 20:
            band = "steady"
        else:
            band = "stale"
    else:
        state = "unknown"
        evidence.append(
            f"Exact niche/metro cohort has {len(values)} usable rates; {minimum_cohort_size} required for a band."
        )

    return ReviewVelocityObservation(
        state=state,
        source=str(observation["source"]),
        observed_at=_iso_timestamp(observation_at),
        expires_at=_iso_timestamp(observation_at + timedelta(days=max_age_days)),
        period_days=observation.get("period_days"),
        count_start=observation.get("count_start"),
        count_end=observation.get("count_end"),
        reviews_added=observation.get("reviews_added"),
        reviews_added_30d=observation.get("reviews_added_30d"),
        reviews_added_90d=observation.get("reviews_added_90d"),
        reviews_per_30d=rate,
        acceleration=observation.get("acceleration"),
        consistency=observation.get("consistency"),
        cohort_key=cohort_key,
        cohort_size=len(values),
        cohort_percentile=percentile,
        velocity_band="unknown" if stale_observation else band,
        score=VELOCITY_SCORE_BY_BAND["unknown" if stale_observation else band],
        bound=str(observation.get("bound") or "unknown"),
        confidence=0.0 if stale_observation else float(observation.get("confidence") or 0.0),
        evidence=tuple(evidence),
        anomalies=tuple(anomalies),
    ).to_dict()


def mine_review_pain(
    record: Mapping[str, Any],
    *,
    now: datetime | str | None = None,
    recency_days: int = 180,
    max_observation_age_days: int = DEFAULT_REVIEW_OBSERVATION_MAX_AGE_DAYS,
) -> Dict[str, Any]:
    reviews = _review_items(record)
    if not reviews:
        return ReviewPainProfile().to_dict()

    evaluation_now = _as_datetime(now) or datetime.now(timezone.utc)
    reference_time = _record_observed_at(record)
    if reference_time is None:
        return ReviewPainProfile(
            anomalies=("missing_review_collection_timestamp",),
        ).to_dict()
    age_days = (evaluation_now - reference_time).total_seconds() / 86400
    expires_at = reference_time + timedelta(days=max_observation_age_days)
    if age_days < 0 or age_days > max_observation_age_days:
        return ReviewPainProfile(
            observed_at=_iso_timestamp(reference_time),
            expires_at=_iso_timestamp(expires_at),
            anomalies=("stale_review_observation",),
        ).to_dict()

    findings: List[ReviewPainFinding] = []
    responded = 0
    for index, review in enumerate(reviews, start=1):
        text = _review_text(review)
        if not text:
            continue
        review_time = _as_datetime(_review_timestamp(review))
        if reference_time is not None:
            if review_time is None:
                continue
            age_days = (reference_time - review_time).total_seconds() / 86400
            if age_days < 0 or age_days > recency_days:
                continue
        if _owner_response(review):
            responded += 1
        review_id = str(review.get("review_id") or review.get("id") or f"review-{index}")
        observed_at = _review_timestamp(review)
        rating = _safe_float(review.get("rating") or review.get("stars"))
        for category, patterns in PAIN_PATTERNS.items():
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
                corroboration = _structural_review_corroboration(record, category)
                verification = verify_review_pain(category, text)
                findings.append(
                    ReviewPainFinding(
                        category=category,
                        review_id=review_id,
                        observed_at=observed_at,
                        rating=rating,
                        excerpt=_excerpt(text),
                        evidence_ref=f"review:{review_id}:{category}",
                        classifier_result=f"review-pain-classifier-v1:{category}",
                        verifier_result=f"{verification.verdict}:{verification.rule_id}",
                        severe=bool(rating is not None and rating <= 2.0),
                        corroborating_evidence_refs=tuple(corroboration),
                    )
                )

    by_category: Dict[str, set[str]] = {}
    for finding in findings:
        if finding.verifier_result.startswith("PASS:"):
            by_category.setdefault(finding.category, set()).add(finding.review_id)
    severe_corroborated = {
        finding.category
        for finding in findings
        if finding.verifier_result.startswith("PASS:")
        and finding.severe
        and finding.corroborating_evidence_refs
    }
    verified = sorted(
        category
        for category, ids in by_category.items()
        if category in OPERATIONAL_CATEGORIES and (len(ids) >= 2 or category in severe_corroborated)
    )
    risks = sorted(category for category in by_category if category in REPUTATION_RISK_CATEGORIES)
    offer_lanes = sorted({OFFER_BY_CATEGORY[category] for category in verified})
    verified_findings = [
        finding for finding in findings if finding.category in set(verified) | set(risks)
    ]
    evidence_refs = tuple(finding.evidence_ref for finding in verified_findings)
    text_review_count = sum(1 for review in reviews if _review_text(review))
    response_rate = round(responded / text_review_count, 3) if text_review_count else None
    return ReviewPainProfile(
        state="present",
        observed_at=_iso_timestamp(reference_time),
        expires_at=_iso_timestamp(expires_at),
        confidence=0.9 if verified or risks else 0.7,
        verified_categories=tuple(verified),
        reputation_risk_categories=tuple(risks),
        offer_lanes=tuple(offer_lanes),
        findings=tuple(verified_findings),
        reviews_scanned=text_review_count,
        owner_response_rate=response_rate,
        evidence_refs=evidence_refs,
    ).to_dict()


def _velocity_from_snapshots(
    history: Sequence[Mapping[str, Any]], now: datetime
) -> Dict[str, Any] | None:
    parsed: List[tuple[datetime, int]] = []
    for item in history:
        timestamp = _as_datetime(item.get("observed_at") or item.get("timestamp"))
        count = _safe_int(item.get("review_count") if "review_count" in item else item.get("reviews"))
        if timestamp is not None and count is not None:
            parsed.append((timestamp, count))
    parsed.sort(key=lambda value: value[0])
    if len(parsed) < 2:
        return None
    end_at, end_count = parsed[-1]
    candidates = [value for value in parsed[:-1] if (end_at - value[0]).total_seconds() >= MIN_SNAPSHOT_SPAN_DAYS * 86400]
    if not candidates:
        return None
    start_at, start_count = candidates[-1]
    period_days = (end_at - start_at).total_seconds() / 86400
    delta = end_count - start_count
    if delta < 0:
        return {
            "source": "snapshot_delta",
            "period_days": round(period_days, 2),
            "count_start": start_count,
            "count_end": end_count,
            "reviews_added": None,
            "reviews_per_30d": None,
            "bound": "unknown",
            "confidence": 0.0,
            "evidence": ["Review count decreased; treating this as a provider correction, not negative momentum."],
            "anomalies": ["negative_review_count_delta"],
        }
    rate = delta * 30.0 / period_days
    recent_rate = _window_rate(parsed, days=30)
    long_rate = _window_rate(parsed, days=90)
    acceleration = None
    if recent_rate is not None and long_rate is not None:
        acceleration = round(recent_rate - long_rate, 3)
    return {
        "source": "snapshot_delta",
        "period_days": round(period_days, 2),
        "count_start": start_count,
        "count_end": end_count,
        "reviews_added": delta,
        "reviews_added_30d": _window_delta(parsed, 30),
        "reviews_added_90d": _window_delta(parsed, 90),
        "reviews_per_30d": round(rate, 3),
        "acceleration": acceleration,
        "consistency": _snapshot_consistency(parsed),
        "bound": "exact",
        "confidence": min(1.0, 0.65 + min(period_days, 90) / 260),
        "evidence": [
            f"Google review count increased by {delta} over {period_days:.1f} days ({rate:.2f} per 30 days)."
        ],
        "anomalies": [],
    }


def _velocity_from_review_timestamps(record: Mapping[str, Any], now: datetime) -> Dict[str, Any] | None:
    reviews = _review_items(record)
    timestamps = [timestamp for item in reviews if (timestamp := _as_datetime(_review_timestamp(item))) is not None]
    if not timestamps:
        return None
    count_30 = sum(1 for timestamp in timestamps if 0 <= (now - timestamp).total_seconds() <= 30 * 86400)
    count_90 = sum(1 for timestamp in timestamps if 0 <= (now - timestamp).total_seconds() <= 90 * 86400)
    oldest = min(timestamps)
    requested_depth = _safe_int(_nested_pick(record, "review_collection", "depth")) or len(reviews)
    total_reviews = _record_review_count(record)
    truncated = total_reviews is not None and total_reviews > len(reviews)
    lower_bound = truncated and (now - oldest).total_seconds() <= 90 * 86400
    prior_60 = max(0, count_90 - count_30)
    prior_month_rate = prior_60 / 2.0
    acceleration = round(count_30 - prior_month_rate, 3)
    monthly_counts = [count_30, sum(1 for value in timestamps if 30 * 86400 < (now - value).total_seconds() <= 60 * 86400), sum(1 for value in timestamps if 60 * 86400 < (now - value).total_seconds() <= 90 * 86400)]
    return {
        "source": "timestamp_bootstrap",
        "period_days": 90.0,
        "count_start": None,
        "count_end": total_reviews,
        "reviews_added": count_90,
        "reviews_added_30d": count_30,
        "reviews_added_90d": count_90,
        "reviews_per_30d": float(count_30),
        "acceleration": acceleration,
        "consistency": _count_consistency(monthly_counts),
        "bound": "lower_bound" if lower_bound else "exact",
        "confidence": 0.75 if lower_bound else 0.85,
        "evidence": [
            f"Timestamped review sample contains {count_30} reviews in 30 days and {count_90} in 90 days."
        ],
        "anomalies": [],
    }


def _structural_review_corroboration(record: Mapping[str, Any], category: str) -> List[str]:
    raw = record.get("review_pain_corroboration")
    if not isinstance(raw, Mapping):
        return []
    value = raw.get(category)
    if not isinstance(value, list):
        return []
    evidence = record.get("evidence") if isinstance(record.get("evidence"), Mapping) else {}
    items = evidence.get("evidence_items") if isinstance(evidence.get("evidence_items"), list) else []
    known_refs = {
        str(item.get("id") or item.get("evidence_id") or "").strip()
        for item in items
        if isinstance(item, Mapping)
    }
    return sorted(
        {
            str(item).strip()
            for item in value
            if str(item).strip() and str(item).strip() in known_refs
        }
    )


def _snapshot_consistency(parsed: Sequence[tuple[datetime, int]]) -> float | None:
    if len(parsed) < 3:
        return None
    rates: List[float] = []
    for (start_at, start_count), (end_at, end_count) in zip(parsed, parsed[1:]):
        days = (end_at - start_at).total_seconds() / 86400
        delta = end_count - start_count
        if days > 0 and delta >= 0:
            rates.append(delta * 30.0 / days)
    return _count_consistency(rates)


def _count_consistency(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    maximum = max(values)
    if maximum <= 0:
        return 1.0
    return round(max(0.0, 1.0 - (max(values) - min(values)) / maximum), 3)


def _snapshot_history(record: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    candidates = [
        record.get("review_count_history"),
        record.get("review_velocity_history"),
        _nested_pick(record, "google", "review_count_history"),
        _nested_pick(record, "review_signals", "count_history"),
    ]
    for value in candidates:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _record_observed_at(record: Mapping[str, Any]) -> datetime | None:
    for value in (
        _nested_pick(record, "review_collection", "observed_at"),
        _nested_pick(record, "review_signals", "observed_at"),
        record.get("observed_at"),
    ):
        parsed = _as_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _latest_snapshot_at(history: Sequence[Mapping[str, Any]]) -> datetime | None:
    timestamps = [
        parsed
        for item in history
        if (parsed := _as_datetime(item.get("observed_at") or item.get("timestamp"))) is not None
    ]
    return max(timestamps) if timestamps else None


def _review_items(record: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    candidates = [
        record.get("google_reviews"),
        record.get("review_items"),
        _nested_pick(record, "review_collection", "items"),
        _nested_pick(record, "reviews_data", "items"),
        _nested_pick(record, "google", "review_items"),
    ]
    for value in candidates:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _window_delta(parsed: Sequence[tuple[datetime, int]], days: int) -> int | None:
    end_at, end_count = parsed[-1]
    candidates = [value for value in parsed[:-1] if 0 < (end_at - value[0]).total_seconds() <= days * 86400]
    if not candidates:
        return None
    delta = end_count - candidates[0][1]
    return delta if delta >= 0 else None


def _window_rate(parsed: Sequence[tuple[datetime, int]], days: int) -> float | None:
    end_at, end_count = parsed[-1]
    candidates = [value for value in parsed[:-1] if (end_at - value[0]).total_seconds() >= MIN_SNAPSHOT_SPAN_DAYS * 86400 and (end_at - value[0]).total_seconds() <= days * 86400]
    if not candidates:
        return None
    start_at, start_count = candidates[0]
    delta = end_count - start_count
    period = (end_at - start_at).total_seconds() / 86400
    if delta < 0 or period <= 0:
        return None
    return delta * 30.0 / period


def _percentile(value: float, values: Sequence[float]) -> float:
    below = sum(1 for candidate in values if candidate < value)
    equal = sum(1 for candidate in values if candidate == value)
    return round(100.0 * (below + 0.5 * equal) / len(values), 2)


def _review_text(review: Mapping[str, Any]) -> str:
    raw = review.get("review_text") or review.get("text") or review.get("value") or ""
    if isinstance(raw, Mapping):
        raw = raw.get("value") or raw.get("text") or ""
    return " ".join(str(raw).split())


def _review_timestamp(review: Mapping[str, Any]) -> str:
    return str(
        review.get("timestamp")
        or review.get("datetime")
        or review.get("time")
        or review.get("review_date")
        or ""
    )


def _owner_response(review: Mapping[str, Any]) -> bool:
    return bool(
        review.get("owner_answer")
        or review.get("owner_response")
        or review.get("response")
    )


def _excerpt(text: str, limit: int = 180) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _record_review_count(record: Mapping[str, Any]) -> int | None:
    google = record.get("google") if isinstance(record.get("google"), Mapping) else {}
    for value in (
        record.get("Reviews"),
        record.get("reviews"),
        record.get("google_review_count"),
        google.get("reviews"),
        google.get("userRatingCount"),
    ):
        parsed = _safe_int(value)
        if parsed is not None:
            return parsed
    return None


def _nested_pick(record: Mapping[str, Any], first: str, second: str) -> Any:
    value = record.get(first)
    return value.get(second) if isinstance(value, Mapping) else None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.isdigit():
            number = float(text)
            if number > 10_000_000_000:
                number /= 1000
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for pattern in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
            else:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_timestamp(value: Any) -> str:
    parsed = _as_datetime(value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return parsed.isoformat().replace("+00:00", "Z")


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _finite_number(value: Any) -> bool:
    return _safe_float(value) is not None
