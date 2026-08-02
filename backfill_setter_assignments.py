#!/usr/bin/env python3
"""Backfill blank Airtable setter assignments without touching manual values."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine.setter_assignment import (
    assign_setter,
    assignment_identity,
    configured_setter_roster,
)
from upload_airtable import (
    AIRTABLE_MIN_SECONDS_BETWEEN_WRITES,
    _airtable_write_with_retry,
    airtable_base_url,
    assert_required_choices_ready,
)


BACKFILL_FIELDS = ("Assigned Setter", "Business Name", "Place Key", "Phone")


def _assigned_setter(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def plan_setter_assignment_backfill(
    records: Iterable[dict[str, Any]],
    roster: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Plan PATCHes for blank assignments only, refusing ambiguous identities.

    Returns `(planned, skipped)`. A duplicate Place Key is still fatal — two
    rows claiming one identity means the base itself is wrong, and assigning
    either would be a guess. A row with neither a Place Key nor a phone is
    merely unhashable: it is reported and skipped, because one legacy row must
    not withhold the backfill from every other blank assignment in the base.
    """
    if not roster:
        raise ValueError("LPM_SETTER_ROSTER must contain at least one active setter")

    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_place_keys: dict[str, str] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        fields = record.get("fields")
        if not isinstance(fields, dict):
            raise ValueError(f"Airtable record {record_id or '<missing id>'} has no fields object")

        place_key = str(fields.get("Place Key") or "").strip()
        if place_key:
            prior = seen_place_keys.get(place_key)
            if prior and prior != record_id:
                raise ValueError(
                    f"duplicate Place Key {place_key} on Airtable records {prior} and {record_id}"
                )
            seen_place_keys[place_key] = record_id

        # Existing values are human-owned, even if they disagree with the
        # current hash or name a rep who has since left the active roster.
        if _assigned_setter(fields.get("Assigned Setter")):
            continue
        if not record_id:
            raise ValueError("blank Assigned Setter record has no Airtable record id")

        identity = assignment_identity(place_key, fields.get("Phone"))
        if not identity:
            skipped.append(
                {
                    "record_id": record_id,
                    "business_name": str(fields.get("Business Name") or "").strip(),
                    "reason": "no Place Key or Phone to hash; assign this row by hand",
                }
            )
            continue

        setter = assign_setter(identity, roster)
        planned.append(
            {
                "method": "PATCH",
                "record_id": record_id,
                "place_key": place_key,
                "upsert_key": f"place:{place_key}" if place_key else identity,
                "payload": {"fields": {"Assigned Setter": setter}},
            }
        )
    return planned, skipped


def assignment_counts(planned: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(
        str((item.get("payload") or {}).get("fields", {}).get("Assigned Setter") or "")
        for item in planned
    )
    counts.pop("", None)
    return dict(sorted(counts.items()))


def fetch_prospects(base_id: str, table: str, token: str) -> list[dict[str, Any]]:
    """Fetch only the fields needed to plan the blank-only backfill."""
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    params: list[tuple[str, Any]] = [("pageSize", 100)]
    params.extend(("fields[]", field) for field in BACKFILL_FIELDS)
    records: list[dict[str, Any]] = []

    while True:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Airtable prospect read failed {response.status_code}: {response.text[:1000]}"
            )
        payload = response.json()
        page = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            raise ValueError("Airtable prospect read returned no records list")
        records.extend(record for record in page if isinstance(record, dict))
        offset = str(payload.get("offset") or "")
        if not offset:
            return records
        params = [("pageSize", 100)]
        params.extend(("fields[]", field) for field in BACKFILL_FIELDS)
        params.append(("offset", offset))


