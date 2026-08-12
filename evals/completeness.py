#!/usr/bin/env python3
"""Read-only completeness check between scored CSVs and Airtable Prospects.

**Reject-verdict leads are deliberately pruned from the CRM.** Confirmed by the
operator; it is why the 2026-08-11 upload receipt records 1,084 records written
against a base holding 462 with zero rejects. Every pruned reject is therefore a
permanent orphan, and `scored_orphans = scored_keys - base_keys` counted all of
them — most of the 503 it reported. A check that can only go green by uploading
leads the operator prunes on purpose is measuring the wrong invariant.

The invariant that matters is narrower: **every lead the current gate calls
actionable must exist in the base.** So a scored lead missing from the base is an
orphan only when its verdict is something other than `reject`; pruned rejects are
counted separately as `pruned_rejects` and never fail the run. Do not "fix" this
back to the set difference.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lead_machine import verdict as verdict_module  # noqa: E402


from evals.corpus import (  # noqa: E402
    DEFAULT_PATTERN,
    CorpusManifestError,
    describe,
    resolve_scored_paths,
)

# Two sides, two vocabularies. The base stores `Verdict` after the verdict
# cutover and step 4 deletes `Packet Tier` outright, so a base read still naming
# the tier 422s. Every scored CSV carries the tier column, so the corpus-conflict
# check keeps reading that one — it is the only column present across the whole
# corpus.
#
# `Verdict` is *not* absent from the CSVs, as this comment used to claim: 5 of
# the 14 active corpora carry it, the ones written since the cutover. The
# reject-pruning below reads it deliberately, and treats a blank as "not provably
# a pruned reject" — so the 9 older corpora keep their missing leads in
# `scored_orphans` rather than having them silently excused.
DEFAULT_VERDICT_FIELD = verdict_module.VERDICT_FIELD
SCORED_VERDICT_FIELD = "Packet Tier"
FINDING_CATEGORIES = (
    "base_orphans",
    "blank_base_keys",
    "blank_base_verdicts",
    "blank_scored_keys",
    "corpus_conflicts",
    "duplicate_base_keys",
    "scored_orphans",
)
# Counted and listed, never failed — see the module docstring. Kept out of
# FINDING_CATEGORIES because that tuple is what `ok` is computed from.
REPORTED_CATEGORIES = ("pruned_rejects",)
PRUNED_VERDICT = "reject"


def _set_csv_field_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _source(path: Path, row_number: int) -> str:
    return f"{path.name}:{row_number}"


def _read_scored_rows(paths: Sequence[Path]) -> tuple[list[dict[str, str]], int]:
    _set_csv_field_limit()
    rows: list[dict[str, str]] = []
    count = 0
    for path in sorted(paths, key=lambda item: (item.name, str(item))):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_number, raw in enumerate(reader, start=2):
                count += 1
                row = {
                    str(key): _text(value)
                    for key, value in raw.items()
                    if key is not None
                }
                row["_source"] = _source(path, row_number)
                rows.append(row)
    return rows, count


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("fields")
    return value if isinstance(value, dict) else {}


def _record_id(record: dict[str, Any]) -> str:
    return _text(record.get("id")) or "<missing-record-id>"


def _corpus_conflicts(
    scored_by_key: dict[str, list[dict[str, str]]],
    verdict_field: str,
) -> list[dict[str, Any]]:
    conflict_fields = ("Business Name", "City", verdict_field)
    findings: list[dict[str, Any]] = []
    for place_key in sorted(scored_by_key):
        rows = scored_by_key[place_key]
        fields: dict[str, list[dict[str, str]]] = {}
        for field in conflict_fields:
            observations = [
                {"source": row["_source"], "value": _text(row.get(field))}
                for row in rows
            ]
            distinct = {observation["value"] for observation in observations}
            if len(distinct) > 1:
                fields[field] = sorted(
                    observations,
                    key=lambda item: (item["source"], item["value"]),
                )
        if fields:
            findings.append({"place_key": place_key, "fields": fields})
    return findings


def evaluate_completeness(
    scored_files: Sequence[Path],
    base_records: Iterable[dict[str, Any]],
    *,
    verdict_field: str = DEFAULT_VERDICT_FIELD,
) -> dict[str, Any]:
    """Join the scored corpus to already-loaded Airtable rows on ``Place Key``."""
    paths = [Path(path) for path in scored_files]
    if not paths:
        raise ValueError("no scored CSV files matched")
    scored_rows, scored_row_count = _read_scored_rows(paths)
    records = list(base_records)

    findings: dict[str, list[dict[str, Any]]] = {
        category: [] for category in (*FINDING_CATEGORIES, *REPORTED_CATEGORIES)
    }
    scored_by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in scored_rows:
        place_key = _text(row.get("Place Key"))
        if not place_key:
            findings["blank_scored_keys"].append(
                {
                    "business_name": _text(row.get("Business Name")),
                    "city": _text(row.get("City")),
                    "source": row["_source"],
                }
            )
            continue
        scored_by_key[place_key].append(row)

    base_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        fields = _fields(record)
        place_key = _text(fields.get("Place Key"))
        business_name = _text(fields.get("Business Name"))
        record_id = _record_id(record)
        if not place_key:
            findings["blank_base_keys"].append(
                {"business_name": business_name, "record_id": record_id}
            )
        else:
            base_by_key[place_key].append(record)
        if not _text(fields.get(verdict_field)):
            findings["blank_base_verdicts"].append(
                {
                    "place_key": place_key,
                    "business_name": business_name,
                    "record_id": record_id,
                }
            )

    for place_key in sorted(base_by_key):
        matched = base_by_key[place_key]
        if len(matched) > 1:
            findings["duplicate_base_keys"].append(
                {
                    "place_key": place_key,
                    "records": sorted(
                        [
                            {
                                "business_name": _text(
                                    _fields(record).get("Business Name")
                                ),
                                "record_id": _record_id(record),
                            }
                            for record in matched
                        ],
                        key=lambda item: (item["record_id"], item["business_name"]),
                    ),
                }
            )

    scored_keys = set(scored_by_key)
    base_keys = set(base_by_key)
    for place_key in sorted(scored_keys - base_keys):
        rows = scored_by_key[place_key]
        verdicts = {_text(row.get(verdict_module.VERDICT_FIELD)).casefold() for row in rows}
        verdicts.discard("")
        finding = {
            "place_key": place_key,
            "business_name": _text(rows[0].get("Business Name")),
            "sources": sorted(row["_source"] for row in rows),
            "verdicts": sorted(verdicts),
        }
        # Only when *every* scoring of this lead says reject. A key with any
        # non-reject scoring is still an orphan: it fails toward reporting,
        # which is the direction a completeness check should fail.
        if verdicts == {PRUNED_VERDICT}:
            findings["pruned_rejects"].append(finding)
        else:
            findings["scored_orphans"].append(finding)
    for place_key in sorted(base_keys - scored_keys):
        for record in sorted(base_by_key[place_key], key=_record_id):
            fields = _fields(record)
            findings["base_orphans"].append(
                {
                    "place_key": place_key,
                    "business_name": _text(fields.get("Business Name")),
                    "record_id": _record_id(record),
                }
            )

    findings["corpus_conflicts"] = _corpus_conflicts(
        scored_by_key, SCORED_VERDICT_FIELD
    )
    for category in ("blank_base_keys", "blank_base_verdicts", "blank_scored_keys", "pruned_rejects"):
        findings[category].sort(
            key=lambda item: tuple(_text(item.get(key)) for key in sorted(item))
        )

    finding_counts = {
        category: len(findings[category]) for category in FINDING_CATEGORIES
    }
    return {
        "ok": not any(finding_counts.values()),
        "verdict_field": verdict_field,
        "counts": {
            "scored_files": len(paths),
            "scored_rows": scored_row_count,
            "distinct_scored_leads": len(scored_by_key),
            "base_rows": len(records),
            "findings": finding_counts,
            "reported": {
                category: len(findings[category]) for category in REPORTED_CATEGORIES
            },
        },
        "findings": findings,
    }


def fetch_base_records(
    base_id: str,
    table: str,
    token: str,
    *,
    verdict_field: str = DEFAULT_VERDICT_FIELD,
) -> list[dict[str, Any]]:
    """Load every Airtable row with the minimum fields needed by this eval."""
    url = f"https://api.airtable.com/v0/{quote(base_id)}/{quote(table, safe='')}"
    records: list[dict[str, Any]] = []
    offset = ""
    while True:
        params: list[tuple[str, Any]] = [
            ("pageSize", 100),
            ("fields[]", "Place Key"),
            ("fields[]", "Business Name"),
            ("fields[]", verdict_field),
        ]
        if offset:
            params.append(("offset", offset))
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise ValueError("Airtable records response must contain a records list")
        records.extend(
            record for record in page["records"] if isinstance(record, dict)
        )
        offset = _text(page.get("offset"))
        if not offset:
            return records


def _credentials(
    base_id: str,
    table: str,
    token: str,
) -> tuple[str, str, str]:
    load_dotenv()
    resolved_base = base_id or os.getenv("AIRTABLE_BASE_ID", "")
    resolved_table = (
        table
        or os.getenv("AIRTABLE_PROSPECTS_TABLE", "")
        or os.getenv("AIRTABLE_TABLE", "")
        or "Prospects"
    )
    resolved_token = (
        token
        or os.getenv("AIRTABLE_PAT", "")
        or os.getenv("AIRTABLE_API_KEY", "")
    )
    missing = [
        name
        for name, value in (
            ("AIRTABLE_BASE_ID", resolved_base),
            ("AIRTABLE_PAT", resolved_token),
        )
        if not value
    ]
    if missing:
        raise ValueError("missing Airtable configuration: " + ", ".join(missing))
    return resolved_base, resolved_table, resolved_token


def _load_fixture_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("records")
    if not isinstance(value, list) or not all(
        isinstance(record, dict) for record in value
    ):
        raise ValueError("base records fixture must be a list or an object with records")
    return value


def run_eval(
    *,
    output_dir: Path = Path("output"),
    pattern: str | None = None,
    scored_files: Sequence[Path] | None = None,
    base_records: Iterable[dict[str, Any]] | None = None,
    base_id: str = "",
    table: str = "",
    token: str = "",
    verdict_field: str = DEFAULT_VERDICT_FIELD,
) -> tuple[int, dict[str, Any]]:
    paths, corpus_source = resolve_scored_paths(
        output_dir=output_dir, pattern=pattern, scored_files=scored_files
    )
    if base_records is None:
        resolved_base, resolved_table, resolved_token = _credentials(
            base_id, table, token
        )
        base_records = fetch_base_records(
            resolved_base,
            resolved_table,
            resolved_token,
            verdict_field=verdict_field,
        )
    report = evaluate_completeness(
        paths, base_records, verdict_field=verdict_field
    )
    report["corpora"] = describe(paths, corpus_source)
    return (0 if report["ok"] else 2), report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check scored CSV coverage against Airtable Prospects (read-only)."
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--glob",
        default=None,
        help=(
            "Select corpora by glob instead of evals/active_corpora.json. "
            f"Unset means the manifest; pass {DEFAULT_PATTERN!r} to replay everything."
        ),
    )
    parser.add_argument("--scored-file", type=Path, action="append")
    parser.add_argument("--base-records-json", type=Path)
    parser.add_argument("--base-id", default="")
    parser.add_argument("--table", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--verdict-field", default=DEFAULT_VERDICT_FIELD)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        fixture_records = (
            _load_fixture_records(args.base_records_json)
            if args.base_records_json
            else None
        )
        code, report = run_eval(
            output_dir=args.output_dir,
            pattern=args.glob,
            scored_files=args.scored_file,
            base_records=fixture_records,
            base_id=args.base_id,
            table=args.table,
            token=args.token,
            verdict_field=args.verdict_field,
        )
    except requests.RequestException as exc:
        code = 2
        report = {
            "ok": False,
            "error": f"Airtable GET failed ({type(exc).__name__})",
        }
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        code = 2
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
