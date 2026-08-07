#!/usr/bin/env python3
"""Record a post-send funnel outcome for one Airtable prospect record.

The pipeline's only write-back after outreach used to be mark_sent.py, which
stops at Status=Follow-Up. Nothing recorded what happened after that: a reply, a
booked call, a closed-won deal, a closed-lost. This script is the write-back
for those four events — it never sends anything, only logs what a human
already observed, mirroring mark_sent.py's guarded-PATCH conventions.

Writes:
  - Prospects.Status -> Follow-Up (replied / meeting_booked), Won (closed_won), Lost (closed_lost)
  - Prospects.Next Follow-Up -> re-armed one cadence step out while the lead is
    still open, and cleared on a close so a closed lead leaves the follow-up queue
  - a new row in Sales Activities, linked back to the prospect
  - on closed_won only: a new row in Deals, linked back to the prospect

No write path here sets `typecast`, and none may. The Sales Activities create
used to, because that table's Activity Type/Outcome vocabularies were built for
meeting logistics (Completed/Scheduled/No Show) and Airtable's Meta API refuses
to add select choices to an existing field — typecast on write was the only
mechanism that could. The cost was a silent schema-drift channel: typecast makes
Airtable invent a choice for *whatever* string it is handed, so one mis-mapped
value minted a `Lead Priority` choice literally named `(440) 537-2459`.

Dropping typecast moves that obligation onto the base, and the base has not met
it yet. **Precondition, unmet as of 2026-07-29:** every choice in
`REQUIRED_SELECT_CHOICES` must already exist in the live base, and for
Sales Activities none of them do — `Activity Type` offers Call/Email/Meeting/
Follow Up/Demo/Note/Other (no Reply, no Close) and `Outcome` offers Completed/
Scheduled/Rescheduled/Cancelled/No Show/Outcome Pending (none of the four written
here). `Prospects.Status` now joins that list: this module writes `Won`/`Lost`
where it used to write a single `Closed`, because one closed state cannot say
which of the two happened, and the base still offers only `Closed`. Until a
human runs the Airtable runbook step that adds those choices by hand, **every**
mark_outcome event 422s — this script cannot record a single outcome. That
runbook step is a hard prerequisite, not a cleanup task. Restoring typecast is
not the fix; it is the drift channel this removal closed.

`REQUIRED_SELECT_CHOICES` states the exact choices this writer emits.
`OPERATOR_REQUIRED_SELECT_CHOICES` separately declares operator-marked values
such as `Held`, preserving the writer's exact-vocabulary invariant while giving
the schema gate one source for manual workflow requirements.
`preflight_select_values` checks the values a run is about to send against it
before the first request, so a value the declaration has not caught up with fails
by name instead of as an opaque Airtable 422. It cannot see the other direction —
a choice correctly declared here that the base has simply never been given — so
the 422 that case does produce is annotated with the choices the failed write
needed and a pointer to the runbook, rather than left as a raw API error.

`TARGET_STATUS`/`ACTIVITY_TYPE`/`ACTIVITY_OUTCOME` below are still the complete
set of values this script can ever emit, pinned by `tests/test_mark_outcome.py`
against `REQUIRED_SELECT_CHOICES` so a fifth event cannot be added without
declaring the choices it needs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from urllib.parse import quote

import requests

from lead_machine.compliance import normalize_airtable_status
from lead_machine.follow_up import next_follow_up_date

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False


OUTCOME_SOURCE_STATUSES = frozenset({"follow-up"})

# Won and Lost, not a shared Closed: the two closing events are the only place
# the pipeline ever learns whether the work converted, and collapsing them threw
# that away at the exact moment it was observed. Both values are part of
# queue_state.PROSPECT_STATUS_CHOICES, which is the canonical vocabulary.
TARGET_STATUS = {
    "replied": "Follow-Up",
    "meeting_booked": "Follow-Up",
    "closed_won": "Won",
    "closed_lost": "Lost",
}

ACTIVITY_TYPE = {
    "replied": "Reply",
    "meeting_booked": "Call",
    "closed_won": "Close",
    "closed_lost": "Close",
}

ACTIVITY_OUTCOME = {
    "replied": "Replied",
    "meeting_booked": "Meeting Booked",
    "closed_won": "Won",
    "closed_lost": "Lost",
}

# A close ends the outreach cadence; anything else re-arms it. Keyed off the
# same map as the status so a new event can never quietly skip this decision.
CLOSING_EVENTS = frozenset({"closed_won", "closed_lost"})

# A closed_won creates the Deal at the point the prospect said yes — before any
# money has moved. "Proposed" is the state of an outstanding proposal, not of a
# win; the deposit is the next thing that has to happen.
CLOSED_WON_DEAL_STATUS = "Deposit Pending"

OUTCOME_EVENTS = frozenset(TARGET_STATUS)

# Where the runbook's "create these choices" step is written down for a human.
RUNBOOK_DOC = "docs/airtable_schema_required.md"

# The error code Airtable returns for a select value that is not one of the
# field's choices. It is the expected failure for the whole window between this
# code shipping and the runbook running, so it must not read as an API fault.
UNKNOWN_CHOICE_ERROR_CODE = "INVALID_MULTIPLE_CHOICE_OPTIONS"

# The select choices the base must already offer for a mark_outcome write to
# succeed at all, keyed by the logical table and field that has to offer them.
# Spelled out rather than derived from the maps above on purpose: this is a
# statement about the *base*, so the runbook generator can import it verbatim
# and the test below can catch the maps growing past what has been declared.
# Keys are the default table names — the --*-table flags rename the table, not
# the field vocabulary a renamed table still has to carry.
REQUIRED_SELECT_CHOICES: dict[str, dict[str, frozenset[str]]] = {
    "Prospects": {
        "Status": frozenset({"Follow-Up", "Won", "Lost"}),
    },
    "Sales Activities": {
        "Activity Type": frozenset({"Reply", "Call", "Close"}),
        "Outcome": frozenset({"Replied", "Meeting Booked", "Won", "Lost"}),
    },
    "Deals": {
        "Deal Status": frozenset({"Deposit Pending"}),
    },
}

# Values required by the same CRM workflow but applied by a human rather than
# emitted by `apply_mark_outcome`. Keep this separate: REQUIRED_SELECT_CHOICES
# is pinned exactly to the writer's on-the-wire vocabulary.
OPERATOR_REQUIRED_SELECT_CHOICES: dict[str, dict[str, frozenset[str]]] = {
    "Sales Activities": {
        "Outcome": frozenset({"Held"}),
    },
}



def airtable_record_url(base_id: str, table: str, record_id: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}/{record_id}"


def airtable_table_url(base_id: str, table: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"


class MarkOutcomeError(RuntimeError):
    pass


def pending_select_writes(event: str) -> list[tuple[str, str, str]]:
    """Every (table, field, choice) this event will hand Airtable, before sending.

    Mirrors the write paths in `apply_mark_outcome` so the preflight and the test
    that pins `REQUIRED_SELECT_CHOICES` both read the intent from one place.
    """
    writes = [
        ("Prospects", "Status", TARGET_STATUS[event]),
        ("Sales Activities", "Activity Type", ACTIVITY_TYPE[event]),
        ("Sales Activities", "Outcome", ACTIVITY_OUTCOME[event]),
    ]
    if event == "closed_won":
        writes.append(("Deals", "Deal Status", CLOSED_WON_DEAL_STATUS))
    return writes


def select_writes_for_table(event: str, table: str) -> list[tuple[str, str, str]]:
    """The subset of `pending_select_writes` a single request is responsible for."""
    return [write for write in pending_select_writes(event) if write[0] == table]


def preflight_select_values(event: str) -> None:
    """Refuse an undeclared select choice before the first request goes out.

    Without typecast, Airtable answers an unknown choice with a bare 422
    INVALID_MULTIPLE_CHOICE_OPTIONS, which reads as a transient API fault rather
    than "the runbook step that creates these choices was never run". This is an
    offline check against the declaration only — it never fetches the schema, so
    it catches code that outran `REQUIRED_SELECT_CHOICES`; a base that is behind
    the declaration still surfaces as the 422 the module docstring warns about.
    """
    for table, field, value in pending_select_writes(event):
        if value not in REQUIRED_SELECT_CHOICES.get(table, {}).get(field, frozenset()):
            raise MarkOutcomeError(
                f"{table}.{field} choice {value!r} is not declared in "
                f"REQUIRED_SELECT_CHOICES, so the Airtable runbook step that "
                f"pre-creates these choices ({RUNBOOK_DOC}) does not cover it and "
                f"Airtable will 422 on it. Declare the choice here and create it in "
                f"the base before running this event."
            )


def _request_json(
    response: requests.Response,
    operation: str,
    *,
    select_writes: list[tuple[str, str, str]] | None = None,
) -> dict:
    if response.status_code >= 400:
        detail = ""
        # The preflight only proves the code agrees with its own declaration.
        # The base being behind that declaration — the state this repo is in
        # until the runbook runs — can only surface here, and Airtable says so
        # with an error code and no field name. Name the choices this request
        # required so the operator knows which runbook line was skipped instead
        # of reading it as a transient failure and retrying forever.
        if select_writes and UNKNOWN_CHOICE_ERROR_CODE in response.text:
            required = ", ".join(f"{table}.{field}={value!r}" for table, field, value in select_writes)
            detail = (
                f" — Airtable rejected a select choice this write requires ({required}). "
                f"These are declared in REQUIRED_SELECT_CHOICES but must also exist in the "
                f"base; run the choice-creation step in {RUNBOOK_DOC} before retrying."
            )
        raise MarkOutcomeError(
            f"Airtable {operation} failed {response.status_code}: {response.text[:1000]}{detail}"
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise MarkOutcomeError(f"Airtable {operation} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise MarkOutcomeError(f"Airtable {operation} returned a non-object response")
    return payload


def apply_mark_outcome(
    *,
    record_id: str,
    event: str,
    notes: str,
    occurred_at: str,
    base_id: str,
    prospects_table: str,
    activities_table: str,
    deals_table: str,
    token: str,
    deal_package: str = "",
    deal_setup_price: float | None = None,
    deal_retainer_price: float | None = None,
    session: object = requests,
) -> dict:
    if event not in OUTCOME_EVENTS:
        raise MarkOutcomeError(f"unknown event {event!r}; expected one of {sorted(OUTCOME_EVENTS)}")

    # Runs before the GET so an undeclared choice costs no request and cannot
    # leave a half-applied outcome: the Prospects PATCH lands before the Sales
    # Activities create, so a 422 on the activity would otherwise strand the
    # record at Follow-Up/Won/Lost with nothing in the ledger explaining why.
    preflight_select_values(event)

    try:
        occurred_on = dt.datetime.fromisoformat(occurred_at).date().isoformat()
    except ValueError as exc:
        raise MarkOutcomeError(f"occurred_at {occurred_at!r} is not an ISO 8601 timestamp") from exc

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    prospect_url = airtable_record_url(base_id, prospects_table, record_id)
    current = _request_json(session.get(prospect_url, headers=headers, timeout=30), "GET")
    if current.get("id") != record_id:
        raise MarkOutcomeError("Airtable GET did not confirm the requested record id")
    raw_fields = current.get("fields")
    if not isinstance(raw_fields, dict):
        raise MarkOutcomeError("Airtable GET response has no fields object")
    fields = dict(raw_fields)

    if normalize_airtable_status(fields.get("Status")) not in OUTCOME_SOURCE_STATUSES:
        raise MarkOutcomeError(
            f"current Airtable Status is not eligible for {event}; expected Follow-Up"
        )

    target_status = TARGET_STATUS[event]

    # A closed lead must leave the follow-up queue: Airtable clears a date on an
    # explicit null, and leaving the stale mark_sent Day-4 date behind would keep
    # a won or lost prospect surfacing as due forever. An open lead re-arms one
    # cadence step out from the event that was just observed.
    closing = event in CLOSING_EVENTS
    next_follow_up = None if closing else next_follow_up_date(occurred_on)

    patch_response = _request_json(
        session.patch(
            prospect_url,
            headers=headers,
            json={"fields": {"Status": target_status, "Next Follow-Up": next_follow_up}},
            timeout=30,
        ),
        "PATCH prospect",
        select_writes=select_writes_for_table(event, "Prospects"),
    )
    patched_fields = patch_response.get("fields")
    if not isinstance(patched_fields, dict) or str(patched_fields.get("Status") or "").strip() != target_status:
        raise MarkOutcomeError("Airtable PATCH response does not confirm the requested Status")
    applied_follow_up = str(patched_fields.get("Next Follow-Up") or "").strip()
    if applied_follow_up != (next_follow_up or ""):
        raise MarkOutcomeError("Airtable PATCH response does not confirm the requested Next Follow-Up")

    business_name = str(fields.get("Business Name") or record_id)
    activities_url = airtable_table_url(base_id, activities_table)
    activity_payload = {
        "records": [
            {
                "fields": {
                    "Activity Name": f"{ACTIVITY_TYPE[event]} — {business_name}",
                    "Activity Type": ACTIVITY_TYPE[event],
                    "Activity Date": occurred_at,
                    "Outcome": ACTIVITY_OUTCOME[event],
                    "Details": notes,
                    "Linked Prospect": [record_id],
                }
            }
        ]
    }
    activity_response = _request_json(
        session.post(activities_url, headers=headers, json=activity_payload, timeout=30),
        "POST activity",
        select_writes=select_writes_for_table(event, "Sales Activities"),
    )
    activity_records = activity_response.get("records")
    if not isinstance(activity_records, list) or not activity_records:
        raise MarkOutcomeError("Airtable POST to Sales Activities returned no records")
    activity_record_id = activity_records[0].get("id")

    result = {
        "result": "applied",
        "record_id": record_id,
        "event": event,
        "status": target_status,
        "next_follow_up": next_follow_up,
        "activity_record_id": activity_record_id,
    }

    if event == "closed_won":
        # Package is the Deals primary field: leaving it empty makes the deal
        # show as a blank chip everywhere it is linked, including on the
        # prospect that just closed. Fall back to the business name.
        deal_fields = {
            "Package": deal_package or business_name,
            "Deal Status": CLOSED_WON_DEAL_STATUS,
            "Prospect": [record_id],
        }
        if deal_setup_price is not None:
            deal_fields["Setup Price"] = deal_setup_price
        if deal_retainer_price is not None:
            deal_fields["Retainer Price"] = deal_retainer_price
        deals_url = airtable_table_url(base_id, deals_table)
        deal_response = _request_json(
            session.post(
                deals_url,
                headers=headers,
                json={"records": [{"fields": deal_fields}]},
                timeout=30,
            ),
            "POST deal",
            select_writes=select_writes_for_table(event, "Deals"),
        )
        deal_records = deal_response.get("records")
        if not isinstance(deal_records, list) or not deal_records:
            raise MarkOutcomeError("Airtable POST to Deals returned no records")
        result["deal_record_id"] = deal_records[0].get("id")

    return result


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-id", required=True, help="Airtable Prospects record id")
    parser.add_argument("--event", required=True, choices=sorted(OUTCOME_EVENTS))
    parser.add_argument("--notes", default="", help="Free-text details for the Sales Activities row")
    parser.add_argument(
        "--date", default="", help="When this happened (YYYY-MM-DD); defaults to today (UTC)"
    )
    parser.add_argument("--deal-package", default="", help="closed_won only: Package/tier sold")
    parser.add_argument("--deal-setup-price", type=float, default=None, help="closed_won only")
    parser.add_argument("--deal-retainer-price", type=float, default=None, help="closed_won only")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--prospects-table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument(
        "--activities-table", default=os.getenv("AIRTABLE_SALES_ACTIVITIES_TABLE", "Sales Activities")
    )
    parser.add_argument("--deals-table", default=os.getenv("AIRTABLE_DEALS_TABLE", "Deals"))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    args = parser.parse_args()

    if not args.base_id or not args.prospects_table or not args.token:
        print(
            "[ERROR] AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required",
            file=sys.stderr,
        )
        return 1

    try:
        occurred_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    except ValueError:
        print("[ERROR] --date must use YYYY-MM-DD", file=sys.stderr)
        return 1
    occurred_at = dt.datetime.combine(occurred_date, dt.time(12, 0), tzinfo=dt.timezone.utc).isoformat()

    try:
        receipt = apply_mark_outcome(
            record_id=args.record_id,
            event=args.event,
            notes=args.notes,
            occurred_at=occurred_at,
            base_id=args.base_id,
            prospects_table=args.prospects_table,
            activities_table=args.activities_table,
            deals_table=args.deals_table,
            token=args.token,
            deal_package=args.deal_package,
            deal_setup_price=args.deal_setup_price,
            deal_retainer_price=args.deal_retainer_price,
        )
    except MarkOutcomeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
