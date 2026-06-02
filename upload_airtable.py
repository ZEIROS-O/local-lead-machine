#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine.airtable_sync import plan_airtable_sync


def read_cards(path: Path) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cards.append(json.loads(line))
    return cards


def airtable_base_url(base_id: str, table: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"


def fetch_existing_records(base_id: str, table: str, token: str) -> Dict[str, str]:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    params: Dict[str, Any] = {"pageSize": 100, "fields[]": "Place Key"}
    existing: Dict[str, str] = {}

    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Airtable list failed {resp.status_code}: {resp.text[:1000]}")
        data = resp.json()
        for record in data.get("records", []):
            key = str((record.get("fields") or {}).get("Place Key") or "")
            if key:
                existing[key] = record.get("id", "")
        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset

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
                f"Airtable {item['method']} failed for {item['place_key']} "
                f"{resp.status_code}: {resp.text[:1000]}"
            )
        print(f"[SYNC] {item['method']} {item['place_key']}")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Lead cards JSONL")
    parser.add_argument("--dry-run", action="store_true", help="Print planned Airtable writes without sending")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    cards = read_cards(input_path)

    if args.dry_run:
        planned = plan_airtable_sync(cards, existing_records_by_key={})
        print(f"[DRY-RUN] Cards: {len(cards)}")
        print(json.dumps(planned, ensure_ascii=False, indent=2))
        return 0

    if not args.base_id or not args.table or not args.token:
        print("[ERROR] AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required", file=sys.stderr)
        return 1

    existing = fetch_existing_records(args.base_id, args.table, args.token)
    planned = plan_airtable_sync(cards, existing_records_by_key=existing)
    send_airtable_plan(args.base_id, args.table, args.token, planned)
    print(f"[DONE] Synced cards: {len(planned)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
