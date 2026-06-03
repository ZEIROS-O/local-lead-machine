#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine.airtable_sync import build_airtable_payload, plan_airtable_sync
from lead_machine.schema import normalize_phone


CSV_AIRTABLE_FIELDS = [
    "Business Name",
    "Owner Name",
    "Vertical",
    "City",
    "Phone",
    "Email",
    "Website URL",
    "Google Maps URL",
    "Reviews",
    "Rating",
    "Current Website Quality",
    "Best Service Angle",
    "Specific Observation",
    "Packet Eligible",
    "Sendable",
    "Lead Priority",
    "Offer Fit",
    "Site Status",
    "AI Confidence",
    "Skip Reason",
    "Score Total",
    "Opener",
    "Recommended Action",
    "Send Blockers",
    "Place Key",
    "Mockup URL",
    "Video URL",
    "Last Contacted",
    "Next Follow-Up",
    "Assigned To",
]

OPERATIONAL_FIELDS = ["Status", "Next Action", "Outreach Channel"]
OPTIONAL_FIELDS = ["Packet Path"]
DEFAULT_PACKET_DIR = Path("packets_manual_existing_manual")
INTEGER_FIELDS = {"Reviews", "Score Total"}
FLOAT_FIELDS = {"Rating"}


def read_cards(path: Path) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cards.append(json.loads(line))
    return cards


def truthy_yes(value: Any) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def is_sendable_record(record: Dict[str, Any]) -> bool:
    fields = record.get("airtable_fields") or record
    sendable = record.get("sendable")
    packet_eligible = record.get("packet_eligible")

    if sendable is None:
        sendable = fields.get("Sendable")
    if packet_eligible is None:
        packet_eligible = fields.get("Packet Eligible")

    return truthy_yes(sendable) and truthy_yes(packet_eligible)


def clean_cell(value: Any) -> str:
    return str(value or "").strip()


def coerce_airtable_value(field: str, value: Any) -> Any:
    cleaned = clean_cell(value)
    if cleaned == "":
        return ""
    if field in INTEGER_FIELDS:
        try:
            return int(float(cleaned))
        except ValueError:
            return cleaned
    if field in FLOAT_FIELDS:
        try:
            return float(cleaned)
        except ValueError:
            return cleaned
    return cleaned


def safe_slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def existing_packet_path(row: Dict[str, str], packet_dir: Path) -> str:
    if not packet_dir.exists():
        return ""

    business = clean_cell(row.get("Business Name"))
    business_key = business.casefold()
    phone = normalize_phone(row.get("Phone", ""))
    place_key = clean_cell(row.get("Place Key"))

    index_path = packet_dir / "packet_index.csv"
    if index_path.exists():
        with index_path.open(newline="", encoding="utf-8") as f:
            for index_row in csv.DictReader(f):
                indexed_path = clean_cell(index_row.get("Packet Path"))
                if not indexed_path:
                    continue
                path = Path(indexed_path)
                if not path.is_absolute():
                    path = Path.cwd() / path
                if not path.exists():
                    continue

                indexed_place = clean_cell(index_row.get("Place Key"))
                indexed_phone = normalize_phone(index_row.get("Phone", ""))
                indexed_business = clean_cell(index_row.get("Business Name")).casefold()
                if place_key and indexed_place == place_key:
                    return str(path)
                if phone and indexed_phone == phone:
                    return str(path)
                if business_key and indexed_business == business_key:
                    return str(path)

    business_slug = safe_slug(business)
    if not business_slug:
        return ""

    for path in sorted(packet_dir.rglob("*.md")):
        if business_slug in path.stem:
            return str(path)

    return ""


def csv_row_to_record(row: Dict[str, str], packet_dir: Path) -> Dict[str, Any]:
    business_name = clean_cell(row.get("Business Name"))
    if not business_name:
        raise ValueError("CSV row missing required Business Name")

    fields: Dict[str, Any] = {}
    defaulted_fields: List[str] = []
    for field in CSV_AIRTABLE_FIELDS:
        value = coerce_airtable_value(field, row.get(field, ""))
        if value != "":
            fields[field] = value

    for field in OPERATIONAL_FIELDS:
        value = clean_cell(row.get(field, ""))
        if value:
            fields[field] = value

    if is_sendable_record(fields):
        if not clean_cell(fields.get("Status")):
            fields["Status"] = "Mockup Needed"
            defaulted_fields.append("Status")
        if not clean_cell(fields.get("Next Action")):
            fields["Next Action"] = "Build mockup"
            defaulted_fields.append("Next Action")

    if not clean_cell(fields.get("Outreach Channel")):
        fields["Outreach Channel"] = "Google Maps"
        defaulted_fields.append("Outreach Channel")

    packet_path = existing_packet_path(row, packet_dir)
    if packet_path:
        fields["Packet Path"] = packet_path

    place_key = clean_cell(row.get("Place Key"))
    phone_key = normalize_phone(row.get("Phone", ""))
    if place_key:
        upsert_key = f"place:{place_key}"
    elif phone_key:
        upsert_key = f"phone:{phone_key}"
    else:
        name = clean_cell(row.get("Business Name")) or "<unnamed>"
        raise ValueError(f"CSV row cannot be upserted because Place Key and Phone are blank: {name}")

    return {
        "place_id_or_unique_key": place_key,
        "upsert_key": upsert_key,
        "business_name": business_name,
        "phone": clean_cell(row.get("Phone")),
        "airtable_fields": fields,
        "defaulted_airtable_fields": defaulted_fields,
    }


