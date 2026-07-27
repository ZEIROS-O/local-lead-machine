#!/usr/bin/env python3
"""Mark one Airtable prospect record as sent.

Write-back half of the `send-queue` skill's Airtable loop. Only ever called
after Lou has already sent the message himself and reports it via
`/send-queue log` — this script logs status, it never sends anything.
Uses Airtable's `typecast` so a not-yet-listed Outreach Channel value
(e.g. "SMS") is added to the select field automatically instead of 422ing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from urllib.parse import quote

import requests


def follow_up_cadence_days() -> int:
    # Day 0 -> Day 4: first follow-up cadence step (SKILLS/outreach in
    # operator-agent documents Day 0/4/11/14; only Day 4 applies here since
    # mark_sent only ever fires once per lead — later touches are Loop C's
    # job, not built yet). Read lazily so a .env value picked up by
    # load_dotenv() in main() is honored.
    return int(os.getenv("FOLLOW_UP_CADENCE_DAYS", "4"))

from lead_machine.compliance import (
    ComplianceError,
    has_acceptable_sms_consent,
    is_mark_sent_source_status,
    is_suppressed,
    load_suppression_index,
    record_suppression_identifiers,
)
from lead_machine.sendability import contact_readiness_failures

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False


def airtable_record_url(base_id: str, table: str, record_id: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}/{record_id}"


class MarkSentError(RuntimeError):
    pass


def _request_json(response: requests.Response, operation: str) -> dict:
    if response.status_code >= 400:
        raise MarkSentError(f"Airtable {operation} failed {response.status_code}: {response.text[:1000]}")
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise MarkSentError(f"Airtable {operation} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise MarkSentError(f"Airtable {operation} returned a non-object response")
    return payload


def _exact_requested_state(
    fields: dict, *, channel: str, last_contacted: str, next_follow_up: str
) -> bool:
    if str(fields.get("Status") or "").strip().casefold() != "sent":
        return False
    if str(fields.get("Last Contacted") or "").strip() != last_contacted:
        return False
    if str(fields.get("Next Follow-Up") or "").strip() != next_follow_up:
        return False
    return not channel or str(fields.get("Outreach Channel") or "").strip() == channel


def apply_mark_sent(
    *,
    record_id: str,
    channel: str,
    last_contacted: str,
    base_id: str,
    table: str,
    token: str,
    suppression_source: str,
    session: object = requests,
) -> dict:
    try:
        suppression = load_suppression_index(suppression_source)
    except ComplianceError as exc:
        raise MarkSentError(str(exc)) from exc

    url = airtable_record_url(base_id, table, record_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    current = _request_json(session.get(url, headers=headers, timeout=30), "GET")
    if current.get("id") != record_id:
        raise MarkSentError("Airtable GET did not confirm the requested record id")
    raw_fields = current.get("fields")
    if not isinstance(raw_fields, dict):
        raise MarkSentError("Airtable GET response has no fields object")
    fields = dict(raw_fields)

    next_follow_up = (
        dt.date.fromisoformat(last_contacted) + dt.timedelta(days=follow_up_cadence_days())
    ).isoformat()

    # Exact recovery is safe even if readiness changed after the original
    # PATCH. It never performs another external mutation.
    if _exact_requested_state(
        fields, channel=channel, last_contacted=last_contacted, next_follow_up=next_follow_up
    ):
        return {
            "result": "already_applied",
            "record_id": record_id,
            "status": "Sent",
            "channel": channel,
            "date": last_contacted,
            "next_follow_up": next_follow_up,
        }

    status = str(fields.get("Status") or "").strip().casefold()
    if status == "sent":
        raise MarkSentError("record is already Sent with conflicting channel or date")
    if not is_mark_sent_source_status(fields.get("Status")):
        raise MarkSentError(
            "current Airtable Status is not eligible for mark-sent; expected New or Mockup Needed"
        )

    readiness = contact_readiness_failures(fields)
    if readiness:
        raise MarkSentError("record is not contact-ready: " + "; ".join(readiness))

    suppression_record = {"id": record_id, "fields": fields}
    if not record_suppression_identifiers(suppression_record):
        raise MarkSentError("current Airtable record has no suppression identity")
    if is_suppressed(suppression_record, suppression):
        raise MarkSentError("current Airtable record is suppressed or opted out")
    if channel.casefold() == "sms" and not has_acceptable_sms_consent(fields.get("Consent Posture")):
        raise MarkSentError("current Airtable Consent Posture does not permit SMS")

    requested_fields = {
        "Status": "Sent",
        "Last Contacted": last_contacted,
        "Next Follow-Up": next_follow_up,
    }
    if channel:
        requested_fields["Outreach Channel"] = channel
    response = _request_json(
        session.patch(
            url,
            headers=headers,
            json={"fields": requested_fields, "typecast": True},
            timeout=30,
        ),
        "PATCH",
    )
    if response.get("id") != record_id:
        raise MarkSentError("Airtable PATCH did not confirm the requested record id")
    applied_fields = response.get("fields")
    if not isinstance(applied_fields, dict) or not _exact_requested_state(
        applied_fields, channel=channel, last_contacted=last_contacted, next_follow_up=next_follow_up
    ):
        raise MarkSentError("Airtable PATCH response does not confirm the requested status, channel, and date")
    return {
        "result": "applied",
        "next_follow_up": next_follow_up,
        "record_id": record_id,
        "status": "Sent",
        "channel": channel,
        "date": last_contacted,
    }


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-id", required=True, help="Airtable record id (from query_ready_to_send.py)")
    parser.add_argument("--channel", default="", help="Outreach Channel value, e.g. 'SMS', 'Instagram DM'")
    parser.add_argument("--date", default="", help="Last Contacted date (YYYY-MM-DD); defaults to today")
    parser.add_argument(
        "--suppression-source",
        required=True,
        help="Resolved outcomes JSONL path used for current opt-out suppression",
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
        last_contacted = dt.date.fromisoformat(args.date).isoformat() if args.date else dt.date.today().isoformat()
    except ValueError:
        print("[ERROR] --date must use YYYY-MM-DD", file=sys.stderr)
        return 1
    try:
        receipt = apply_mark_sent(
            record_id=args.record_id,
            channel=args.channel,
            last_contacted=last_contacted,
            base_id=args.base_id,
            table=args.table,
            token=args.token,
            suppression_source=args.suppression_source,
        )
    except MarkSentError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
