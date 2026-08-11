#!/usr/bin/env python3
"""Compute the accept-rate KPI from the human decisions in the base.

AGENTS.md names this the KPI — "below ~50% the loop is losing" — and until now
nothing computed it. The one existing consumer, `feedback_report.py`, reads a
hand-maintained feedback CSV that no stage of the pipeline writes, so scheduling
it would have scheduled a report on a file that never exists.

The decisions do exist: `Prospects.Status` is human-owned, and every rep action
lands there. The rate is therefore read straight off the base —

    accept rate = accepted / (accepted + rejected)

over the leads the machine actually proposed (the sendable tier). Leads still in
the queue are undecided and stay out of the denominator; counting them would
report the machine's own proposals back as approvals.

Below the floor fails. Too few decisions to judge reports `insufficient-sample`
and says so — it never prints PASS, because a KPI with no data behind it reading
green is the same defect as a gate that cannot fail.

What this does *not* measure. `mark_sent.py` sets `Follow-Up`, and `Follow-Up`
is an accepted status, so logging a dial creates an acceptance. A rep who works
the queue in order scores 100% here whatever the leads are worth. This is an
operational check that proposals are being actioned, not evidence about lead
quality, and it cannot say whether demoting the commercial tribunal cost us
anything: a lead the rep silently skipped never reaches the denominator.
`evals/pre_dial_approval.py` measures the decision made before the dial and is
the one to read for that question.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lead_machine.queue_state import (  # noqa: E402
    ACCEPTED_STATUSES,
    REJECTED_STATUSES,
    UNDECIDED_STATUSES,
)
from lead_machine import verdict as verdict_module  # noqa: E402


# Step 4 of the verdict cutover deleted `Packet Tier` from the base, so a read
# still naming it 422s on every run. Both other base-reading evals were migrated
# in dc3bbae and this one was missed, because its test pinned the old name in a
# fixture rather than asserting against `verdict`. Derived, never spelled, so the
# next rename cannot leave a third eval behind.
DEFAULT_VERDICT_FIELD = verdict_module.VERDICT_FIELD
DEFAULT_PROPOSED_VERDICT = verdict_module.VERDICT_SEND
STATUS_FIELD = "Status"
# AGENTS.md: "KPI = accept-rate; below ~50% the loop is losing."
ACCEPT_RATE_FLOOR = 0.5
# docs/phase-b-c-roadmap.md graduates autonomy at "20+ approved touch-1s"; fewer
# decisions than that cannot separate a losing loop from a small sample.
MINIMUM_DECIDED = 20


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("fields")
    return value if isinstance(value, dict) else {}


def _record_id(record: dict[str, Any]) -> str:
    return _text(record.get("id")) or "<missing-record-id>"


def evaluate_accept_rate(
    base_records: Iterable[dict[str, Any]],
    *,
    verdict_field: str = DEFAULT_VERDICT_FIELD,
    proposed_verdict: str = DEFAULT_PROPOSED_VERDICT,
    floor: float = ACCEPT_RATE_FLOOR,
    minimum_decided: int = MINIMUM_DECIDED,
) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    undecided = 0
    unknown: list[dict[str, Any]] = []
    proposed = 0

    for record in base_records:
        fields = _fields(record)
        if _text(fields.get(verdict_field)) != proposed_verdict:
            continue
        proposed += 1
        status = _text(fields.get(STATUS_FIELD))
        entry = {
            "status": status,
            "business_name": _text(fields.get("Business Name")),
            "record_id": _record_id(record),
        }
        if status in ACCEPTED_STATUSES:
            accepted.append(entry)
        elif status in REJECTED_STATUSES:
            rejected.append(entry)
        elif status in UNDECIDED_STATUSES:
            undecided += 1
        else:
            # Never silently undecided: an unclassified value would leave the
            # denominator and quietly inflate the rate.
            unknown.append(entry)

    decided = len(accepted) + len(rejected)
    rate = (len(accepted) / decided) if decided else None

    if unknown:
        status = "unknown-status"
        summary = (
            f"- FAIL: accept-rate — {len(unknown)} lead(s) hold a Status outside the "
            "accept/reject/undecided partition"
        )
    elif decided < minimum_decided:
        status = "insufficient-sample"
        summary = (
            f"- NO VERDICT: accept-rate — only {decided} decided lead(s) of "
            f"{proposed} proposed; {minimum_decided} needed to judge"
        )
    elif rate is not None and rate < floor:
        status = "below-floor"
        summary = (
            f"- FAIL: accept-rate {rate:.1%} is below the {floor:.0%} floor "
            f"({len(accepted)} accepted / {decided} decided)"
        )
    else:
        status = "pass"
        summary = (
            f"- PASS: accept-rate {rate:.1%} "
            f"({len(accepted)} accepted / {decided} decided)"
        )

    return {
        "ok": status in {"pass", "insufficient-sample"},
        "status": status,
        "summary": summary,
        "accept_rate": rate,
        "floor": floor,
        "minimum_decided": minimum_decided,
        "verdict_field": verdict_field,
        "proposed_verdict": proposed_verdict,
        "counts": {
            "proposed": proposed,
            "accepted": len(accepted),
            "rejected": len(rejected),
            "undecided": undecided,
            "decided": decided,
            "unknown_statuses": len(unknown),
        },
        "findings": {
            "unknown_statuses": sorted(
                unknown, key=lambda item: (item["status"], item["record_id"])
            )
        },
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
            ("fields[]", STATUS_FIELD),
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute the accept-rate KPI from Airtable Status (read-only)."
    )
    parser.add_argument("--base-records-json", type=Path)
    parser.add_argument("--base-id", default="")
    parser.add_argument("--table", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--verdict-field", default=DEFAULT_VERDICT_FIELD)
    parser.add_argument("--floor", type=float, default=ACCEPT_RATE_FLOOR)
    parser.add_argument("--minimum-decided", type=int, default=MINIMUM_DECIDED)
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Append the one-line verdict here (the CI job summary).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.base_records_json:
            records = _load_fixture_records(args.base_records_json)
        else:
            base_id, table, token = _credentials(args.base_id, args.table, args.token)
            records = fetch_base_records(
                base_id, table, token, verdict_field=args.verdict_field
            )
        report = evaluate_accept_rate(
            records,
            verdict_field=args.verdict_field,
            floor=args.floor,
            minimum_decided=args.minimum_decided,
        )
        code = 0 if report["ok"] else 2
    except requests.RequestException as exc:
        code = 2
        report = {
            "ok": False,
            "error": f"Airtable GET failed ({type(exc).__name__})",
            "summary": "- FAIL: accept-rate — could not read the base",
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        code = 2
        report = {
            "ok": False,
            "error": str(exc),
            "summary": f"- FAIL: accept-rate — {exc}",
        }
    if args.summary_file:
        with args.summary_file.open("a", encoding="utf-8") as handle:
            handle.write(report["summary"] + "\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
