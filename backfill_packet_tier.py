#!/usr/bin/env python3
"""Repair the Airtable `Packet Tier` column from scored CSVs.

The Airtable-side sibling of `refresh_packet_tiers.py`, which does the same job
for a scored CSV. This exists because the hand cutover that replaced `Sendable`
and `Packet Eligible` with `Packet Tier` deleted the two booleans without
populating their replacement, leaving the column empty on almost every record
and `query_ready_to_send` returning nothing.

Deliberately narrow. It writes exactly one field — `Packet Tier` — on records
matched by an exact `Place Key`, and never touches anything else. A full
`upload_airtable.py` re-sync would restore the tier too, but it PATCHes the
whole ~54-column payload from whichever scored CSV it was pointed at, which on
an aged base means overwriting `Status`, `Opener`, `Score Total` and the
commercial fields with stale values and silently discarding hand edits.

Resolution rule: for each `Place Key`, the tier comes from the most recently
modified CSV that carries a non-empty tier for it. Older CSVs that disagree are
reported rather than silently discarded, because a key whose tier changed
between runs is a real signal about the scorer, not noise to swallow.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import requests
from dotenv import load_dotenv

from lead_machine.sendability import (
    PACKET_TIER_MANUAL,
    PACKET_TIER_NONE,
    PACKET_TIER_SENDABLE,
    PACKET_TIER_WATCHLIST,
)
from upload_airtable import airtable_base_url, raise_csv_field_limit


TIER_FIELD = "Packet Tier"
PLACE_KEY_FIELD = "Place Key"

VALID_TIERS = (
    PACKET_TIER_SENDABLE,
    PACKET_TIER_MANUAL,
    PACKET_TIER_WATCHLIST,
    PACKET_TIER_NONE,
)

# Airtable caps a batch write at 10 records and the API at 5 requests/second.
AIRTABLE_MAX_BATCH = 10
AIRTABLE_MIN_REQUEST_INTERVAL_SECONDS = 0.25
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0


@dataclass
class ResolvedTier:
    place_key: str
    tier: str
    source: str
    conflicts: List[tuple[str, str]] = field(default_factory=list)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def resolve_tiers_from_csvs(paths: Sequence[Path]) -> Dict[str, ResolvedTier]:
    """Map Place Key -> tier, newest file wins.

    `paths` must already be ordered newest-first; the caller owns that ordering
    so tests can pin it without touching mtimes.
    """
    raise_csv_field_limit()
    resolved: Dict[str, ResolvedTier] = {}
    for path in paths:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
        if not rows or PLACE_KEY_FIELD not in rows[0]:
            continue
        for row in rows:
            place_key = _clean(row.get(PLACE_KEY_FIELD))
            tier = _clean(row.get(TIER_FIELD))
            if not place_key or not tier:
                continue
            if tier not in VALID_TIERS:
                continue
            existing = resolved.get(place_key)
            if existing is None:
                resolved[place_key] = ResolvedTier(place_key, tier, path.name)
            elif tier != existing.tier:
                existing.conflicts.append((path.name, tier))
    return resolved


def csv_paths_newest_first(directory: Path) -> List[Path]:
    paths = [path for path in directory.glob("*.csv") if path.is_file()]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def plan_backfill(
    records: Iterable[Mapping[str, Any]],
    resolved: Mapping[str, ResolvedTier],
    *,
    overwrite_conflicts: bool = False,
    only_place_keys: Sequence[str] | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Split base records into write / skip buckets. Pure; no network.

    `only_place_keys` narrows the write set to an explicit allowlist. Most of
    the stored tiers in this repo's older CSVs predate the commercial-fit gate
    and no longer match what the scorer produces, so writing them wholesale
    would push contradicted data into the base; the allowlist is how you apply
    just the subset you have actually re-verified.
    """
    allowlist = {_clean(key) for key in only_place_keys} if only_place_keys else None
    plan: Dict[str, List[Dict[str, Any]]] = {
        "write": [],
        "already_set": [],
        "differs": [],
        "no_place_key": [],
        "unresolved": [],
        "filtered_out": [],
    }
    for record in records:
        fields = record.get("fields") or {}
        record_id = _clean(record.get("id"))
        place_key = _clean(fields.get(PLACE_KEY_FIELD))
        current = _clean(fields.get(TIER_FIELD))
        name = _clean(fields.get("Business Name"))
        entry = {"record_id": record_id, "place_key": place_key, "business_name": name, "current": current}

        if not place_key:
            plan["no_place_key"].append(entry)
            continue
        if allowlist is not None and place_key not in allowlist:
            plan["filtered_out"].append(entry)
            continue
        match = resolved.get(place_key)
        if match is None:
            plan["unresolved"].append(entry)
            continue
        entry["tier"] = match.tier
        entry["source"] = match.source
        entry["conflicts"] = [f"{name}={tier}" for name, tier in match.conflicts]

        if not current:
            plan["write"].append(entry)
        elif current == match.tier:
            plan["already_set"].append(entry)
        elif overwrite_conflicts:
            plan["write"].append(entry)
            plan["differs"].append(entry)
        else:
            plan["differs"].append(entry)
    return plan


