from __future__ import annotations

from typing import Any, Dict, Iterable, List


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float:
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compute_feedback_metrics(rows: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    records = list(rows)
    a_rows = [row for row in records if _clean(row.get("AI Grade") or row.get("Lead Priority")) == "A"]
    approved = []
    minutes: List[float] = []

    for row in a_rows:
        lou = _clean(row.get("Lou Grade"))
        partner = _clean(row.get("Partner Grade"))
        if lou in {"A", "B", "Outreach-worthy"} or partner in {"A", "B", "Outreach-worthy"}:
            approved.append(row)
        if _clean(row.get("Human Review Minutes")):
            minutes.append(_float(row.get("Human Review Minutes")))

    sampled_skips = [
        row
        for row in records
        if _clean(row.get("AI Grade") or row.get("Lead Priority")) == "Skip"
        and _clean(row.get("Sampled Skip Verdict"))
    ]
    false_skips = [row for row in sampled_skips if _clean(row.get("Sampled Skip Verdict")).lower() == "false skip"]

    return {
        "total_records": float(len(records)),
        "a_leads": float(len(a_rows)),
        "a_lead_human_approval_rate": len(approved) / len(a_rows) if a_rows else 0.0,
        "average_a_review_minutes": sum(minutes) / len(minutes) if minutes else 0.0,
        "sampled_skips": float(len(sampled_skips)),
        "sampled_false_skip_rate": len(false_skips) / len(sampled_skips) if sampled_skips else 0.0,
    }

