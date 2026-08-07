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
        if len(self.allowed) == 1:
            return f"{{{self.field}}}='{self.allowed[0]}'"
        return "OR(" + ",".join(f"{{{self.field}}}='{value}'" for value in self.allowed) + ")"


# One clause, because there is now one verdict. The three it replaces were a
# compound guard against the base storing this decision across three
# independently hand-editable columns: `Packet Tier` said sendable while
# `Commercial Fit State` disagreed, and the queue had to check all three to
# notice. `Verdict` is machine-owned and `verdict.evaluate_verdict` already
# folds both other conditions in — `send` cannot be reached unless the lead is
# commercially-sendable and contact-now, since both are blockers inside the same
# admission (pinned by tests/test_verdict.py).
#
# The literal `"send"` is spelled here rather than imported: `verdict` imports
# `sendability`, which imports this module, so an import would cycle. It is
# pinned against `verdict.VERDICT_SEND` by tests/test_queue_state_formula.py —
# the same way QUEUE_STATUS_CLAUSE is pinned against PROSPECT_STATUS_CHOICES
# rather than derived from it.
READINESS_CLAUSES = (
    ReadinessClause("Verdict", "send", "Verdict must be send"),
)

# Cross-checks, not filter clauses — and the distinction is the whole reason
# they survive the collapse. As base filter clauses they were redundant, because
# `send` already implies both. As row-level checks they are not: a scored CSV row
# carries this evidence beside its declared tier, and a row whose declared
# verdict disagrees with its own commercial-fit evidence is precisely the drift
# the dashboard exists to surface. Dropping them entirely would have made
# `sendable_readiness_mismatch` unreachable.
#
# Applied only to fields the row actually carries. A base row after the cutover
# has neither column, and treating absent as failing would empty the send queue
# — the exact failure mode the blank-Packet-Tier rows already demonstrated.
READINESS_CROSS_CHECKS = (
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
# rate was recoverable from the base. On 2026-08-03 the live base consolidated
# Mockup Needed into New, Sent into Follow-Up, and Do Not Contact into Not Fit.
# Opt-out suppression remains independent ledger state; a visible Not Fit value
# is not a substitute for a suppression entry.
PROSPECT_STATUS_CHOICES = (
    "New",
    "Manual Review",
    "Not Fit",
    "Follow-Up",
    "Won",
    "Lost",
    "Booked",
)
PROSPECT_STATUS_CHOICE_SET = frozenset(PROSPECT_STATUS_CHOICES)

# The accept-rate KPI — AGENTS.md: "below ~50% the loop is losing" — asks one
# question of Status: did a human accept what the machine proposed? That is a
# reading of this vocabulary, so it is declared beside it rather than in the eval
# that consumes it; a gate carrying its own copy would keep answering for a
# Status set that had moved underneath it.
#
# Accepted means "taken into outreach", not "won". Contact logging moves a lead
# directly from New to Follow-Up, so Follow-Up is the first durable acceptance
# signal. A lead that later loses was still accepted; conversion remains a
# separate metric.
ACCEPTED_STATUSES = frozenset({"Follow-Up", "Won", "Lost", "Booked"})
REJECTED_STATUSES = frozenset({"Not Fit"})
UNDECIDED_STATUSES = frozenset({"New", "Manual Review"})
# Pinned as a partition by tests/test_queue_state_formula.py: a new Status must
# be classified as accept, reject or undecided, because the alternative is a KPI
# that silently stops counting the cases it cannot place.

# An allowlist, never a "not in terminal statuses" denylist: a denylist silently
# widens the morning queue the moment a status is added, and the Won/Lost split
# is exactly such an addition. Pinned against PROSPECT_STATUS_CHOICES by
# tests/test_queue_state_formula.py so a vocabulary edit that renames or drops a
# queued status fails there rather than quietly emptying "Ready to Dial".
QUEUE_STATUS_CLAUSE = QueueStatusClause(
    "Status",
    ("New",),
    "Status must be New",
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
