"""Append-only ledger of the human decision made *before* a dial.

Why this exists. `evals/accept_rate.py` reads `Prospects.Status`, and
`queue_state.ACCEPTED_STATUSES` counts `Follow-Up` as accepted — but
`mark_sent.py` sets `Follow-Up` as the act of logging a completed contact. So
the workflow manufactures its own approval: a rep who dials whatever the queue
serves produces a 100% accept rate, and the KPI reads green whether the leads
were good or worthless. That was tolerable while the commercial tribunal was
refusing 71% of the corpus upstream. With the tribunal demoted to a ranking,
accept-rate became the only proposed check on whether the strip cost us
anything, and it cannot answer that question as built.

The missing event is the refusal that never reaches Airtable: the rep reads a
lead, decides not to call it, and moves on. Nothing recorded it, so the
denominator only ever contained leads someone had already agreed to dial.

This ledger records both sides at the moment of judgement, with a reason and a
timestamp, and carries the rank the lead was served at. That last field is what
makes the ranking falsifiable: if a high rank does not predict approval, the
weights in `dial_rank` are wrong, and nothing else in the repo can tell us.

Deliberately a local JSONL file rather than an Airtable column: a decline is
not a lead state, it is an observation about a lead, and the base has no field
for one. Adding a column would need a schema runbook step and would still
conflate a pre-dial refusal with the post-dial `Not Fit` that `mark_outcome`
writes for an opt-out. Append-only for the reason `corpus_index.PlaceRegistry`
is: a decision already made is evidence, and no later run may erase it.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DECISION_DIAL = "dial"
DECISION_DECLINE = "decline"
DECISIONS = frozenset({DECISION_DIAL, DECISION_DECLINE})

DIAL_DECISIONS_ENV = "LPM_DIAL_DECISIONS_JSONL"


class DialDecisionError(RuntimeError):
    """A decision that could not be recorded as stated."""


def configured_decisions_path() -> str:
    """The ledger path the environment names, unresolved and unvalidated.

    Shared by every reader and writer so a run cannot record decisions into one
    file and compute the rate from another.
    """
    return os.getenv(DIAL_DECISIONS_ENV, "").strip()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_decision(
    ledger: Path | str,
    *,
    decision: str,
    record_id: Any,
    reason: Any = "",
    decided_at: Any = "",
    rank: Optional[float] = None,
    business_name: Any = "",
    vertical: Any = "",
    decided_by: Any = "",
) -> Dict[str, Any]:
    """Append one pre-dial decision and return the row written.

    Raises rather than writing a row that cannot be read back as a judgement:
    an unknown verb, a decision naming no lead, or a refusal with no reason.
    """
    verb = _text(decision).casefold()
    if verb not in DECISIONS:
        allowed = ", ".join(sorted(DECISIONS))
        raise DialDecisionError(f"decision {decision!r} is not one of: {allowed}")

    identity = _text(record_id)
    if not identity:
        raise DialDecisionError(
            "a decision must name the record it is about, or it can never be "
            "joined to the rank that produced it"
        )

    why = _text(reason)
    if verb == DECISION_DECLINE and not why:
        raise DialDecisionError(
            "a decline must carry a reason: without one the ledger records that "
            "a lead was refused but not which signal was wrong, which is the "
            "only thing that can retire or restore a demoted blocker"
        )

    row: Dict[str, Any] = {
        "decision": verb,
        "record_id": identity,
        "reason": why,
        "decided_at": _text(decided_at) or _now(),
        "rank": rank,
        "business_name": _text(business_name),
        "vertical": _text(vertical),
        "decided_by": _text(decided_by),
    }

    path = Path(ledger)
    line = json.dumps(row, ensure_ascii=False)
    # The mkdir belongs inside the guard, not above it. `mark_sent` records the
    # dial after the Airtable mutation is confirmed and catches DialDecisionError
    # in order to warn rather than fail; a bare OSError from mkdir escapes that
    # catch and tracebacks the script after a real state change, which reads as
    # "that did not work" and invites the rep to run it again. Every way this
    # write can fail has to arrive as one type.
    try:
        parent = path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        # One `write` of one short line under O_APPEND, so two reps working the
        # queue at once cannot interleave inside a record.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        raise DialDecisionError(f"dial-decision ledger {path} is not writable: {exc}") from exc
    return row


def load_decisions(ledger: Path | str) -> List[Dict[str, Any]]:
    """Every decision row in *ledger*, oldest first.

    A missing file reads as no decisions — the rep has not started, which is
    different from a rate of zero. A corrupt line is skipped rather than
    refused: one unreadable row costs one decision, where refusing the file
    costs every decision ever made.
    """
    path = Path(ledger)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if _text(data.get("decision")).casefold() not in DECISIONS:
                continue
            if not _text(data.get("record_id")):
                continue
            rows.append(data)
    return rows


def approval_rate(decisions: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Pre-dial approval over *decisions*, folded to one verdict per lead.

    Folded because a lead reconsidered is still one lead: counting each visit
    would make the rate drift with how often the rep revisits rather than with
    how good the leads are. The last decision recorded for a record wins, which
    is what "reconsidered" means.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for row in decisions:
        identity = _text(row.get("record_id"))
        if identity:
            latest[identity] = row

    dialed = 0
    declined = 0
    reasons: Counter[str] = Counter()
    ranks: Dict[str, List[float]] = {DECISION_DIAL: [], DECISION_DECLINE: []}
    for row in latest.values():
        verb = _text(row.get("decision")).casefold()
        if verb == DECISION_DIAL:
            dialed += 1
        elif verb == DECISION_DECLINE:
            declined += 1
            reasons[_text(row.get("reason"))] += 1
        rank = row.get("rank")
        # `verb not in ranks` is unreachable through `load_decisions`, which
        # filters — but this function is public and a direct caller passing a
        # hand-built row should not get a KeyError out of a metrics helper.
        if verb in ranks and isinstance(rank, (int, float)) and not isinstance(rank, bool):
            ranks[verb].append(float(rank))

    decided = dialed + declined
    return {
        "decided": decided,
        "dialed": dialed,
        "declined": declined,
        # How many decisions on each side actually carried a rank. The two sides
        # are recorded by different commands and one automated path
        # (`hermes_queue`) invokes mark_sent without `--rank`, so these can
        # diverge badly — and a mean-vs-mean comparison across unequal coverage
        # compares two different populations while looking like one number.
        "ranked_dialed": len(ranks[DECISION_DIAL]),
        "ranked_declined": len(ranks[DECISION_DECLINE]),
        # None, never 0.0: an empty ledger means nobody has judged anything, and
        # reporting that as a failing rate would raise an alarm about the leads
        # when the truth is that the rep has not started.
        "rate": (dialed / decided) if decided else None,
        "decline_reasons": dict(reasons),
        "mean_rank_dialed": (sum(ranks[DECISION_DIAL]) / len(ranks[DECISION_DIAL]))
        if ranks[DECISION_DIAL]
        else None,
        "mean_rank_declined": (sum(ranks[DECISION_DECLINE]) / len(ranks[DECISION_DECLINE]))
        if ranks[DECISION_DECLINE]
        else None,
    }
