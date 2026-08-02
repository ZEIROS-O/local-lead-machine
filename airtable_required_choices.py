#!/usr/bin/env python3
"""Check the live base offers every select choice the code writes. Read-only.

`typecast` was removed from every write path (`91c7131`) because it minted a
choice for whatever string it was handed — that is how `Prospects.Lead Priority`
acquired an option named `(440) 537-2459`. Removing it moved the obligation onto
the base: a select value the base does not already offer now 422s instead of
silently creating itself. That is the correct trade, but it means the base can
drift behind the code and the only symptom is a failed write at the worst
possible moment.

This module is the check that catches that drift ahead of time. It is exactly
the verification the cutover runbook calls for — "a read-only schema check
contains every value in `mark_outcome.REQUIRED_SELECT_CHOICES`" — made runnable
and repeatable instead of a prose instruction.

WHY IT ONLY REPORTS, AND CANNOT FIX
-----------------------------------
Creating the missing choices is a hand step in the Airtable UI. That is not a
missing feature here; the API genuinely cannot do it. Observed 2026-07-30
against `Prospects.Status` with a PAT that has schema-write access:

  - `PATCH /v0/meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}` with
    `{"description": ...}` -> **200**, and again to restore it -> 200. The
    endpoint, the token and the field are all fine.
  - The same endpoint with `options.choices` -> **422
    INVALID_REQUEST_UNKNOWN**, in five structurally different body shapes:
    existing choices echoed verbatim; without colours; ids only; new choices
    with and without colours; and with an explicit `"type": "singleSelect"`.
    Identical generic validation failure every time.

So Airtable's Update Field API does not modify an existing single-select's
choices. `POST .../fields` *can* create a new field with choices — that is how
`ICP Lane` was created the same day — but update cannot, and there is no delete
endpoint at all. Re-test with the two calls above before believing otherwise;
an earlier version of `docs/airtable_schema_required.md` asserted the opposite
on no recorded evidence, which is the mistake this note exists to prevent.

The remaining routes are both worse than a hand step: restoring `typecast` is
the drift channel that was deliberately closed, and creating a replacement field
to migrate 145 records onto would be a destructive rename dance on the CRM's
most-read column, with no way to delete the original afterwards.

The target vocabulary is READ FROM THE CODE, never transcribed here:
`mark_outcome.REQUIRED_SELECT_CHOICES` owns writer-emitted choices,
`mark_outcome.OPERATOR_REQUIRED_SELECT_CHOICES` owns manual workflow choices,
`queue_state.PROSPECT_STATUS_CHOICES` is the canonical Status vocabulary, and
`upload_airtable.configured_write_path_select_choices()` adds the active setter
roster. Restating any of them here would create exactly the drift channel those
declarations exist to close.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from lead_machine.queue_state import PROSPECT_STATUS_CHOICES
from mark_outcome import OPERATOR_REQUIRED_SELECT_CHOICES, REQUIRED_SELECT_CHOICES
from upload_airtable import configured_write_path_select_choices

META_TABLES_URL = "https://api.airtable.com/v0/meta/bases/{base_id}/tables"

# The logical table names REQUIRED_SELECT_CHOICES is keyed by, mapped to the env
# var that renames each one. Same defaults as mark_outcome's --*-table flags: a
# renamed table still carries the same field vocabulary.
TABLE_ENV = {
    "Prospects": ("AIRTABLE_PROSPECTS_TABLE", ""),
    "Sales Activities": ("AIRTABLE_SALES_ACTIVITIES_TABLE", "Sales Activities"),
    "Deals": ("AIRTABLE_DEALS_TABLE", "Deals"),
}


@dataclass(frozen=True)
class FieldStatus:
    """One field's live choices measured against what the code needs."""

    table_label: str
    table_id: str
    field_id: str
    field_name: str
    live: tuple[str, ...]
    missing: tuple[str, ...]
    # A field the base does not have at all. Its own state, not an empty
    # `missing`: with an unconfigured roster `Assigned Setter` declares no
    # choices, and "absent with nothing declared" must not read as satisfied.
    present: bool = True

    @property
    def satisfied(self) -> bool:
        return self.present and not self.missing


