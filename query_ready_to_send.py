#!/usr/bin/env python3
"""Read-only Airtable query for records ready to send.

Built for the ZEIROS `send-queue` skill: pulls LPM prospects where
Sendable=Yes and Packet Eligible=Yes and Status is not yet Sent/Closed,
so the daily send queue can draft against real pipeline state instead of
stale exports. Never writes anything (see mark_sent.py for the write-back
half of this loop).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine.airtable_sync import resolve_vertical_label


DISPLAY_FIELDS = [
    "Business Name",
    "Owner Name",
    "Vertical",
    "City",
    "Metro Context",
    "Phone",
    "Email",
    "Website URL",
    "Reviews",
    "Rating",
    "AI Confidence",
    "Money Signal",
    "Buyer Intent Signals",
    "Send Trigger",
    "Best Service Angle",
    "Offer Fit",
    "Specific Observation",
    "Site Status",
    "Opener",
    "Safe Outreach Claims",
    "Evidence Refs",
    "Evidence Snapshot",
    "Commercial Fit State",
    "Commercial Materiality",
    "Recurring Consequence",
    "Root Cause Confidence",
    "LPM Offer Component",
    "Packageable Solution",
    "Value Mechanism",
    "Beyond Free Fix",
    "Self Fix Risk",
    "Intentionality",
    "Contact Eligibility",
    "Asset State",
    "Promise State",
    "Asset ID",
    "Asset Verifier Verdict",
    "Outreach Channel",
    "Consent Posture",
    "Mockup URL",
    "Video URL",
    "Lead Priority",
    "Score Total",
    "Status",
    "Last Contacted",
    "Next Follow-Up",
]

_PRIORITY_RANK = {"A": 0, "B": 1, "C": 2}
REQUIRED_READINESS_FIELDS = {"Commercial Fit State", "Contact Eligibility"}


def airtable_base_url(base_id: str, table: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"


def build_formula(vertical: str) -> str:
    formula = (
        "AND({Sendable}='Yes',{Packet Eligible}='Yes',"
        "{Commercial Fit State}='commercially-sendable',{Contact Eligibility}='contact-now',"
        "NOT({Status}='Sent'),NOT({Status}='Closed')"
    )
    if vertical:
        escaped = vertical.replace("'", "\\'")
        formula += f",{{Vertical}}='{escaped}'"
    formula += ")"
    return formula


_UNKNOWN_FIELD_RE = re.compile(r'Unknown field name: \\?"(.+?)\\?"')


def fetch_ready_records(base_id: str, table: str, token: str, *, vertical: str = "") -> List[Dict[str, Any]]:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}

    # A column that exists in DISPLAY_FIELDS but not (yet) in the base 422s the
    # whole query; drop it with a warning instead so the morning queue degrades
    # to an empty value rather than a dead digest.
    requested = list(DISPLAY_FIELDS)
    while True:
        params: List[tuple[str, Any]] = [("filterByFormula", build_formula(vertical))]
        for field in requested:
            params.append(("fields[]", field))
        probe = requests.get(url, headers=headers, params=params + [("maxRecords", 1)], timeout=30)
        if probe.status_code < 400:
            break
        match = _UNKNOWN_FIELD_RE.search(probe.text)
        if (
            probe.status_code == 422
            and match
            and match.group(1) in requested
            and match.group(1) not in REQUIRED_READINESS_FIELDS
        ):
            print(f"[WARN] Airtable has no column '{match.group(1)}'; querying without it", file=sys.stderr)
            requested.remove(match.group(1))
            continue
        raise RuntimeError(f"Airtable list failed {probe.status_code}: {probe.text[:1000]}")

    params = [("filterByFormula", build_formula(vertical))]
    for field in requested:
        params.append(("fields[]", field))

    records: List[Dict[str, Any]] = []
    offset = None
    while True:
        page_params = list(params)
        if offset:
            page_params.append(("offset", offset))
        resp = requests.get(url, headers=headers, params=page_params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Airtable list failed {resp.status_code}: {resp.text[:1000]}")
        data = resp.json()
        for record in data.get("records", []):
            fields = record.get("fields") or {}
            row = {"record_id": record.get("id", "")}
            row.update({field: fields.get(field, "") for field in DISPLAY_FIELDS})
            if (
                str(row.get("Commercial Fit State") or "").strip().casefold() != "commercially-sendable"
                or str(row.get("Contact Eligibility") or "").strip().casefold() != "contact-now"
            ):
                print(
                    f"[WARN] Airtable record {row['record_id'] or '<missing-id>'} failed defensive commercial readiness check",
                    file=sys.stderr,
                )
                continue
            records.append(row)
        offset = data.get("offset")
        if not offset:
            break

    records.sort(
        key=lambda r: (
            _PRIORITY_RANK.get(str(r.get("Lead Priority", "")).strip().upper(), 9),
            -(r.get("Score Total") or 0),
        )
    )
    return records


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vertical",
        default="",
        help="Filter by Vertical — any phrase works, e.g. 'water restoration', water_restoration, "
        "'garage door repair'. Resolved via lead_machine.airtable_sync.VERTICAL_LABELS.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Truncate output to N records (0 = no limit)")
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

    vertical = resolve_vertical_label(args.vertical)

    try:
        records = fetch_ready_records(args.base_id, args.table, args.token, vertical=vertical)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.limit:
        records = records[: args.limit]

    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
