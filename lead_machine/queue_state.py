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


@dataclass(frozen=True)
class QueueStatusClause:
    field: str
    allowed: tuple[str, ...]
    failure: str

    @property
    def airtable_condition(self) -> str:
        return "OR(" + ",".join(f"{{{self.field}}}='{value}'" for value in self.allowed) + ")"


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
# The whole `Prospects.Status` vocabulary, declared here because this module
# already owns the only clause that reads Status and is imported by the send
# path. Everything else that names a Status is a narrower view of this tuple —
# the queue gate below, `compliance.MARK_SENT_ALLOWED_SOURCE_STATUSES`,
# `mark_outcome.REQUIRED_SELECT_CHOICES` — and none of those views can notice
# the field changing underneath them on their own.
#
# `Closed` was split into `Won`/`Lost` (2026-07-29). One closed state cannot say
# which, so a won deal and a dead lead landed in the same cell and no conversion
# rate was recoverable from the base. The base still offers `Closed` and neither
# new choice: code leads, the Airtable runbook follows, and a write of `Won`
# 422s until a human has created it. That ordering is deliberate — removing the
# choice before the code stopped writing it would 422 every upload instead.
PROSPECT_STATUS_CHOICES = (
    "New",
    "Mockup Needed",
    "Manual Review",
    "Not Fit",
    "Sent",
    "Follow-Up",
    "Won",
    "Lost",
)

# An allowlist, never a "not in {Sent, Closed}" denylist: a denylist silently
# widens the morning queue the moment a status is added, and the Won/Lost split
# is exactly such an addition. Pinned against PROSPECT_STATUS_CHOICES by
# tests/test_queue_state_formula.py so a vocabulary edit that renames or drops a
# queued status fails there rather than quietly emptying "Ready to Dial".
QUEUE_STATUS_CLAUSE = QueueStatusClause(
    "Status",
    ("New", "Mockup Needed"),
    "Status must be New or Mockup Needed",
)

READINESS_FILTER_CLAUSES = ",".join(
    clause.airtable_condition for clause in READINESS_CLAUSES
)
READINESS_FILTER_FORMULA = f"AND({READINESS_FILTER_CLAUSES})"
QUEUE_FILTER_CLAUSES = f"{READINESS_FILTER_CLAUSES},{QUEUE_STATUS_CLAUSE.airtable_condition}"
QUEUE_FILTER_FORMULA = f"AND({QUEUE_FILTER_CLAUSES})"


def queue_state_formula() -> str:
    expression = '"Ready to Dial"'
    for clause in reversed((*READINESS_CLAUSES, QUEUE_STATUS_CLAUSE)):
        expression = f'IF({clause.airtable_condition},{expression},"Blocked: {clause.failure}")'
    return expression


def queue_state_for_row(row: Mapping[str, Any]) -> str:
    """Evaluate the generated Airtable formula against a base-shaped row."""
    for clause in READINESS_CLAUSES:
        if str(row.get(clause.field) or "").strip() != clause.expected:
            return f"Blocked: {clause.failure}"
    if str(row.get(QUEUE_STATUS_CLAUSE.field) or "").strip() not in QUEUE_STATUS_CLAUSE.allowed:
        return f"Blocked: {QUEUE_STATUS_CLAUSE.failure}"
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
