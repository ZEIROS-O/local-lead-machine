from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Sequence


PILOT_CELLS = (
    "detailing__cleveland",
    "detailing__columbus",
    "water_restoration__cleveland",
    "water_restoration__columbus",
)
DISPOSITIONS = {"auto-eligible", "manual-review", "reject"}


def build_blind_sample(
    rows: Sequence[Mapping[str, Any]],
    *,
    per_cell: int = 40,
    audit_fraction: float = 0.25,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    grouped: Dict[str, Dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        cell = _cell(row)
        key = _lead_key(row)
        if cell in PILOT_CELLS and key:
            grouped[cell].setdefault(key, row)
    primary: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []
    for cell in PILOT_CELLS:
        candidates = sorted(grouped[cell].values(), key=_sample_hash)
        if len(candidates) < per_cell:
            raise ValueError(f"Cell {cell} has {len(candidates)} unique leads; {per_cell} required")
        selected = candidates[:per_cell]
        tune_count = round(per_cell * 0.75)
        primary.extend(
            _blind_row(
                row,
                cell=cell,
                audit=False,
                split="tune" if index < tune_count else "holdout",
            )
            for index, row in enumerate(selected)
        )
        audit_count = round(per_cell * audit_fraction)
        audit.extend(
            _blind_row(row, cell=cell, audit=True, split="audit")
            for row in selected[:audit_count]
        )
    return primary, audit


def evaluate_calibration(
    rows: Sequence[Mapping[str, Any]],
    *,
    audit_pairs: Iterable[tuple[str, str]] | None = None,
) -> Dict[str, Any]:
    normalized: List[Dict[str, str]] = []
    for row in rows:
        predicted = str(row.get("Scoring V3 Disposition") or row.get("predicted_disposition") or "").strip().lower()
        human = str(row.get("Human Disposition") or row.get("human_disposition") or "").strip().lower()
        if predicted not in DISPOSITIONS or human not in DISPOSITIONS:
            continue
        split = str(row.get("Calibration Split") or row.get("calibration_split") or "all").strip().lower()
        normalized.append(
            {
                "cell": _cell(row),
                "predicted": predicted,
                "human": human,
                "split": split,
                "offer_lane": str(row.get("Scoring V3 Offer Lane") or row.get("offer_lane") or "unknown"),
                "velocity_band": str(row.get("Review Velocity Band") or row.get("review_velocity_band") or "unknown"),
                "evidence_confidence": str(row.get("Score V3 Evidence Confidence") or row.get("evidence_confidence") or "unknown"),
                "data_source": str(row.get("Review Velocity Source") or row.get("data_source") or "unknown"),
            }
        )
    evaluation_rows = [value for value in normalized if value["split"] == "holdout"] or normalized
    aggregate = _metrics(evaluation_rows)
    by_cell = {
        cell: _metrics([value for value in evaluation_rows if value["cell"] == cell])
        for cell in PILOT_CELLS
    }
    kappa = cohens_kappa(audit_pairs or ())
    kappa_passed = kappa is not None and kappa >= 0.70
    metric_gates_passed = bool(
        aggregate["auto_eligible_approval_rate"] >= 0.90
        and aggregate["false_reject_rate"] <= 0.05
        and all(
            metrics["auto_eligible_approval_rate"] >= 0.85
            and metrics["false_reject_rate"] <= 0.10
            for metrics in by_cell.values()
        )
    )
    launch_passed = metric_gates_passed and kappa_passed
    return {
        "rows_evaluated": len(normalized),
        "evaluation_split": "holdout" if evaluation_rows is not normalized else "all",
        "aggregate_all_labeled": _metrics(normalized),
        "aggregate": aggregate,
        "by_cell": by_cell,
        "by_offer_lane": _breakdown(evaluation_rows, "offer_lane"),
        "by_review_velocity_band": _breakdown(evaluation_rows, "velocity_band"),
        "by_evidence_confidence": _breakdown(evaluation_rows, "evidence_confidence"),
        "by_data_source": _breakdown(evaluation_rows, "data_source"),
        "audit_cohens_kappa": kappa,
        "audit_kappa_passed": kappa_passed,
        "metric_gates_passed": metric_gates_passed,
        "launch_passed": launch_passed,
        "launch_requirements": {
            "aggregate_auto_eligible_approval_rate_min": 0.90,
            "aggregate_false_reject_rate_max": 0.05,
            "per_cell_auto_eligible_approval_rate_min": 0.85,
            "per_cell_false_reject_rate_max": 0.10,
            "audit_cohens_kappa_min": 0.70,
        },
    }


def cohens_kappa(pairs: Iterable[tuple[str, str]]) -> float | None:
    values = [(left, right) for left, right in pairs if left in DISPOSITIONS and right in DISPOSITIONS]
    if not values:
        return None
    observed = sum(1 for left, right in values if left == right) / len(values)
    left_rates = {label: sum(1 for left, _ in values if left == label) / len(values) for label in DISPOSITIONS}
    right_rates = {label: sum(1 for _, right in values if right == label) / len(values) for label in DISPOSITIONS}
    expected = sum(left_rates[label] * right_rates[label] for label in DISPOSITIONS)
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 4)


def attach_predictions(
    labeled_rows: Sequence[Mapping[str, Any]],
    prediction_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    predictions = {_lead_key(row): row for row in prediction_rows if _lead_key(row)}
    merged: List[Dict[str, Any]] = []
    missing: List[str] = []
    for row in labeled_rows:
        key = _lead_key(row) or str(row.get("Calibration Lead Key") or "").strip()
        prediction = predictions.get(key)
        if prediction is None:
            missing.append(key or "<missing-key>")
            continue
        updated = dict(row)
        updated["Scoring V3 Disposition"] = prediction.get("Scoring V3 Disposition", "")
        updated["Scoring Profile ID"] = prediction.get("Scoring Profile ID", "")
        for field in (
            "Scoring V3 Offer Lane",
            "Review Velocity Band",
            "Score V3 Evidence Confidence",
            "Review Velocity Source",
        ):
            updated[field] = prediction.get(field, "")
        merged.append(updated)
    if missing:
        raise ValueError(f"Missing predictions for {len(missing)} labeled leads; first={missing[0]}")
    return merged


def _metrics(rows: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    auto = [value for value in rows if value["predicted"] == "auto-eligible"]
    rejects = [value for value in rows if value["predicted"] == "reject"]
    approval = sum(1 for value in auto if value["human"] == "auto-eligible") / len(auto) if auto else 0.0
    false_reject = sum(1 for value in rejects if value["human"] != "reject") / len(rejects) if rejects else 0.0
    return {
        "rows": len(rows),
        "predicted_auto_eligible": len(auto),
        "predicted_reject": len(rejects),
        "auto_eligible_approval_rate": round(approval, 4),
        "false_reject_rate": round(false_reject, 4),
    }


def _breakdown(rows: Sequence[Mapping[str, str]], field: str) -> Dict[str, Dict[str, Any]]:
    values = sorted({str(row.get(field) or "unknown") for row in rows})
    return {
        value: _metrics([row for row in rows if str(row.get(field) or "unknown") == value])
        for value in values
    }


def _blind_row(row: Mapping[str, Any], *, cell: str, audit: bool, split: str) -> Dict[str, Any]:
    blocked_prefixes = ("score", "scoring", "sendable", "packet", "lead priority", "fit ceiling")
    value = {
        str(key): item
        for key, item in row.items()
        if not str(key).strip().lower().startswith(blocked_prefixes)
    }
    value.update(
        {
            "Calibration Cell": cell,
            "Calibration Lead Key": _lead_key(row),
            "Audit Repeat": "Yes" if audit else "No",
            "Calibration Split": split,
            "Human Disposition": "",
            "Human ICP Fit": "",
            "Human Commercial Materiality": "",
            "Human Named Leak": "",
            "Human Offer Lane": "",
            "Human Reputation Risk": "",
            "Human Notes": "",
        }
    )
    return value


def _cell(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("Scoring Profile ID") or row.get("Calibration Cell") or row.get("cell") or "").strip()
    if explicit:
        return explicit
    vertical = str(row.get("Vertical") or row.get("vertical") or "").strip().lower().replace(" ", "_")
    market = str(row.get("Metro Context") or row.get("market") or "").strip().lower().replace("-", "_").replace(" ", "_")
    return f"{vertical}__{market}"


def _lead_key(row: Mapping[str, Any]) -> str:
    return str(
        row.get("Place Key")
        or row.get("place_id")
        or row.get("Business Name")
        or row.get("business_name")
        or ""
    ).strip()


def _sample_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_lead_key(row).encode("utf-8")).hexdigest()
