#!/usr/bin/env python3
"""Re-derive every verdict from stored evidence and diff it against the base.

Two questions, one recompute:

1. *Does the base still hold what the code would decide today?* Any answer but
   "yes" means a row in front of a rep was decided by a version of the gate that
   no longer exists.
2. *Did a refactor change a decision?* `--snapshot-out` writes the recomputed
   verdict for every lead; `--baseline` diffs against one and fails on any
   change. That diff is the gate on a collapse that is supposed to be a pure
   projection: run it before, run it after, require an empty diff.

The verdict is never spelled here. It is recomputed by calling the same
`evaluate_sendability` + `evaluate_packet_tier` the pipeline calls, so a copy of
the rules cannot drift out from under the check that exists to catch drift.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lead_machine import sendability  # noqa: E402
from lead_machine import verdict as verdict_module  # noqa: E402
from lead_machine.score_schema import (  # noqa: E402
    CSV_SCORE_ALIASES,
    SCORE_SCHEMA_VERSION,
)


DEFAULT_PATTERN = "scored_*.csv"
# The base column this eval reads, and the vocabulary `recompute_verdict`
# answers in, have to be the same one or every row compares unequal. `Packet
# Tier` is deleted by step 4 of the verdict cutover; `Verdict` is the
# machine-owned column that replaced it.
DEFAULT_VERDICT_FIELD = verdict_module.VERDICT_FIELD
FINDING_CATEGORIES = ("snapshot_drift", "stale_evidence", "verdict_drift")
# `unverifiable` is reported, never failed: base rows with no scored evidence are
# the completeness eval's finding, and two gates red for one defect teaches
# people to ignore both.
REPORTED_CATEGORIES = ("unverifiable",)
SCHEMA_VERSION_ALIASES = CSV_SCORE_ALIASES["score_schema_version"]

REMEDIES = {
    "verdict_drift": (
        "The gate changed its mind about a lead whose evidence is current. "
        "Investigate before re-uploading — this is a decision change, not staleness."
    ),
    "stale_evidence": (
        "These rows were scored before schema v"
        f"{SCORE_SCHEMA_VERSION} and cannot be re-derived. "
        "Re-run the pipeline upload so the base holds what the current gate decides."
    ),
    "snapshot_drift": (
        "A recomputed verdict differs from the baseline snapshot. If this ran "
        "across a refactor that was meant to change no decision, it changed one."
    ),
}


def _set_csv_field_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("fields")
    return value if isinstance(value, dict) else {}


def _record_id(record: dict[str, Any]) -> str:
    return _text(record.get("id")) or "<missing-record-id>"


def schema_version(row: dict[str, Any]) -> int:
    """The score-schema version a scored row was written under.

    Rows predating the version column report 0 — the honest answer, since the
    columns the current gate reads were not written at all.
    """
    for alias in SCHEMA_VERSION_ALIASES:
        raw = _text(row.get(alias))
        if raw:
            try:
                return int(float(raw))
            except ValueError:
                return 0
    return 0


def recompute_verdict(row: dict[str, Any]) -> str:
    """The verdict the pipeline would write for *row* today.

    Still routed through the tier, because `evaluate_packet_tier` is the
    authority and `TIER_VERDICTS` is a bijection over it — the rename cannot
    change which leads are sendable, only what the answer is called.
    """
    result = sendability.evaluate_sendability(row)
    tier, _reason = sendability.evaluate_packet_tier(row, result)
    return sendability.TIER_VERDICTS[tier]


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
                row["_source"] = f"{path.name}:{row_number}"
                rows.append(row)
    return rows, count


def _select_evidence(
    rows: Sequence[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Pick the row that judges each lead: newest schema first, then last seen.

    Schema version leads the ordering because only a current-schema row can be
    re-derived at all — an older duplicate would report a lead as unreproducible
    when reproducible evidence for it exists.
    """
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        place_key = _text(row.get("Place Key"))
        if not place_key:
            continue
        version = schema_version(row)
        current = chosen.get(place_key)
        if current is None or version >= current["schema_version"]:
            chosen[place_key] = {
                "row": row,
                "schema_version": version,
                "source": row["_source"],
                "business_name": _text(row.get("Business Name")),
            }
    return chosen