def read_csv_records(path: Path, *, only_sendable: bool, packet_dir: Path) -> List[Dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [field for field in ["Business Name"] if field not in fieldnames]
        if missing:
            raise ValueError(f"Input CSV missing required columns: {', '.join(missing)}")

        records: List[Dict[str, Any]] = []
        for row in reader:
            if only_sendable and not (
                truthy_yes(row.get("Sendable")) and truthy_yes(row.get("Packet Eligible"))
            ):
                continue
            records.append(csv_row_to_record(row, packet_dir))
        return records


def read_input_records(path: Path, *, only_sendable: bool, packet_dir: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        return read_csv_records(path, only_sendable=only_sendable, packet_dir=packet_dir)

    cards = read_cards(path)
    if only_sendable:
        return [card for card in cards if is_sendable_record(card)]
    return cards


def airtable_base_url(base_id: str, table: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"


def request_params_for_fields(fields: Sequence[str], *, page_size: int = 100) -> List[tuple[str, Any]]:
    params: List[tuple[str, Any]] = [("pageSize", page_size)]
    for field in fields:
        params.append(("fields[]", field))
    return params


def validate_airtable_fields(base_id: str, table: str, token: str, fields: Iterable[str]) -> None:
    field_list = sorted({field for field in fields if field})
    if not field_list:
        return

    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=request_params_for_fields(field_list, page_size=1), timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(
            "Airtable field validation failed. Create the required fields before running live sync: "
            f"{', '.join(field_list)}. Airtable response {resp.status_code}: {resp.text[:1000]}"
        )


def airtable_field_exists(base_id: str, table: str, token: str, field: str) -> bool:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=request_params_for_fields([field], page_size=1), timeout=30)
    if resp.status_code < 400:
        return True
    if resp.status_code == 422:
        return False
    raise RuntimeError(f"Airtable field check failed for {field} {resp.status_code}: {resp.text[:1000]}")


def remove_airtable_field(records: Iterable[Dict[str, Any]], field: str) -> None:
    for record in records:
        fields = record.get("airtable_fields") or {}
        fields.pop(field, None)


def payload_field_names(records: Iterable[Dict[str, Any]]) -> List[str]:
    field_names = set()
    for record in records:
        field_names.update(build_airtable_payload(record).get("fields", {}).keys())
    return sorted(field_names)


def fetch_existing_records(base_id: str, table: str, token: str) -> Dict[str, str]:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    params = request_params_for_fields(["Place Key", "Phone"])
    existing: Dict[str, str] = {}

    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Airtable list failed {resp.status_code}: {resp.text[:1000]}")
        data = resp.json()
        for record in data.get("records", []):
            fields = record.get("fields") or {}
            record_id = record.get("id", "")
            place_key = clean_cell(fields.get("Place Key"))
            if place_key:
                existing[f"place:{place_key}"] = record_id
                existing[place_key] = record_id
            phone = normalize_phone(fields.get("Phone", ""))
            if phone:
                existing[f"phone:{phone}"] = record_id
        offset = data.get("offset")
        if not offset:
            break
        params = request_params_for_fields(["Place Key", "Phone"])
        params.append(("offset", offset))

    return existing


def send_airtable_plan(base_id: str, table: str, token: str, planned: List[Dict[str, Any]]) -> None:
    base_url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for item in planned:
        if item["method"] == "PATCH":
            url = f"{base_url}/{item['record_id']}"
            resp = requests.patch(url, headers=headers, json=item["payload"], timeout=30)
        else:
            resp = requests.post(base_url, headers=headers, json=item["payload"], timeout=30)

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Airtable {item['method']} failed for {item.get('upsert_key') or item.get('place_key')} "
                f"{resp.status_code}: {resp.text[:1000]}"
            )
        print(f"[SYNC] {item['method']} {item.get('upsert_key') or item.get('place_key')}")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Scored CSV or lead cards JSONL")
    parser.add_argument(
        "--only-sendable",
        action="store_true",
        help="Only upload Sendable = Yes and Packet Eligible = Yes rows",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned Airtable writes without sending")
    parser.add_argument("--live", action="store_true", help="Send planned writes to Airtable")
    parser.add_argument(
        "--packet-dir",
        default=str(DEFAULT_PACKET_DIR),
        help="Packet directory for optional Packet Path mapping",
    )
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    args = parser.parse_args()

    if args.dry_run and args.live:
        parser.error("--dry-run cannot be combined with --live")

    input_path = Path(args.input)
    packet_dir = Path(args.packet_dir)

    try:
        if not input_path.exists():
            raise FileNotFoundError(input_path)

        records = read_input_records(input_path, only_sendable=args.only_sendable, packet_dir=packet_dir)

        if not args.live:
            planned = plan_airtable_sync(records, existing_records_by_key={})
            print(f"[DRY-RUN] Records: {len(records)}")
            print(json.dumps(planned, ensure_ascii=False, indent=2))
            return 0

        if not args.base_id or not args.table or not args.token:
            print(
                "[ERROR] AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required",
                file=sys.stderr,
            )
            return 1

        for field in OPTIONAL_FIELDS:
            has_value = any((record.get("airtable_fields") or {}).get(field) for record in records)
            if has_value and not airtable_field_exists(args.base_id, args.table, args.token, field):
                remove_airtable_field(records, field)

        if input_path.suffix.lower() == ".csv":
            validate_airtable_fields(args.base_id, args.table, args.token, CSV_AIRTABLE_FIELDS + OPERATIONAL_FIELDS)
        else:
            validate_airtable_fields(args.base_id, args.table, args.token, payload_field_names(records))

        existing = fetch_existing_records(args.base_id, args.table, args.token)
        planned = plan_airtable_sync(records, existing_records_by_key=existing)
        send_airtable_plan(args.base_id, args.table, args.token, planned)
        print(f"[DONE] Synced records: {len(planned)}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