def required_choices() -> dict[str, dict[str, frozenset[str]]]:
    """The choices the live base must offer, derived from the code that writes them.

    `REQUIRED_SELECT_CHOICES` states only what `mark_outcome` sends, so for
    `Prospects.Status` it names three of the ten canonical values. The base
    needs the whole vocabulary, and `PROSPECT_STATUS_CHOICES` is the module that
    owns it — union rather than replace, so a value declared in one and not the
    other is still reported. Operator-only and configured upload choices are
    unioned for the same reason.
    """
    required = {
        table: {field: frozenset(choices) for field, choices in fields.items()}
        for table, fields in REQUIRED_SELECT_CHOICES.items()
    }
    for table, fields in OPERATOR_REQUIRED_SELECT_CHOICES.items():
        target = required.setdefault(table, {})
        for field, choices in fields.items():
            target[field] = frozenset(target.get(field, frozenset())) | frozenset(choices)
    prospects = required.setdefault("Prospects", {})
    prospects["Status"] = frozenset(prospects.get("Status", frozenset())) | frozenset(
        PROSPECT_STATUS_CHOICES
    )
    for field, choices in configured_write_path_select_choices().items():
        prospects[field] = frozenset(prospects.get(field, frozenset())) | choices
    return required


def resolve_table_names(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Map each logical table to the name/id it actually has in this base."""
    resolved: dict[str, str] = {}
    for label, (env_var, fallback) in TABLE_ENV.items():
        override = (overrides or {}).get(label, "")
        resolved[label] = override or os.getenv(env_var, "") or fallback
    missing = sorted(label for label, name in resolved.items() if not name)
    if missing:
        raise ValueError("cannot resolve Airtable table name for: " + ", ".join(missing))
    return resolved


def _find_table(tables: list[Any], wanted: str) -> dict[str, Any]:
    matches = [
        item
        for item in tables
        if isinstance(item, dict)
        and (item.get("id") == wanted or item.get("name") == wanted)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one Airtable table named/id {wanted!r}, found {len(matches)}"
        )
    return matches[0]


def _find_field(
    table: dict[str, Any], wanted: str, *, absent_ok: bool = False
) -> dict[str, Any] | None:
    """The named field, or None when `absent_ok` and the base does not have it.

    Absence is a reportable gap with UI steps that close it. Two fields sharing
    a name is still a wrong assumption about the base and stops the check.
    """
    fields = table.get("fields")
    if not isinstance(fields, list):
        raise ValueError(f"table {table.get('name')!r} schema has no fields list")
    matches = [
        item
        for item in fields
        if isinstance(item, dict)
        and (item.get("id") == wanted or item.get("name") == wanted)
    ]
    if not matches and absent_ok:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"expected one field named/id {wanted!r} in table "
            f"{table.get('name')!r}, found {len(matches)}"
        )
    return matches[0]


def live_choice_names(field: dict[str, Any]) -> tuple[str, ...]:
    """The field's live choice names. Anything but a single select is refused.

    A missing choice is a reporting result; the wrong field type is a wrong
    assumption about the base, which should stop the check rather than be
    summarised as "nothing missing".
    """
    if field.get("type") != "singleSelect":
        raise ValueError(
            f"field {field.get('name')!r} is {field.get('type')!r}, not singleSelect; "
            "the declared choices cannot apply to it"
        )
    options = field.get("options")
    choices = (options or {}).get("choices") if isinstance(options, dict) else None
    if not isinstance(choices, list):
        raise ValueError(f"field {field.get('name')!r} has no choices list")
    names = []
    for choice in choices:
        if not isinstance(choice, dict) or not str(choice.get("name") or "").strip():
            raise ValueError(f"field {field.get('name')!r} has an unnamed choice")
        names.append(str(choice["name"]))
    return tuple(names)


def audit_choices(
    tables: list[Any],
    required: dict[str, dict[str, frozenset[str]]],
    table_names: dict[str, str],
) -> list[FieldStatus]:
    """Diff the live schema against the required vocabulary."""
    statuses: list[FieldStatus] = []
    for table_label in sorted(required):
        table = _find_table(tables, table_names[table_label])
        for field_name in sorted(required[table_label]):
            field = _find_field(table, field_name, absent_ok=True)
            if field is None:
                statuses.append(
                    FieldStatus(
                        table_label=table_label,
                        table_id=str(table.get("id") or ""),
                        field_id="",
                        field_name=field_name,
                        live=(),
                        missing=tuple(sorted(required[table_label][field_name])),
                        present=False,
                    )
                )
                continue
            live = live_choice_names(field)
            # Sorted so repeated runs, the report and the hand steps all agree.
            missing = tuple(sorted(required[table_label][field_name] - set(live)))
            statuses.append(
                FieldStatus(
                    table_label=table_label,
                    table_id=str(table.get("id") or ""),
                    field_id=str(field.get("id") or ""),
                    field_name=str(field.get("name") or ""),
                    live=live,
                    missing=missing,
                )
            )
    return statuses


def missing_choices(statuses: list[FieldStatus]) -> list[str]:
    """What the live schema still does not offer.

    An absent field is one entry naming the field rather than one per choice:
    the field has to exist before any choice can be added to it, and an absent
    field whose declaration is empty would otherwise report nothing at all.
    """
    reported: list[str] = []
    for status in statuses:
        if not status.present:
            reported.append(
                f"{status.table_label}.{status.field_name}: field does not exist"
            )
            continue
        reported.extend(
            f"{status.table_label}.{status.field_name}: {name}"
            for name in status.missing
        )
    return reported


def hand_steps(statuses: list[FieldStatus], base_id: str) -> list[str]:
    """The Airtable UI steps that close the gap, since no API call can."""
    incomplete = [status for status in statuses if not status.satisfied]
    if not incomplete:
        return []
    lines = [
        "",
        "The Airtable API cannot add choices to an existing single select (see this",
        "module's docstring for the recorded 422). Add them by hand:",
        "",
    ]
    for status in incomplete:
        lines.append(
            f"  {status.table_label} -> {status.field_name}  "
            f"https://airtable.com/{base_id}/{status.table_id}"
        )
        if status.present:
            lines.append("    Customize field type -> add each option, exact spelling:")
        else:
            lines.append(
                "    This field does not exist. Create it as a single select, then"
            )
            lines.append("    add each option, exact spelling:")
        for name in status.missing:
            lines.append(f"      - {name}")
        if not status.missing:
            lines.append("      (no choices declared yet — set LPM_SETTER_ROSTER)")
        lines.append("")
    lines.append("Then re-run this check; it must exit 0 before mark_outcome can run.")
    return lines


def fetch_base_tables(base_id: str, token: str) -> list[Any]:
    response = requests.get(
        META_TABLES_URL.format(base_id=quote(base_id)),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    value = response.json()
    tables = value.get("tables") if isinstance(value, dict) else None
    if not isinstance(tables, list):
        raise ValueError("Airtable base schema must contain a tables list")
    return tables


def _credentials(args: argparse.Namespace) -> tuple[str, str]:
    load_dotenv()
    base_id = args.base_id or os.getenv("AIRTABLE_BASE_ID", "")
    token = (
        args.token or os.getenv("AIRTABLE_PAT", "") or os.getenv("AIRTABLE_API_KEY", "")
    )
    missing = [
        name
        for name, value in (("AIRTABLE_BASE_ID", base_id), ("AIRTABLE_PAT", token))
        if not value
    ]
    if missing:
        raise ValueError("missing Airtable configuration: " + ", ".join(missing))
    return base_id, token


def run_check(args: argparse.Namespace) -> int:
    base_id, token = _credentials(args)
    tables = fetch_base_tables(base_id, token)
    statuses = audit_choices(tables, required_choices(), resolve_table_names())
    for status in statuses:
        if status.satisfied:
            print(f"[OK] {status.table_label}.{status.field_name}")
        elif not status.present:
            print(
                f"[ABSENT] {status.table_label}.{status.field_name}: "
                "field does not exist in the base"
            )
        else:
            print(
                f"[MISSING] {status.table_label}.{status.field_name} "
                f"({status.field_id}): {', '.join(status.missing)}"
            )
    missing = missing_choices(statuses)
    for line in hand_steps(statuses, base_id):
        print(line)
    print(
        json.dumps(
            {
                "ok": not missing,
                "missing": missing,
                "fields": [
                    {
                        "table": status.table_label,
                        "field": status.field_name,
                        "field_id": status.field_id,
                        "present": status.present,
                        "live_choice_count": len(status.live),
                        "missing": list(status.missing),
                    }
                    for status in statuses
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not missing else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the live Airtable base offers every select choice the "
        "code writes. Read-only; prints the UI steps for anything missing."
    )
    parser.add_argument("--base-id", default="")
    parser.add_argument("--token", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="exit 0 when the base is complete, 2 when not")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_check(args)
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"[airtable-required-choices] REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
