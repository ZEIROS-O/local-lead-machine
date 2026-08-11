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
from lead_machine.compliance import is_mark_sent_source_status

DECLINED_STATUS = "Not Fit"


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

    status = str(fields.get("Status") or "").strip()
    if status == DECLINED_STATUS:
        return {"result": "already_applied", "record_id": record_id, "status": DECLINED_STATUS}
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
