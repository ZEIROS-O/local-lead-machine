#!/usr/bin/env python3
"""Record a post-send funnel outcome for one Airtable prospect record.

The pipeline's only write-back after outreach used to be mark_sent.py, which
stops at Status=Follow-Up. Nothing recorded what happened after that: a reply, a
booked call, a closed-won deal, a closed-lost. This script is the write-back
for those events — it never sends anything, only logs what a human
already observed, mirroring mark_sent.py's guarded-PATCH conventions.

`opt_out` (alias `unsubscribed`) is the fifth event and the odd one out. The
other four are funnel telemetry; this one is a compliance record, and it is the
only verb whose primary output is *not* an Airtable write. It appends a row to
the suppression ledger — the file `compliance.load_suppression_index` reads and
every stage of the pipeline gates on — capturing the prospect's phone and email
as well as its record id, because `record_matches_suppression` matches on
contact channels. A business that opts out and is later re-discovered under a
brand-new Airtable record has a different record id and the same phone; only the
contact channels keep it suppressed.

Two things follow from it being a compliance record rather than telemetry:

  1. **No source-status gate.** A rep hears "take me off your list" on the first
     dial, when Status is still New. The other four verbs require Follow-Up.
  2. **The ledger row lands before any Airtable write.** A suppression row with
     no status change is over-suppression, which is safe; a status change with
     no suppression row is the liability. Every Airtable write in this module
     can still 422 until the runbook step below is run, so the row goes first.

`Do Not Contact` is deliberately *not* a Status: the 2026-08-03 consolidation
folded it into `Not Fit` because opt-out state belongs in the ledger, where
every stage already reads it, not in a hand-editable select column
(`queue_state.py:85-86`). `Not Fit` here is the visible consequence of the
suppression row, never a substitute for it.

Writes:
  - the suppression ledger (opt_out / unsubscribed only) — see above
  - Prospects.Status -> Follow-Up (replied / meeting_booked), Won (closed_won),
    Lost (closed_lost), Not Fit (opt_out / unsubscribed)
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
against `REQUIRED_SELECT_CHOICES` so a sixth event cannot be added without
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

from lead_machine.compliance import (
    ComplianceError,
    append_suppression_event,
    configured_suppression_source,
    normalize_airtable_status,
)
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
    # Terminal, and the *visible* half of an opt-out only. The binding half is
    # the suppression ledger row; see the module docstring.
    "opt_out": "Not Fit",
    "unsubscribed": "Not Fit",
}

ACTIVITY_TYPE = {
    "replied": "Reply",
    "meeting_booked": "Call",
    "closed_won": "Close",
    "closed_lost": "Close",
    # `Note` rather than a new choice: it already exists in the live base
    # (docs/airtable_schema_required.md), so the opt-out verb needs one new
    # choice created by hand, not two. The compliance meaning is carried by
    # Outcome, which had to be new either way.
    "opt_out": "Note",
    "unsubscribed": "Note",
}

ACTIVITY_OUTCOME = {
    "replied": "Replied",
    "meeting_booked": "Meeting Booked",
    "closed_won": "Won",
    "closed_lost": "Lost",
    "opt_out": "Opted Out",
    "unsubscribed": "Opted Out",
}

# The two spellings of the same compliance event. Both are already in
# `compliance.SUPPRESSION_EVENTS`, so either reads back out of the ledger.
SUPPRESSION_WRITING_EVENTS = frozenset({"opt_out", "unsubscribed"})

# A close ends the outreach cadence; anything else re-arms it. Keyed off the
# same map as the status so a new event can never quietly skip this decision.
# An opt-out closes it hardest of all: leaving a Next Follow-Up date behind
# would keep surfacing a suppressed prospect as due.
CLOSING_EVENTS = frozenset({"closed_won", "closed_lost"}) | SUPPRESSION_WRITING_EVENTS

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
        "Status": frozenset({"Follow-Up", "Won", "Lost", "Not Fit"}),
    },
    "Sales Activities": {
        "Activity Type": frozenset({"Reply", "Call", "Close", "Note"}),
        "Outcome": frozenset({"Replied", "Meeting Booked", "Won", "Lost", "Opted Out"}),
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
    suppression_source: str = "",
    extra_phones: list[str] | tuple[str, ...] = (),
    extra_emails: list[str] | tuple[str, ...] = (),
    session: object = requests,
) -> dict:
    if event not in OUTCOME_EVENTS:
        raise MarkOutcomeError(f"unknown event {event!r}; expected one of {sorted(OUTCOME_EVENTS)}")

    # Runs before the GET so an undeclared choice costs no request and cannot
    # leave a half-applied outcome: the Prospects PATCH lands before the Sales
    # Activities create, so a 422 on the activity would otherwise strand the
    # record at Follow-Up/Won/Lost with nothing in the ledger explaining why.
    preflight_select_values(event)

    suppressing = event in SUPPRESSION_WRITING_EVENTS
    if suppressing and not str(suppression_source or "").strip():
        # Before the GET on purpose: an opt-out with nowhere to record it must
        # not touch Airtable at all. Setting Status=Not Fit without the ledger
        # row is the exact failure this verb exists to prevent — it looks handled
        # and re-admits the business on the next discovery run.
        raise MarkOutcomeError(
            "opt_out requires a suppression ledger: set SUPPRESSION_SOURCE (or "
            "LPM_OUTCOMES_JSONL), or pass --suppression-source. Recording an "
            "opt-out only as an Airtable Status does not suppress the business."
        )

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

    current_status = normalize_airtable_status(fields.get("Status"))
    # An opt-out has no source-status gate, and must not: a rep hears "take me
    # off your list" on the first dial, when Status is still New. Gating it the
    # way the funnel verbs are gated would leave that call unrecordable.
    if not suppressing and current_status not in OUTCOME_SOURCE_STATUSES:
        raise MarkOutcomeError(
            f"current Airtable Status is not eligible for {event}; expected Follow-Up"
        )

    target_status = TARGET_STATUS[event]

    suppression_result = ""
    if suppressing:
        # Before every Airtable write. A suppression row with no status change is
        # over-suppression, which is safe; a status change with no suppression row
        # is the liability, and any of the writes below can still 422 until the
        # runbook creates the choices this module needs.
        try:
            suppression_receipt = append_suppression_event(
                suppression_source,
                event=event,
                record_id=record_id,
                phone=fields.get("Phone"),
                email=fields.get("Email"),
                occurred_at=occurred_at,
                source="mark_outcome",
                notes=notes,
            )
            suppression_result = str(suppression_receipt.get("result") or "")
            # Contacts the base does not carry — a second line, a personal
            # address the rep was given on the call. Suppression matches on
            # channels, so every channel we know has to land in the ledger.
            for kind, values in (("phone", extra_phones), ("email", extra_emails)):
                for value in values:
                    if not str(value or "").strip():
                        continue
                    append_suppression_event(
                        suppression_source,
                        event=event,
                        record_id=record_id,
                        **{kind: value},
                        occurred_at=occurred_at,
                        source="mark_outcome",
                        notes=notes,
                    )
        except ComplianceError as exc:
            raise MarkOutcomeError(f"could not record the opt-out: {exc}") from exc

        if suppression_result == "already_present" and current_status == normalize_airtable_status(
            target_status
        ):
            # Both halves already landed. Re-running must not mint a second
            # Sales Activities row for the same opt-out.
            return {
                "result": "already_applied",
                "record_id": record_id,
                "event": event,
                "status": target_status,
                "next_follow_up": None,
                "suppression": suppression_result,
            }

    # A closed lead must leave the follow-up queue: Airtable clears a date on an
    # explicit null, and leaving the stale mark_sent Day-4 date behind would keep
    # a won or lost prospect surfacing as due forever. An open lead re-arms one
    # cadence step out from the event that was just observed.
    closing = event in CLOSING_EVENTS
    next_follow_up = None if closing else next_follow_up_date(occurred_on)

    # Wrapped so an Airtable failure after the ledger row cannot read as "the
    # opt-out did not go through". The suppression is what binds; the Status
    # and the activity row are its bookkeeping, and every write below can still
    # 422 until the runbook creates the choices this module needs.
    try:
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
        if suppressing:
            result["suppression"] = suppression_result

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
    except MarkOutcomeError as exc:
        if not suppressing:
            raise
        raise MarkOutcomeError(
            f"{exc} — the suppression was recorded in the ledger "
            f"({suppression_result}), so the business is suppressed; only the "
            f"Airtable bookkeeping failed and can be retried."
        ) from exc


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
    parser.add_argument(
        "--suppression-source",
        default=configured_suppression_source(),
        help="opt_out/unsubscribed only: the suppression ledger to append to. "
        "Defaults to SUPPRESSION_SOURCE, then LPM_OUTCOMES_JSONL — the same "
        "resolution the pipeline reads the index with.",
    )
    parser.add_argument(
        "--phone",
        action="append",
        default=[],
        dest="phones",
        help="opt_out only: an extra phone to suppress beyond the one on the "
        "record. Repeatable. Suppression matches on contact channels, so a "
        "second line given on the call has to be recorded too.",
    )
    parser.add_argument(
        "--email",
        action="append",
        default=[],
        dest="emails",
        help="opt_out only: an extra email to suppress beyond the one on the record. Repeatable.",
    )
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
            suppression_source=args.suppression_source,
            extra_phones=args.phones,
            extra_emails=args.emails,
        )
    except MarkOutcomeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
