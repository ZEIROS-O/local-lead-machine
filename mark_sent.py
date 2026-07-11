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
import os
import sys
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False


def airtable_record_url(base_id: str, table: str, record_id: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}/{record_id}"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-id", required=True, help="Airtable record id (from query_ready_to_send.py)")
    parser.add_argument("--channel", default="", help="Outreach Channel value, e.g. 'SMS', 'Instagram DM'")
    parser.add_argument("--date", default="", help="Last Contacted date (YYYY-MM-DD); defaults to today")
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

    last_contacted = args.date or dt.date.today().isoformat()
    fields = {"Status": "Sent", "Last Contacted": last_contacted}
    if args.channel:
        fields["Outreach Channel"] = args.channel

    url = airtable_record_url(args.base_id, args.table, args.record_id)
    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}
    payload = {"fields": fields, "typecast": True}

    resp = requests.patch(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        print(f"[ERROR] Airtable PATCH failed {resp.status_code}: {resp.text[:1000]}", file=sys.stderr)
        return 1

    print(f"[SENT] {args.record_id} -> Status=Sent, Last Contacted={last_contacted}" + (f", Outreach Channel={args.channel}" if args.channel else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