def evaluate_reproducibility(
    scored_files: Sequence[Path],
    base_records: Iterable[dict[str, Any]],
    *,
    verdict_field: str = DEFAULT_VERDICT_FIELD,
    recompute: Callable[[dict[str, Any]], str] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = [Path(path) for path in scored_files]
    if not paths:
        raise ValueError("no scored CSV files matched")
    recompute_fn = recompute or recompute_verdict
    scored_rows, scored_row_count = _read_scored_rows(paths)
    records = list(base_records)

    blank_scored_keys = sum(
        1 for row in scored_rows if not _text(row.get("Place Key"))
    )
    evidence = _select_evidence(scored_rows)

    verdicts: dict[str, dict[str, Any]] = {}
    for place_key in sorted(evidence):
        entry = evidence[place_key]
        verdicts[place_key] = {
            "verdict": recompute_fn(entry["row"]),
            "schema_version": entry["schema_version"],
            "source": entry["source"],
        }

    findings: dict[str, list[dict[str, Any]]] = {
        category: [] for category in FINDING_CATEGORIES + REPORTED_CATEGORIES
    }
    matched = 0
    for record in sorted(records, key=_record_id):
        fields = _fields(record)
        place_key = _text(fields.get("Place Key"))
        business_name = _text(fields.get("Business Name"))
        record_id = _record_id(record)
        if not place_key or place_key not in verdicts:
            findings["unverifiable"].append(
                {
                    "place_key": place_key,
                    "business_name": business_name,
                    "record_id": record_id,
                }
            )
            continue
        recomputed = verdicts[place_key]
        base_verdict = _text(fields.get(verdict_field))
        if base_verdict == recomputed["verdict"]:
            matched += 1
            continue
        category = (
            "verdict_drift"
            if recomputed["schema_version"] >= SCORE_SCHEMA_VERSION
            else "stale_evidence"
        )
        findings[category].append(
            {
                "place_key": place_key,
                "business_name": business_name,
                "record_id": record_id,
                "base_verdict": base_verdict,
                "recomputed_verdict": recomputed["verdict"],
                "schema_version": recomputed["schema_version"],
                "source": recomputed["source"],
            }
        )

    if baseline is not None:
        for place_key in sorted(set(baseline) | set(verdicts)):
            before = _text((baseline.get(place_key) or {}).get("verdict"))
            after = _text((verdicts.get(place_key) or {}).get("verdict"))
            if before != after:
                findings["snapshot_drift"].append(
                    {
                        "place_key": place_key,
                        "baseline_verdict": before,
                        "recomputed_verdict": after,
                    }
                )

    finding_counts = {
        category: len(findings[category])
        for category in FINDING_CATEGORIES + REPORTED_CATEGORIES
    }
    failed = {
        category: count
        for category, count in finding_counts.items()
        if category in FINDING_CATEGORIES and count
    }
    return {
        "ok": not failed,
        "verdict_field": verdict_field,
        "schema_version": SCORE_SCHEMA_VERSION,
        "counts": {
            "scored_files": len(paths),
            "scored_rows": scored_row_count,
            "blank_scored_keys": blank_scored_keys,
            "distinct_scored_leads": len(verdicts),
            "base_rows": len(records),
            "matched": matched,
            "findings": finding_counts,
        },
        "remedies": {category: REMEDIES[category] for category in sorted(failed)},
        "findings": findings,
        "verdicts": verdicts,
    }


def fetch_base_records(
    base_id: str,
    table: str,
    token: str,
    *,
    verdict_field: str = DEFAULT_VERDICT_FIELD,
) -> list[dict[str, Any]]:
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
        records.extend(record for record in page["records"] if isinstance(record, dict))
        offset = _text(page.get("offset"))
        if not offset:
            return records


def _credentials(base_id: str, table: str, token: str) -> tuple[str, str, str]:
    load_dotenv()
    resolved_base = base_id or os.getenv("AIRTABLE_BASE_ID", "")
    resolved_table = (
        table
        or os.getenv("AIRTABLE_PROSPECTS_TABLE", "")
        or os.getenv("AIRTABLE_TABLE", "")
        or "Prospects"
    )
    resolved_token = (
        token or os.getenv("AIRTABLE_PAT", "") or os.getenv("AIRTABLE_API_KEY", "")
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


def _load_baseline(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("verdicts"), dict):
        value = value["verdicts"]
    if not isinstance(value, dict):
        raise ValueError("baseline snapshot must be an object keyed by Place Key")
    return value


def run_eval(
    *,
    output_dir: Path = Path("output"),
    pattern: str = DEFAULT_PATTERN,
    scored_files: Sequence[Path] | None = None,
    base_records: Iterable[dict[str, Any]] | None = None,
    base_id: str = "",
    table: str = "",
    token: str = "",
    verdict_field: str = DEFAULT_VERDICT_FIELD,
    baseline: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    paths = (
        [Path(path) for path in scored_files]
        if scored_files is not None
        else sorted(Path(output_dir).glob(pattern))
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
    report = evaluate_reproducibility(
        paths,
        base_records,
        verdict_field=verdict_field,
        baseline=baseline,
    )
    return (0 if report["ok"] else 2), report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-derive every verdict from stored evidence (read-only)."
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--glob", default=DEFAULT_PATTERN)
    parser.add_argument("--scored-file", type=Path, action="append")
    parser.add_argument("--base-records-json", type=Path)
    parser.add_argument("--base-id", default="")
    parser.add_argument("--table", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--verdict-field", default=DEFAULT_VERDICT_FIELD)
    parser.add_argument(
        "--snapshot-out",
        type=Path,
        help="Write the recomputed verdict for every lead, for a later --baseline diff.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Fail if any recomputed verdict differs from this snapshot.",
    )
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
            baseline=_load_baseline(args.baseline) if args.baseline else None,
        )
        if args.snapshot_out:
            args.snapshot_out.parent.mkdir(parents=True, exist_ok=True)
            args.snapshot_out.write_text(
                json.dumps(report["verdicts"], ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
                encoding="utf-8",
            )
    except requests.RequestException as exc:
        code = 2
        report = {"ok": False, "error": f"Airtable GET failed ({type(exc).__name__})"}
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        code = 2
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