def fetch_records(base_id: str, table: str, token: str, *, session: Any = requests) -> List[Dict[str, Any]]:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    records: List[Dict[str, Any]] = []
    params: Dict[str, str] = {}
    while True:
        response = session.get(url, headers=headers, params=params, timeout=30)
        if response.status_code >= 400:
            raise RuntimeError(f"Airtable list failed {response.status_code}: {response.text[:500]}")
        data = response.json()
        records.extend(data.get("records") or [])
        offset = data.get("offset")
        if not offset:
            return records
        params = {"offset": offset}


def _patch_with_retry(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    *,
    session: Any,
    max_retries: int,
    backoff_seconds: float,
) -> Dict[str, Any]:
    for attempt in range(1, max_retries + 2):
        response = session.patch(url, headers=headers, json=payload, timeout=30)
        if response.status_code < 400:
            return response.json()
        if response.status_code not in TRANSIENT_HTTP_STATUSES or attempt > max_retries:
            raise RuntimeError(f"Airtable PATCH failed {response.status_code}: {response.text[:500]}")
        sleeping = backoff_seconds * (2 ** (attempt - 1))
        print(
            f"[RETRY] attempt={attempt} status={response.status_code} sleeping={sleeping}s",
            file=sys.stderr,
        )
        time.sleep(sleeping)
    raise RuntimeError("Airtable PATCH exhausted retries without a response")


