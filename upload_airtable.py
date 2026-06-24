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

from lead_machine.email_hygiene import clean_airtable_email
from lead_machine.airtable_sync import (
    TYPED_AIRTABLE_FIELDS,
    build_airtable_payload,
    normalize_airtable_value,
    plan_airtable_sync,
)
from lead_machine.schema import normalize_phone


_TIER_SENDABLE = "Sendable Packet"
_TIER_MANUAL = "Manual Review Packet"

# (field, airtable_type, notes)
_FIELD_SCHEMA: List[tuple[str, str, str]] = [
    ("Business Name",          "Single line text",   ""),
    ("Owner Name",             "Single line text",   ""),
    ("Vertical",               "Single select",      "e.g. detailing, pressure_washing, landscaping"),
    ("City",                   "Single line text",   ""),
    ("Phone",                  "Phone number",       ""),
    ("Email",                  "Email",              ""),
    ("Website URL",            "URL",                ""),
    ("Google Maps URL",        "URL",                ""),
    ("Reviews",                "Number",             "Integer; set field to 'Integer' precision"),
    ("Rating",                 "Number",             "Decimal; set field to 2 decimal places"),
    ("Current Website Quality","Single line text",   ""),
    ("Best Service Angle",     "Single select",      "Options: No-Site Launch, Website Rebuild, Estimate Funnel, Automation / Follow-Up"),
    ("Specific Observation",   "Long text",          ""),
    ("Packet Eligible",        "Single select",      "Options: Yes, No"),
    ("Sendable",               "Single select",      "Options: Yes, No"),
    ("Lead Priority",          "Single select",      "Options: A, B, C (grade)"),
    ("Offer Fit",              "Single select",      "Options: No-Site Launch, Website Rebuild, Estimate Funnel, Automation / Follow-Up"),
    ("Site Status",            "Single select",      "Options: Missing, Broken, No Website, Loaded, Redirect Issue, Social Only, Unknown"),
    ("Contact Form Detected",  "Single select",      "Options: Yes, No"),
    ("Estimate Form Detected", "Single select",      "Options: Yes, No"),
    ("Form Strength",          "Single line text",   ""),
    ("Form Detection Method",  "Single line text",   ""),
    ("CTA Strength",           "Single line text",   ""),
    ("Visible Pain Score",     "Number",             "Integer 0–5"),
    ("Visible Pain Reasons",   "Long text",          ""),
    ("Send Trigger",           "Single line text",   ""),
    ("Money Signal",           "Single line text",   ""),
    ("Buyer Intent Signals",   "Long text",          "Comma-separated signals detected by LLM"),
    ("Review Demand Band",     "Single line text",   ""),
    ("AI Confidence",          "Single select",      "Options: low, medium, high"),
    ("Score Total",            "Number",             "Integer"),
    ("Opener",                 "Long text",          ""),
    ("Recommended Action",     "Single line text",   ""),
    ("Send Blockers",          "Long text",          ""),
    ("Skip Reason",            "Single line text",   ""),
    ("Place Key",              "Single line text",   "Used as upsert key; must be unique per business"),
    ("Status",                 "Single select",      "Options: Mockup Needed, Manual Review, Sent, Follow-Up, Closed"),
    ("Outreach Channel",       "Single select",      "Options: Google Maps, Email, Phone"),
    ("Next Action",            "Single line text",   ""),
    ("Last Contacted",         "Date",               ""),
    ("Next Follow-Up",         "Date",               ""),
    ("Assigned To",            "Single line text",   ""),
    ("Mockup URL",             "URL",                ""),
    ("Video URL",              "URL",                ""),
    ("Packet Path",            "Single line text",   "Local file path to the packet markdown file"),
]


def build_schema_report(
    existing_field_names: set[str] | None = None,
) -> List[Dict[str, Any]]:
    """Return a list of dicts describing every field the pipeline may write.

    If *existing_field_names* is provided, the ``exists`` key reflects whether
    the field is present in that set.  When ``None`` the field is unchecked and
    ``exists`` is ``None``.
    """
    rows: List[Dict[str, Any]] = []
    for field, atype, notes in _FIELD_SCHEMA:
        if existing_field_names is None:
            exists: bool | None = None
        else:
            exists = field in existing_field_names
        rows.append({"field": field, "type": atype, "exists": exists, "notes": notes})
    return rows


