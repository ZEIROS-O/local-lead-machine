"""One machine-owned verdict, projected from the existing authority.

Nine Airtable surfaces answered one question — "should we contact this lead" —
and every reader had to reassemble the answer from fifteen single-selects.
`sendability` was already the one authority; it just published through too many
columns.

So this is a projection, not a second gate. `evaluate_verdict` calls
`evaluate_sendability` + `evaluate_packet_tier` and renames the result. There is
no rule here that is not already there, which is what makes "the collapse
changed no decision" provable by `evals/reproducibility.py` rather than argued:
`TIER_VERDICTS` is a bijection on the packet tiers, so the recomputed verdict
set is the recomputed tier set under another name.

The vocabulary itself lives in `sendability` beside the tiers it renames — one
declaration, and `readiness_flags` needs the inverse without importing the
module that imports it. This module owns the evaluation API.

Ownership: `Verdict` is machine-written and never hand-edited. `Status` is
human-owned and never machine-written except by `mark_sent` / `mark_outcome`.
Strict ownership on both sides is what stops the two drifting into a second
disagreement of the kind this collapse exists to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from .sendability import (
    TIER_VERDICTS,
    VERDICT_CHOICES,
    VERDICT_REJECT,
    VERDICT_REVIEW,
    VERDICT_SEND,
    VERDICT_WATCH,
    evaluate_packet_tier,
    evaluate_sendability,
)

__all__ = [
    "TIER_VERDICTS",
    "VERDICT_CHOICES",
    "VERDICT_FIELD",
    "VERDICT_REASONS_FIELD",
    "VERDICT_REJECT",
    "VERDICT_REVIEW",
    "VERDICT_SEND",
    "VERDICT_WATCH",
    "VerdictResult",
    "apply_verdict",
    "evaluate_verdict",
    "verdict_for_tier",
]

VERDICT_FIELD = "Verdict"
VERDICT_REASONS_FIELD = "Verdict Reasons"


@dataclass(frozen=True)
class VerdictResult:
    """The one verdict and the reasons behind it.

    `reasons` is the existing blocker list. `Send Blockers` and `Sendability
    Blockers` were already byte-identical two columns; this is where they become
    one.
    """

    verdict: str
    reasons: tuple[str, ...]

    @property
    def is_send(self) -> bool:
        return self.verdict == VERDICT_SEND


def verdict_for_tier(tier: str) -> str:
    """Rename a packet tier to its verdict.

    Unknown tiers raise. A default would let a tier the mapping has never heard
    of land quietly in a bucket, and this function exists precisely so that
    cannot happen silently.
    """
    try:
        return TIER_VERDICTS[tier]
    except KeyError:
        raise ValueError(f"no verdict declared for packet tier {tier!r}") from None


def evaluate_verdict(row: Dict[str, Any]) -> VerdictResult:
    """The verdict for *row*, projected from the sendability authority."""
    result = evaluate_sendability(row)
    tier, _manual_review_reason = evaluate_packet_tier(row, result)
    return VerdictResult(verdict_for_tier(tier), tuple(result["send_blockers"]))


def apply_verdict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return *row* carrying its verdict and the reasons for it."""
    updated = dict(row)
    result = evaluate_verdict(updated)
    updated[VERDICT_FIELD] = result.verdict
    updated[VERDICT_REASONS_FIELD] = "; ".join(result.reasons)
    return updated
