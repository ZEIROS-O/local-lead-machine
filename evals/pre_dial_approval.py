#!/usr/bin/env python3
"""Compute pre-dial approval — the KPI accept-rate cannot be.

`evals/accept_rate.py` reads `Prospects.Status` and counts `Follow-Up` as
accepted, while `mark_sent.py` sets `Follow-Up` as the act of logging a
completed contact. The workflow therefore manufactures its own approval: a rep
who dials whatever the queue serves scores 100% regardless of lead quality.
That was survivable while the commercial tribunal refused 71% of the corpus
before a human ever saw it. Once the tribunal became a ranking, accept-rate was
the only proposed check on whether the strip cost anything — and it was
measuring the wrong event.

This reads the decision made *before* the dial, from the ledger
`mark_sent.py` and `mark_declined.py` write:

    pre-dial approval = dialled / (dialled + declined)

Folded to one decision per lead, so revisits do not move the rate.

It never prints PASS on a thin sample. Twenty decisions is a smoke test — at
10/20 the 95% interval spans roughly 30–70% — so it can expose a large failure
and cannot calibrate a weight. `--minimum-decided` is the knob; the default
matches accept-rate's so the two KPIs cannot disagree about what "enough" is.

The rank comparison at the bottom is the ranking's own falsifier. If the leads
the rep approves are not ranked meaningfully higher than the ones they refuse,
the weights in `lead_machine/dial_rank.py` are not ordering anything, and no
amount of accept-rate will say so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lead_machine.dial_decisions import (  # noqa: E402
    DIAL_DECISIONS_ENV,
    approval_rate,
    configured_decisions_path,
    load_decisions,
)

# The same floor AGENTS.md names for accept-rate. Reused rather than reinvented:
# two different floors for "is the loop losing" would just mean neither is the
# answer. It is a starting line, not a validated threshold.
APPROVAL_FLOOR = 0.5
MINIMUM_DECIDED = 20
# The margin by which an approved lead should out-rank a refused one before the
# ordering has demonstrated anything. Below this the rank is noise even if the
# approval rate looks healthy.
RANK_SEPARATION_TARGET = 10.0


def evaluate_pre_dial_approval(
    decisions: Any,
    *,
    floor: float = APPROVAL_FLOOR,
    minimum_decided: int = MINIMUM_DECIDED,
    rank_separation_target: float = RANK_SEPARATION_TARGET,
) -> dict[str, Any]:
    summary = approval_rate(decisions)
    decided = summary["decided"]
    rate = summary["rate"]

    if decided == 0:
        status = "no-decisions"
        line = (
            "- NO VERDICT: pre-dial approval — no decisions recorded. Either the "
            "rep has not started or no ledger is configured "
            f"(${DIAL_DECISIONS_ENV}); a rate cannot be read from an empty file."
        )
    elif decided < minimum_decided:
        status = "insufficient-sample"
        line = (
            f"- NO VERDICT: pre-dial approval — only {decided} decision(s); "
            f"{minimum_decided} needed to judge"
        )
    elif rate is not None and rate < floor:
        status = "below-floor"
        line = (
            f"- FAIL: pre-dial approval {rate:.1%} is below the {floor:.0%} floor "
            f"({summary['dialed']} dialled / {decided} decided)"
        )
    else:
        status = "pass"
        line = (
            f"- PASS: pre-dial approval {rate:.1%} "
            f"({summary['dialed']} dialled / {decided} decided)"
        )

    dialed_rank = summary["mean_rank_dialed"]
    declined_rank = summary["mean_rank_declined"]
    separation = (
        dialed_rank - declined_rank
        if dialed_rank is not None and declined_rank is not None
        else None
    )
    if separation is None:
        rank_line = (
            "- rank check: not enough ranked decisions on both sides to compare"
        )
        rank_ok = None
    elif separation < rank_separation_target:
        rank_ok = False
        rank_line = (
            f"- RANK WARNING: approved leads out-rank refused ones by only "
            f"{separation:.1f} points (target {rank_separation_target:.0f}). The "
            "dial_rank weights are not ordering what the rep cares about."
        )
    else:
        rank_ok = True
        rank_line = (
            f"- rank check: approved leads out-rank refused ones by "
            f"{separation:.1f} points"
        )

    return {
        # A thin sample is not a failure, it is an absence of evidence. Only a
        # measured rate below the floor fails.
        "ok": status in {"pass", "no-decisions", "insufficient-sample"},
        "status": status,
        "summary": line,
        "rank_summary": rank_line,
        "approval_rate": rate,
        "floor": floor,
        "minimum_decided": minimum_decided,
        "rank_separation": separation,
        "rank_separation_target": rank_separation_target,
        "rank_ok": rank_ok,
        "counts": {
            "decided": decided,
            "dialed": summary["dialed"],
            "declined": summary["declined"],
        },
        "mean_rank_dialed": dialed_rank,
        "mean_rank_declined": declined_rank,
        "decline_reasons": summary["decline_reasons"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--decisions-source",
        default="",
        help=f"Decision ledger JSONL; defaults to ${DIAL_DECISIONS_ENV}",
    )
    parser.add_argument("--floor", type=float, default=APPROVAL_FLOOR)
    parser.add_argument("--minimum-decided", type=int, default=MINIMUM_DECIDED)
    parser.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    args = parser.parse_args()

    source = args.decisions_source.strip() or configured_decisions_path()
    if not source:
        print(
            "[ERROR] no decision ledger configured "
            f"(${DIAL_DECISIONS_ENV} or --decisions-source)",
            file=sys.stderr,
        )
        return 1

    result = evaluate_pre_dial_approval(
        load_decisions(source),
        floor=args.floor,
        minimum_decided=args.minimum_decided,
    )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["summary"])
        print(result["rank_summary"])
        if result["decline_reasons"]:
            print("- refusal reasons:")
            for reason, count in sorted(
                result["decline_reasons"].items(), key=lambda item: (-item[1], item[0])
            ):
                print(f"    {count:>4}  {reason}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
