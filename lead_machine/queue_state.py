"""Airtable Queue State formula CLI."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ReadinessClause:
    field: str
    expected: str
    failure: str

    @property
    def airtable_condition(self) -> str:
        return f"{{{self.field}}}='{self.expected}'"


READINESS_CLAUSES = (
    ReadinessClause("Packet Tier", "Sendable Packet", "Sendable must be Yes"),
    ReadinessClause(
        "Commercial Fit State",
        "commercially-sendable",
        "Commercial Fit State must be commercially-sendable",
    ),
    ReadinessClause(
        "Contact Eligibility",
        "contact-now",
        "Contact Eligibility must be contact-now",
    ),
)

READINESS_FILTER_CLAUSES = ",".join(
    clause.airtable_condition for clause in READINESS_CLAUSES
)
READINESS_FILTER_FORMULA = f"AND({READINESS_FILTER_CLAUSES})"


def queue_state_formula() -> str:
    expression = '"Ready to Dial"'
    for clause in reversed(READINESS_CLAUSES):
        expression = f'IF({clause.airtable_condition},{expression},"Blocked: {clause.failure}")'
    return expression


def queue_state_for_row(row: Mapping[str, Any]) -> str:
    """Evaluate the generated Airtable formula against a base-shaped row."""
    for clause in READINESS_CLAUSES:
        if str(row.get(clause.field) or "").strip() != clause.expected:
            return f"Blocked: {clause.failure}"
    return "Ready to Dial"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula", action="store_true", help="Print the Queue State Airtable formula")
    args = parser.parse_args(argv)
    if not args.formula:
        parser.error("--formula is required")
    print(queue_state_formula())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
