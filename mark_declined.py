#!/usr/bin/env python3
"""Record that a rep read a served lead and refused to dial it.

The missing half of the loop. `mark_sent.py` records the dials; nothing
recorded the refusals, so the accept-rate KPI only ever saw leads someone had
already agreed to call. `evals/accept_rate.py` counts `Follow-Up` as accepted
and `mark_sent` sets `Follow-Up`, which means a rep working the queue top to
bottom produces a 100% accept rate no matter how bad the leads are. With the
commercial tribunal demoted to a ranking, that KPI was the only proposed check
on whether the strip cost us anything, and it could not answer.

A decline is pre-dial by definition, so this refuses any record that is not
still `New`: once a contact is logged the lead has been accepted, and what
happens next is an outcome for `mark_outcome.py`, not a judgement of the lead.

Two writes, in this order:

  1. the decision ledger, which is the measurement and the only new fact;
  2. Airtable `Status` -> `Not Fit`, so the lead leaves the rep's queue.

The ledger first because it is the durable half. If the Airtable write then
fails the lead is still served tomorrow — visible, re-decidable, and honest —
whereas a base updated with no ledger row would drop the lead out of the queue
and leave no trace of why, which is the state this script exists to end.

This is not an opt-out. A business that asks not to be contacted belongs in the
suppression ledger via `mark_outcome.py --event opt_out`; `Not Fit` here means
"we chose not to call", which carries no compliance weight and must never be
read as consent state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine import dial_decisions
from lead_machine.compliance import (
    ComplianceError,
    is_mark_sent_source_status,
    is_suppressed,
    load_configured_suppression_index,
    opt_out_language_in,
)

DECLINED_STATUS = "Not Fit"

# Said on every decline, because the receipt is the only thing the rep reads.
# A green `{"status": "Not Fit"}` here is visually identical to the visible half
# of an opt-out, and that resemblance is the whole risk.
NO_SUPPRESSION_NOTE = (
    "No suppression was recorded — this is a 'we chose not to call', not consent "
    "state. If the business asked not to be contacted, run "
    "mark_outcome.py --event opt_out instead; only that writes the ledger row "
    "that stops re-discovery under a new record id."
)


class MarkDeclinedError(RuntimeError):
    pass


def airtable_record_url(base_id: str, table: str, record_id: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}/{record_id}"


def _request_json(response: requests.Response, operation: str) -> dict:
    if response.status_code >= 400:
        raise MarkDeclinedError(
            f"Airtable {operation} failed {response.status_code}: {response.text[:1000]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise MarkDeclinedError(f"Airtable {operation} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise MarkDeclinedError(f"Airtable {operation} returned a non-object response")
    return payload


def apply_mark_declined(
    *,
    record_id: str,
    reason: str,
    decisions_source: str,
    base_id: str,
    table: str,
    token: str,
    rank: float | None = None,
    decided_by: str = "",
    session: object = requests,
) -> dict:
    """Record the refusal, then retire the lead from the queue."""
    if not str(reason or "").strip():
        raise MarkDeclinedError(
            "--reason is required: a refusal with no reason records that a lead "
            "was rejected but not which signal was wrong, and the reason is the "
            "only thing that can retire or restore a demoted blocker"
        )

    # Screened before any call, so a misfiled opt-out costs a retyped reason
    # rather than a missing suppression row. Refusing is the safe direction: the
    # rep is told which command to use, and the lead stays in the queue until
    # one of them is run.
    phrase = opt_out_language_in(reason)
    if phrase:
        raise MarkDeclinedError(
            f"--reason contains opt-out language ({phrase!r}), and this command "
            "records no consent state: it writes Status='Not Fit' and nothing "
            "else, so the business would be re-discovered under a new record id "
            "and contacted again. If they asked not to be contacted, run "
            "mark_outcome.py --event opt_out, which writes the suppression "
            "ledger row. If they did not, reword the reason."
        )
    if not str(decisions_source or "").strip():
        raise MarkDeclinedError(
            "no decision ledger configured "
            f"(${dial_decisions.DIAL_DECISIONS_ENV} or --decisions-source); the ledger "
            "is the entire point of this command, so recording the refusal only "
            "in Airtable would lose it"
        )

    url = airtable_record_url(base_id, table, record_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    current = _request_json(session.get(url, headers=headers, timeout=30), "GET")
    if current.get("id") != record_id:
        raise MarkDeclinedError("Airtable GET did not confirm the requested record id")
    raw_fields = current.get("fields")
    if not isinstance(raw_fields, dict):
        raise MarkDeclinedError("Airtable GET response has no fields object")
    fields = dict(raw_fields)

    # Read, never gated. A suppressed lead being retired is the outcome we want,
    # so refusing would block the rep for no safety gain — but `already_applied`
    # on a record retired by an opt-out looks exactly like one retired by an
    # earlier decline, and the rep should know which they are holding.
    suppression_note = ""
    try:
        suppression = load_configured_suppression_index()
    except ComplianceError as exc:
        # Fails open deliberately — this load gates nothing, it only decides
        # which note the receipt carries, and an empty index falls through to
        # the *more* cautionary text. But it must not fail open in silence: this
        # is the same index `mark_sent` hard-refuses on, so a dead
        # $LPM_OUTCOMES_JSONL would otherwise stay invisible for a whole decline
        # session and surface only at the first send attempt.
        suppression = frozenset()
        print(
            f"[WARN] suppression index unreadable ({exc}); this decline is unaffected, "
            "but the same index gates mark_sent — fix it before dialling.",
            file=sys.stderr,
        )
    if suppression and is_suppressed({"id": record_id, "fields": fields}, suppression):
        suppression_note = (
            "This record is already suppressed (opt-out). It is retired for "
            "compliance, not judgement; the decline is recorded for the KPI only."
        )

    status = str(fields.get("Status") or "").strip()
    if status == DECLINED_STATUS:
        return {
            "result": "already_applied",
            "record_id": record_id,
            "status": DECLINED_STATUS,
            "suppression_recorded": False,
            "suppression_note": suppression_note or NO_SUPPRESSION_NOTE,
        }
    # `is_mark_sent_source_status` is the shared reading of "still in the
    # queue", so the decline gate and the dial gate cannot drift apart.
    if not is_mark_sent_source_status(status):
        raise MarkDeclinedError(
            f"current Airtable Status {status!r} is not eligible for a pre-dial "
            "decline; a decline is a judgement made before the call, and this "
            "lead has already been worked. Use mark_outcome.py instead."
        )

    dial_decisions.append_decision(
        decisions_source,
        decision=dial_decisions.DECISION_DECLINE,
        record_id=record_id,
        reason=reason,
        rank=rank,
        business_name=fields.get("Business Name", ""),
        vertical=fields.get("Vertical", ""),
        decided_by=decided_by,
    )

    response = _request_json(
        session.patch(
            url, headers=headers, json={"fields": {"Status": DECLINED_STATUS}}, timeout=30
        ),
        "PATCH",
    )
    applied = response.get("fields")
    if not isinstance(applied, dict) or str(applied.get("Status") or "").strip() != DECLINED_STATUS:
        raise MarkDeclinedError(
            "Airtable PATCH did not confirm the declined status; the decision is "
            "recorded in the ledger and the lead will be served again"
        )
    return {
        "result": "applied",
        "record_id": record_id,
        "status": DECLINED_STATUS,
        "reason": reason,
        # Stated on every receipt, never inferred from its absence: this command
        # writes no consent state, and the rep must not read `Not Fit` as one.
        "suppression_recorded": False,
        "suppression_note": suppression_note or NO_SUPPRESSION_NOTE,
    }


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-id", required=True, help="Airtable record id")
    parser.add_argument(
        "--reason", required=True, help="Why this lead is not worth a call, in the rep's words"
    )
    parser.add_argument(
        "--rank", default="", help="dial_rank the lead was served at, if known"
    )
    parser.add_argument(
        "--decisions-source",
        default="",
        help=f"Decision ledger JSONL; defaults to ${dial_decisions.DIAL_DECISIONS_ENV}",
    )
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    args = parser.parse_args()

    if not args.base_id or not args.table or not args.token:
        print(
            "[ERROR] AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required",
            file=sys.stderr,
        )
        return 1

    try:
        receipt = apply_mark_declined(
            record_id=args.record_id,
            reason=args.reason,
            decisions_source=args.decisions_source.strip()
            or dial_decisions.configured_decisions_path(),
            base_id=args.base_id,
            table=args.table,
            token=args.token,
            rank=float(args.rank) if str(args.rank).strip() else None,
            decided_by=os.getenv("LPM_SETTER", ""),
        )
    except (MarkDeclinedError, dial_decisions.DialDecisionError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("[ERROR] --rank must be a number", file=sys.stderr)
        return 1

    print(json.dumps(receipt, sort_keys=True))
    # Also on stderr in prose. The JSON is piped and parsed by the queue tooling;
    # the human reading the terminal is the one who might mistake this for an
    # opt-out, and they are the audience for the warning.
    note = receipt.get("suppression_note")
    if note:
        print(f"[NOTE] {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
