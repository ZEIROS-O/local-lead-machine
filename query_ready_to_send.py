#!/usr/bin/env python3
"""Read-only Airtable query for records ready to send.

Built for the ZEIROS `send-queue` skill: pulls LPM prospects that pass the
canonical readiness contract and are currently New,
so the daily send queue can draft against real pipeline state instead of
stale exports. Never writes anything (see mark_sent.py for the write-back
half of this loop).

Suppression is applied here as well as at send time. The pipeline's three
fail-closed gates (search, enrichment, scoring) stop a *re-discovered*
opted-out lead before it ever reaches the base, but they cannot touch a record
already in Airtable when the opt-out is logged — a duplicate of the same
business under a different record id is exactly what `lead_machine/corpus_index.py`
exists because of. mark_sent refuses such a record, but only after the rep has been
handed it and dialled it by hand, which is the call the ledger was supposed to
prevent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import AbstractSet, Any, Dict, List
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine.airtable_sync import resolve_vertical_label
from lead_machine.compliance import (
    ComplianceError,
    is_mark_sent_source_status,
    is_suppressed,
    load_configured_suppression_index,
)
from lead_machine import queue_state
from lead_machine.dial_rank import dial_rank, dial_sort_key, rank_components
from lead_machine.sendability import is_contact_ready


# Requested from Airtable by name, so this list cannot outlive the columns: a
# `fields[]` naming a deleted field is a 422, not a blank. The seven the cutover
# removes are gone from here in the same change that adds Verdict, which is why
# the runbook can delete them without the morning queue going down.
DISPLAY_FIELDS = [
    "Verdict",
    "Verdict Reasons",
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
    "Buyer Intent Signals",
    "Visible Pain Score",
    "Send Trigger",
    "Offer Fit",
    "Specific Observation",
    "Site Status",
    "Opener",
    "Safe Outreach Claims",
    "Evidence Refs",
    "LPM Offer Component",
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

# The fields the read-back guard must actually receive to mean anything: a guard
# whose evidence never arrives is one that cannot fail. `Verdict` and `Status`
# answer the send question after the cutover collapsed it into one column.
#
# `Phone` and `Email` joined them when suppression started being applied here.
# They are what `record_matches_suppression` matches on, so letting the schema
# probe drop them degrades the opt-out check to a record-id-only comparison,
# which by construction misses a suppressed business re-discovered under a new
# record — the case the ledger exists for. That must not happen behind a warning
# that reads as cosmetic; if they are absent the query fails rather than serving
# a queue it cannot screen.
#
# `Reviews` joined them when `contact_readiness_failures` started re-deriving
# reachability from the projection instead of trusting a stored verdict. The
# failure mode is worse here than for the others, because a dropped column is
# not absent — the row rebuild below reads `DISPLAY_FIELDS`, not `requested`, so
# every record comes back carrying `Reviews: ""`. An absent review count now
# fails the gate, so one 422 on this column refuses *every* lead at once and
# serves an empty queue behind a schema warning. While it only fed the rank it
# degraded the sort; now it can decide the answer, so it has to fail loudly.
# `tests/test_verdict.py` derives this set from the gate rather than trusting
# this list, so a future gate input cannot be added without protecting it.
REQUIRED_READINESS_FIELDS = {
    "Verdict",
    "Status",
    "Phone",
    "Email",
    "Reviews",
}

# The columns `dial_rank` actually reads. Declared here because the fetch and
# the ranker are the two halves of one contract and nothing else joined them:
# `Visible Pain Score` — the single largest weight — was absent from
# DISPLAY_FIELDS while dial_rank asked for it, so every lead scored zero pain
# and the queue served an order nobody chose. The unit tests could not see it,
# because they hand dial_rank a full scored row rather than the projection onto
# DISPLAY_FIELDS that is the only thing this query can ever produce.
#
# Not added to REQUIRED_READINESS_FIELDS: a missing rank input degrades the
# order, it does not make the queue unsafe to serve, and a hard failure here
# would take the morning queue down over a sort. It warns in its own right
# instead, because the generic schema warning reads as cosmetic.
RANK_INPUT_FIELDS = {
    "Visible Pain Score",
    "Buyer Intent Signals",
    "Reviews",
    "Rating",
    "AI Confidence",
    "Evidence Refs",
    "Score Total",
    "Lead Priority",
}


def airtable_base_url(base_id: str, table: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"


def build_formula(vertical: str) -> str:
    formula = f"AND({queue_state.QUEUE_FILTER_CLAUSES}"
    if vertical:
        escaped = vertical.replace("'", "\\'")
        formula += f",{{Vertical}}='{escaped}'"
    formula += ")"
    return formula


_UNKNOWN_FIELD_RE = re.compile(r'Unknown field name: \\?"(.+?)\\?"')


def fetch_ready_records(
    base_id: str,
    table: str,
    token: str,
    *,
    suppression: AbstractSet[str],
    vertical: str = "",
) -> List[Dict[str, Any]]:
    """Fetch the send queue. `suppression` is required, never defaulted.

    A default would make "no index" indistinguishable from "checked and clear"
    at every call site — the same fail-open this module's other guards are
    written against. `main()` resolves the real index and fails closed if the
    environment names none.
    """
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}

    # A column that exists in DISPLAY_FIELDS but not (yet) in the base 422s the
    # whole query. Keep the queue available before the schema rollout, but make
    # every missing expected field explicit once the probe has finished.
    requested = list(DISPLAY_FIELDS)
    missing_display_fields: List[str] = []
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
            missing_display_fields.append(match.group(1))
            requested.remove(match.group(1))
            continue
        raise RuntimeError(f"Airtable list failed {probe.status_code}: {probe.text[:1000]}")

    if missing_display_fields:
        missing = ", ".join(repr(field) for field in missing_display_fields)
        print(
            "[WARN] Airtable schema mismatch: expected display field(s) missing: "
            f"{missing}. Continuing without them for this read-only queue query; "
            "update the Airtable schema or DISPLAY_FIELDS.",
            file=sys.stderr,
        )
        degraded = sorted(set(missing_display_fields) & RANK_INPUT_FIELDS)
        if degraded:
            print(
                "[WARN] dial-queue ordering is degraded: rank input(s) "
                f"{', '.join(repr(field) for field in degraded)} are missing from the "
                "base, so their weight is dead and the queue order is not the one "
                "dial_rank describes.",
                file=sys.stderr,
            )

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
            if not is_contact_ready(row):
                print(
                    f"[WARN] Airtable record {row['record_id'] or '<missing-id>'} failed defensive commercial readiness check",
                    file=sys.stderr,
                )
                continue
            if not is_mark_sent_source_status(row.get("Status")):
                print(
                    f"[WARN] Airtable record {row['record_id'] or '<missing-id>'} failed defensive pre-send status check",
                    file=sys.stderr,
                )
                continue
            if suppression and is_suppressed(row, suppression):
                print(
                    f"[SKIP] Airtable record {row['record_id'] or '<missing-id>'} is "
                    "suppressed (opt-out); excluded from the send queue",
                    file=sys.stderr,
                )
                continue
            records.append(row)
        offset = data.get("offset")
        if not offset:
            break

    # Grade first, score second was the old order, and while grade was also a
    # gate that was defensible: everything in the queue had already cleared A/B,
    # so the sort only separated leads the gate had blessed. Grade is no longer a
    # gate — a C lead with a real leak and a dialable number now reaches the
    # queue — so sorting on it first would put the model's holistic guess ahead
    # of the evidence the rep actually pitches on.
    #
    # `dial_rank` reads the signals the demoted gates used to enforce by
    # exclusion, so the ordering they were protecting survives them.
    records.sort(key=dial_sort_key)

    # Emitted alongside the Airtable columns, lower-cased like `record_id` to
    # mark it as computed here rather than read from the base. Two reasons it
    # ships rather than staying implicit in the sort: an order nobody can
    # interrogate is one nobody trusts, and `mark_sent`/`mark_declined` record
    # it with the rep's decision, which is the only way to find out whether a
    # high rank actually predicts a lead worth calling.
    for row in records:
        row["dial_rank"] = dial_rank(row)
        row["dial_rank_components"] = rank_components(row)
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

    # Fails closed, exactly as search, enrichment and scoring do: an unresolved
    # index is an unsafe state, not an empty one. ALLOW_MISSING_SUPPRESSION_INDEX
    # is the loud bypass.
    try:
        suppression = load_configured_suppression_index()
    except ComplianceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    try:
        records = fetch_ready_records(
            args.base_id, args.table, args.token, suppression=suppression, vertical=vertical
        )
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.limit:
        records = records[: args.limit]

    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