def _exists_marker(exists: bool | None) -> str:
    if exists is True:
        return "YES"
    if exists is False:
        return "MISSING"
    return "?"


def print_schema_report(report: List[Dict[str, Any]], file: Any = None) -> None:
    if file is None:
        file = sys.stdout
    col_field = max(len(r["field"]) for r in report) + 2
    col_type = max(len(r["type"]) for r in report) + 2
    header = f"{'Field':<{col_field}}{'Airtable Type':<{col_type}}{'Exists':<10}Notes"
    print(header, file=file)
    print("-" * len(header), file=file)
    for row in report:
        print(
            f"{row['field']:<{col_field}}{row['type']:<{col_type}}"
            f"{_exists_marker(row['exists']):<10}{row['notes']}",
            file=file,
        )


def _write_schema_markdown(report: List[Dict[str, Any]], path: Path) -> None:
    lines = [
        "# Airtable Schema — Required Fields\n",
        "| Field | Airtable Type | Exists | Notes |",
        "|-------|--------------|--------|-------|",
    ]
    for row in report:
        lines.append(
            f"| {row['field']} | {row['type']} | {_exists_marker(row['exists'])} | {row['notes']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_schema_csv(report: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["field", "type", "exists", "notes"])
        writer.writeheader()
        for row in report:
            writer.writerow({
                "field": row["field"],
                "type": row["type"],
                "exists": _exists_marker(row["exists"]),
                "notes": row["notes"],
            })


def write_schema_report(report: List[Dict[str, Any]], path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _write_schema_csv(report, path)
    else:
        _write_schema_markdown(report, path)


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
    "Contact Form Detected",
    "Estimate Form Detected",
    "Form Strength",
    "Form Detection Method",
    "CTA Strength",
    "Visible Pain Score",
    "Visible Pain Reasons",
    "Send Trigger",
    "Money Signal",
    "Buyer Intent Signals",
    "Review Demand Band",
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
    if field in TYPED_AIRTABLE_FIELDS:
        normalized = normalize_airtable_value(field, cleaned)
        if normalized is None:
            return ""
        return normalized
    return cleaned


def safe_slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def infer_packet_dir(input_path: Path) -> Path:
    stem = input_path.stem
    prefix = "scored_"
    suffix = "_manual"
    if stem.startswith(prefix) and stem.endswith(suffix):
        batch = stem[len(prefix) : -len(suffix)]
        if batch:
            return Path(f"packets_{batch}_manual")
    return DEFAULT_PACKET_DIR


def path_from_cwd(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def packet_path_exists(path_value: str) -> bool:
    return path_from_cwd(path_value).exists()


def packet_path_is_inside_dir(path_value: str, packet_dir: Path) -> bool:
    try:
        path = path_from_cwd(path_value).resolve()
        root = path_from_cwd(str(packet_dir)).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return False
    return True


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
                if not packet_path_exists(indexed_path):
                    continue

                indexed_place = clean_cell(index_row.get("Place Key"))
                indexed_phone = normalize_phone(index_row.get("Phone", ""))
                indexed_business = clean_cell(index_row.get("Business Name")).casefold()
                if place_key and indexed_place == place_key:
                    return indexed_path
                if phone and indexed_phone == phone:
                    return indexed_path
                if business_key and indexed_business == business_key:
                    return indexed_path

    business_slug = safe_slug(business)
    if not business_slug:
        return ""

    for path in sorted(packet_dir.rglob("*.md")):
        if business_slug in path.stem:
            return str(path)

    return ""


def current_packet_path(row: Dict[str, str], packet_dir: Path) -> str:
    packet_path = existing_packet_path(row, packet_dir)
    if packet_path:
        return packet_path

    explicit_path = clean_cell(row.get("Packet Path"))
    if not explicit_path or not packet_path_exists(explicit_path):
        return ""
    if packet_dir.exists() and not packet_path_is_inside_dir(explicit_path, packet_dir):
        return ""
    return explicit_path


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
    elif clean_cell(row.get("Packet Tier", "")) == _TIER_MANUAL:
        fields["Status"] = "Manual Review"
        defaulted_fields.append("Status")

    # Filter platform/system emails before Airtable payload creation.
    if "Email" in fields:
        cleaned_email = clean_airtable_email(fields["Email"])
        if cleaned_email:
            fields["Email"] = cleaned_email
        else:
            del fields["Email"]

    if not clean_cell(fields.get("Outreach Channel")):
        fields["Outreach Channel"] = "Google Maps"
        defaulted_fields.append("Outreach Channel")

    packet_path = current_packet_path(row, packet_dir)
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


def raise_csv_field_limit() -> None:
    """Allow csv to read very large fields (e.g. the Raw Evidence JSON blob).

    The default field-size limit is 128 KB; real scored/tiered CSVs routinely
    exceed it, which would otherwise raise
    `_csv.Error: field larger than field limit`.
    """
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def read_csv_records(
    path: Path, *, only_sendable: bool, include_manual_review: bool = False, packet_dir: Path
) -> List[Dict[str, Any]]:
    raise_csv_field_limit()
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
            if include_manual_review and not only_sendable:
                tier = (row.get("Packet Tier") or "").strip()
                if tier not in {_TIER_SENDABLE, _TIER_MANUAL}:
                    continue
            records.append(csv_row_to_record(row, packet_dir))
        return records


def read_input_records(
    path: Path, *, only_sendable: bool, include_manual_review: bool = False, packet_dir: Path
) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        return read_csv_records(
            path, only_sendable=only_sendable, include_manual_review=include_manual_review, packet_dir=packet_dir
        )

    cards = read_cards(path)
    if only_sendable:
        return [card for card in cards if is_sendable_record(card)]
    if include_manual_review:
        def _is_manual_review(card: Dict[str, Any]) -> bool:
            fields = card.get("airtable_fields") or card
            return str(fields.get("Packet Tier", "")).strip() == _TIER_MANUAL
        return [card for card in cards if is_sendable_record(card) or _is_manual_review(card)]
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
    raise_csv_field_limit()

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="", help="Scored CSV or lead cards JSONL")
    parser.add_argument(
        "--only-sendable",
        action="store_true",
        help="Only upload Sendable = Yes and Packet Eligible = Yes rows",
    )
    parser.add_argument(
        "--include-manual-review",
        action="store_true",
        help="Upload Sendable Packet and Manual Review Packet rows; exclude Watchlist and No Packet",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned Airtable writes without sending")
    parser.add_argument("--live", action="store_true", help="Send planned writes to Airtable")
    parser.add_argument(
        "--packet-dir",
        default="",
        help="Packet directory for optional Packet Path mapping; defaults to packets_<BATCH>_manual for scored_<BATCH>_manual.csv",
    )
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    parser.add_argument(
        "--schema-report", "--print-schema",
        action="store_true",
        dest="schema_report",
        help="Print required Airtable fields and their recommended types; no sync is performed",
    )
    parser.add_argument(
        "--schema-report-output",
        default="",
        metavar="PATH",
        help="Write schema report to PATH (.md or .csv); implies --schema-report",
    )
    args = parser.parse_args()

    if args.dry_run and args.live:
        parser.error("--dry-run cannot be combined with --live")

    schema_mode = args.schema_report or bool(args.schema_report_output)

    if schema_mode:
        report = build_schema_report()
        print_schema_report(report)
        if args.schema_report_output:
            out_path = Path(args.schema_report_output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_schema_report(report, out_path)
            print(f"[SCHEMA] Written to {out_path}")
        return 0

    if not args.input:
        parser.error("--input is required (or use --schema-report to print the field schema)")

    input_path = Path(args.input)
    packet_dir = Path(args.packet_dir) if args.packet_dir else infer_packet_dir(input_path)

    try:
        if not input_path.exists():
            raise FileNotFoundError(input_path)

        records = read_input_records(
            input_path,
            only_sendable=args.only_sendable,
            include_manual_review=args.include_manual_review,
            packet_dir=packet_dir,
        )

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
