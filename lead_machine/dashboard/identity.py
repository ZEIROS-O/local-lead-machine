from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal

from lead_machine.schema import normalize_domain, normalize_phone


def _first(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    google = row.get("google")
    return google if isinstance(google, dict) else row


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _place_key(row: dict[str, Any]) -> str:
    return _first(_flat(row), "Place Key", "place_key")


def _place_id(row: dict[str, Any]) -> str:
    return _first(
        _flat(row),
        "Google Place ID",
        "Place ID",
        "google_place_id",
        "place_id",
    )


def _phone(row: dict[str, Any]) -> str:
    return normalize_phone(_first(_flat(row), "Phone", "phone"))


def _domain(row: dict[str, Any]) -> str:
    return normalize_domain(
        _first(_flat(row), "Website URL", "website_url", "website")
    )


def _name_city(row: dict[str, Any]) -> str:
    flat = _flat(row)
    name = _normalized_text(_first(flat, "Business Name", "business_name", "name"))
    city = _normalized_text(_first(flat, "City", "city"))
    return f"{name}|{city}" if name and city else ""


IDENTITY_STEPS: tuple[tuple[str, Callable[[dict[str, Any]], str]], ...] = (
    ("place_key", _place_key),
    ("place_id", _place_id),
    ("phone", _phone),
    ("domain", _domain),
    ("name_city", _name_city),
)


@dataclass(frozen=True)
class Reconciliation:
    status: Literal["reconciled", "unknown"]
    matched: int
    unmatched_enriched: int
    unmatched_scored: int
    ambiguous: int
    scored_by_enriched_index: dict[int, int]

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "status": self.status,
            "matched": self.matched,
            "unmatched_enriched": (
                self.unmatched_enriched if self.status == "reconciled" else None
            ),
            "unmatched_scored": self.unmatched_scored,
            "ambiguous": self.ambiguous,
        }


def reconcile_rows(
    enriched_rows: Iterable[dict[str, Any]],
    scored_rows: Iterable[dict[str, Any]],
) -> Reconciliation:
    enriched = list(enriched_rows)
    scored = list(scored_rows)
    unmatched_enriched = set(range(len(enriched)))
    unmatched_scored = set(range(len(scored)))
    pairs: dict[int, int] = {}
    ambiguous_rows: set[tuple[str, int]] = set()

    for _label, extractor in IDENTITY_STEPS:
        left: dict[str, list[int]] = defaultdict(list)
        right: dict[str, list[int]] = defaultdict(list)
        for index in unmatched_enriched:
            if token := extractor(enriched[index]):
                left[token].append(index)
        for index in unmatched_scored:
            if token := extractor(scored[index]):
                right[token].append(index)

        for token in sorted(set(left) & set(right)):
            if len(left[token]) == 1 and len(right[token]) == 1:
                enriched_index = left[token][0]
                scored_index = right[token][0]
                pairs[enriched_index] = scored_index
                unmatched_enriched.remove(enriched_index)
                unmatched_scored.remove(scored_index)
            else:
                ambiguous_rows.update(
                    ("enriched", index) for index in left[token]
                )
                ambiguous_rows.update(("scored", index) for index in right[token])

    unresolved_ambiguity = sum(
        1
        for side, index in ambiguous_rows
        if index in (
            unmatched_enriched if side == "enriched" else unmatched_scored
        )
    )
    status: Literal["reconciled", "unknown"] = (
        "reconciled"
        if not unmatched_scored and not unresolved_ambiguity
        else "unknown"
    )
    return Reconciliation(
        status=status,
        matched=len(pairs),
        unmatched_enriched=len(unmatched_enriched),
        unmatched_scored=len(unmatched_scored),
        ambiguous=unresolved_ambiguity,
        scored_by_enriched_index=pairs,
    )