def apply_blank_assignment_backfill(
    base_id: str,
    table: str,
    token: str,
    planned: Sequence[dict[str, Any]],
) -> int:
    """Apply a plan only while each target is still blank.

    The immediate GET closes the planning/read-to-write race: if a rep assigns
    a lead after the bulk read, their value wins. A fresh Airtable read is also
    the idempotency authority, so historical receipts never suppress a needed
    blank-only PATCH.
    """
    base_url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    applied = 0
    last_write_at = 0.0
    with requests.Session() as session:
        for item in planned:
            record_id = str(item.get("record_id") or "").strip()
            label = str(item.get("upsert_key") or record_id)
            record_url = f"{base_url}/{quote(record_id, safe='')}"
            current = session.get(
                record_url,
                headers={"Authorization": f"Bearer {token}"},
                params=[("fields[]", "Assigned Setter")],
                timeout=30,
            )
            if current.status_code >= 400:
                raise RuntimeError(
                    f"Airtable assignment recheck failed for {label} "
                    f"{current.status_code}: {current.text[:1000]}"
                )
            current_payload = current.json()
            current_fields = current_payload.get("fields") if isinstance(current_payload, dict) else None
            if not isinstance(current_fields, dict):
                raise ValueError(f"Airtable assignment recheck returned no fields for {label}")
            if _assigned_setter(current_fields.get("Assigned Setter")):
                print(f"[SKIP] {label}: Assigned Setter became non-blank after planning")
                continue

            wait = AIRTABLE_MIN_SECONDS_BETWEEN_WRITES - (time.monotonic() - last_write_at)
            if wait > 0:
                time.sleep(wait)
            response = _airtable_write_with_retry(
                session,
                method="PATCH",
                url=record_url,
                headers=headers,
                payload=item["payload"],
                label=label,
            )
            last_write_at = time.monotonic()
            response_payload = response.json()
            response_fields = response_payload.get("fields") if isinstance(response_payload, dict) else None
            expected = str(item["payload"]["fields"]["Assigned Setter"])
            confirmed = (
                _assigned_setter(response_fields.get("Assigned Setter"))
                if isinstance(response_fields, dict)
                else ""
            )
            if confirmed != expected:
                raise RuntimeError(
                    f"Airtable PATCH did not confirm Assigned Setter {expected!r} for {label}"
                )
            applied += 1
            print(f"[SYNC] PATCH {label}")
    return applied


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the blank-only PATCH plan")
    mode.add_argument("--live", action="store_true", help="apply the PATCH plan after schema preflight")
    parser.add_argument("--base-id", default="")
    parser.add_argument("--table", default="")
    parser.add_argument("--token", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    base_id = args.base_id or os.getenv("AIRTABLE_BASE_ID", "")
    table = args.table or os.getenv("AIRTABLE_PROSPECTS_TABLE", "")
    token = (
        args.token
        or os.getenv("AIRTABLE_PAT", "")
        or os.getenv("AIRTABLE_API_KEY", "")
    )
    try:
        missing_config = [
            name
            for name, value in (
                ("AIRTABLE_BASE_ID", base_id),
                ("AIRTABLE_PROSPECTS_TABLE", table),
                ("AIRTABLE_PAT", token),
            )
            if not value
        ]
        if missing_config:
            raise ValueError("missing Airtable configuration: " + ", ".join(missing_config))

        roster = configured_setter_roster()
        if not roster:
            raise ValueError("LPM_SETTER_ROSTER must contain at least one active setter")
        if args.live:
            assert_required_choices_ready(base_id, token, table)

        records = fetch_prospects(base_id, table, token)
        planned, skipped = plan_setter_assignment_backfill(records, roster)
        print(
            json.dumps(
                {
                    "mode": "live" if args.live else "dry-run",
                    "records_read": len(records),
                    "blank_assignments": len(planned),
                    "unidentifiable": len(skipped),
                    "assignment_counts": assignment_counts(planned),
                },
                sort_keys=True,
            )
        )
        for item in skipped:
            print(
                f"[SKIP] {item['business_name'] or '<unnamed>'} "
                f"({item['record_id']}): {item['reason']}"
            )
        if not args.live:
            print(json.dumps(planned, ensure_ascii=False, indent=2))
            return 0

        applied = apply_blank_assignment_backfill(
            base_id,
            table,
            token,
            planned,
        )
        print(f"[DONE] Backfilled setter assignments: {applied} of {len(planned)} planned")
        return 0
    except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