def apply_backfill(
    base_id: str,
    table: str,
    token: str,
    writes: Sequence[Mapping[str, Any]],
    *,
    receipts_path: Path | None = None,
    session: Any = requests,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    sleep_seconds: float = AIRTABLE_MIN_REQUEST_INTERVAL_SECONDS,
) -> int:
    """PATCH `Packet Tier` in batches of 10, appending a receipt per applied record.

    Receipts are flushed after every batch so an interrupted run says exactly
    which records already landed instead of leaving the base in an unknown state.
    """
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    applied = 0
    handle = receipts_path.open("a", encoding="utf-8") if receipts_path else None
    try:
        for start in range(0, len(writes), AIRTABLE_MAX_BATCH):
            chunk = list(writes[start : start + AIRTABLE_MAX_BATCH])
            payload = {
                "records": [
                    {"id": entry["record_id"], "fields": {TIER_FIELD: entry["tier"]}}
                    for entry in chunk
                ]
            }
            _patch_with_retry(
                url,
                headers,
                payload,
                session=session,
                max_retries=max_retries,
                backoff_seconds=backoff_seconds,
            )
            applied += len(chunk)
            if handle is not None:
                for entry in chunk:
                    handle.write(
                        json.dumps(
                            {
                                "record_id": entry["record_id"],
                                "place_key": entry["place_key"],
                                "tier": entry["tier"],
                                "source": entry.get("source", ""),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                handle.flush()
            print(f"[SYNC] PATCH {len(chunk)} records ({applied}/{len(writes)})")
            if sleep_seconds > 0 and start + AIRTABLE_MAX_BATCH < len(writes):
                time.sleep(sleep_seconds)
    finally:
        if handle is not None:
            handle.close()
    return applied


def print_plan(plan: Mapping[str, Sequence[Mapping[str, Any]]], *, verbose: bool) -> None:
    counts = {bucket: len(entries) for bucket, entries in plan.items()}
    print("[PLAN] " + "  ".join(f"{bucket}={count}" for bucket, count in counts.items()))

    tier_counts: Dict[str, int] = {}
    for entry in plan["write"]:
        tier_counts[entry["tier"]] = tier_counts.get(entry["tier"], 0) + 1
    if tier_counts:
        print("[PLAN] tiers to write: " + ", ".join(f"{tier}={count}" for tier, count in sorted(tier_counts.items())))

    for entry in plan["differs"]:
        print(
            f"[DIFFERS] {entry['business_name'][:40]!r} base={entry['current']!r} "
            f"csv={entry.get('tier')!r} from {entry.get('source')}",
            file=sys.stderr,
        )
    conflicted = [entry for entry in plan["write"] if entry.get("conflicts")]
    for entry in conflicted:
        print(
            f"[CONFLICT] {entry['business_name'][:40]!r} using {entry['tier']!r} from "
            f"{entry['source']}; older CSVs said {', '.join(entry['conflicts'])}",
            file=sys.stderr,
        )
    if verbose:
        for entry in plan["write"]:
            print(f"    WRITE {entry['place_key']} {entry['tier']!r} <- {entry['source']} ({entry['business_name'][:40]})")


def main(argv: List[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv-dir", default="output", help="Directory of scored CSVs to resolve tiers from")
    parser.add_argument("--live", action="store_true", help="Apply the writes (default is a dry run)")
    parser.add_argument(
        "--overwrite-conflicts",
        action="store_true",
        help="Also overwrite records whose existing tier differs from the CSV (default: report only)",
    )
    parser.add_argument(
        "--place-key",
        action="append",
        default=[],
        metavar="KEY",
        help="Restrict writes to this Place Key (repeatable). Use when only some tiers are trusted.",
    )
    parser.add_argument("--receipts", default="", help="Append applied writes to this JSONL path")
    parser.add_argument("--verbose", action="store_true", help="List every planned write")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    args = parser.parse_args(argv)

    if not (args.base_id and args.table and args.token):
        print(
            "[ERROR] AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required",
            file=sys.stderr,
        )
        return 1

    csv_dir = Path(args.csv_dir)
    if not csv_dir.is_dir():
        print(f"[ERROR] Not a directory: {csv_dir}", file=sys.stderr)
        return 1

    paths = csv_paths_newest_first(csv_dir)
    resolved = resolve_tiers_from_csvs(paths)
    print(f"[INFO] Scanned {len(paths)} CSVs; resolved {len(resolved)} place keys")

    try:
        records = fetch_records(args.base_id, args.table, args.token)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[INFO] Base records: {len(records)}")

    plan = plan_backfill(
        records,
        resolved,
        overwrite_conflicts=args.overwrite_conflicts,
        only_place_keys=args.place_key or None,
    )
    print_plan(plan, verbose=args.verbose)

    if not args.live:
        print("[DRY-RUN] No writes sent. Re-run with --live to apply.")
        return 0

    if not plan["write"]:
        print("[DONE] Nothing to write.")
        return 0

    receipts_path = Path(args.receipts) if args.receipts else None
    if receipts_path is not None:
        receipts_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        applied = apply_backfill(
            args.base_id,
            args.table,
            args.token,
            plan["write"],
            receipts_path=receipts_path,
        )
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[DONE] Applied {applied} Packet Tier writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
