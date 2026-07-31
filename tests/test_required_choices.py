from __future__ import annotations

import ast
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import airtable_required_choices as checker
import upload_airtable
import mark_sent
import score_leads
from airtable_required_choices import (
    FieldStatus,
    audit_choices,
    hand_steps,
    live_choice_names,
    main,
    missing_choices,
    required_choices,
    resolve_table_names,
)
from lead_machine.queue_state import PROSPECT_STATUS_CHOICES
from lead_machine import airtable_sync, queue_state, score_schema, sendability, verdict
from mark_outcome import REQUIRED_SELECT_CHOICES

REPO_ROOT = Path(__file__).resolve().parent.parent

TABLE_NAMES = {
    "Prospects": "Prospects",
    "Sales Activities": "Sales Activities",
    "Deals": "Deals",
}


def choice(choice_id: str, name: str, color: str = "blueLight2") -> dict:
    return {"id": choice_id, "name": name, "color": color}


def select_field(field_id: str, name: str, choices: list[dict]) -> dict:
    return {
        "id": field_id,
        "name": name,
        "type": "singleSelect",
        "options": {"choices": choices},
    }


def live_tables(
    *,
    status: list[dict] | None = None,
    activity_type: list[dict] | None = None,
    outcome: list[dict] | None = None,
    deal_status: list[dict] | None = None,
) -> list[dict]:
    """The base as the Meta API returns it, defaulting to the 2026-07-30 state."""
    prospect_fields = [
        select_field(
            "fldStatus",
            "Status",
            status
            if status is not None
            else [
                choice("sel1", "New"),
                choice("sel2", "Mockup Needed"),
                choice("sel3", "Manual Review"),
                choice("sel4", "Not Fit"),
                choice("sel5", "Sent"),
                choice("sel6", "Follow-Up"),
                choice("sel7", "Closed"),
            ],
        )
    ]
    prospect_fields.extend(
        select_field(
            f"fldProspect{index}",
            field_name,
            [choice(f"selProspect{index}_{choice_index}", name) for choice_index, name in enumerate(sorted(choices))],
        )
        for index, (field_name, choices) in enumerate(upload_airtable.WRITE_PATH_SELECT_CHOICES.items())
        if field_name != "Status"
    )
    return [
        {
            "id": "tblProspects",
            "name": "Prospects",
            "fields": prospect_fields,
        },
        {
            "id": "tblActivities",
            "name": "Sales Activities",
            "fields": [
                select_field(
                    "fldActivityType",
                    "Activity Type",
                    activity_type
                    if activity_type is not None
                    else [choice("selA", "Call"), choice("selB", "Email")],
                ),
                select_field(
                    "fldOutcome",
                    "Outcome",
                    outcome if outcome is not None else [choice("selC", "Completed")],
                ),
            ],
        },
        {
            "id": "tblDeals",
            "name": "Deals",
            "fields": [
                select_field(
                    "fldDealStatus",
                    "Deal Status",
                    deal_status
                    if deal_status is not None
                    else [choice("selD", "Deposit Pending")],
                )
            ],
        },
    ]


def complete_tables() -> list[dict]:
    return live_tables(
        status=[
            choice(f"sel{i}", name) for i, name in enumerate(PROSPECT_STATUS_CHOICES)
        ],
        activity_type=[
            choice("selA", "Call"),
            choice("selB", "Reply"),
            choice("selC", "Close"),
        ],
        outcome=[
            choice("selD", "Replied"),
            choice("selE", "Meeting Booked"),
            choice("selF", "Won"),
            choice("selG", "Lost"),
        ],
    )


def statuses_for(tables: list[dict]) -> list[FieldStatus]:
    return audit_choices(tables, required_choices(), TABLE_NAMES)


def status_named(statuses: list[FieldStatus], field: str) -> FieldStatus:
    return next(status for status in statuses if status.field_name == field)


class RequiredVocabularyIsDerivedTest(unittest.TestCase):
    """The target set must come from the code that writes it, never a local copy."""

    def test_status_target_is_the_canonical_eight_value_vocabulary(self):
        self.assertEqual(
            required_choices()["Prospects"]["Status"],
            frozenset(PROSPECT_STATUS_CHOICES)
            | REQUIRED_SELECT_CHOICES["Prospects"]["Status"],
        )
        # The whole point of the split: the base must be able to hold both.
        self.assertIn("Won", required_choices()["Prospects"]["Status"])
        self.assertIn("Lost", required_choices()["Prospects"]["Status"])

    def test_every_required_select_choice_declaration_is_covered(self):
        required = required_choices()
        for table, fields in REQUIRED_SELECT_CHOICES.items():
            for field, choices in fields.items():
                self.assertTrue(
                    choices <= required[table][field],
                    f"{table}.{field} lost declared choices",
                )

    def test_target_moves_when_the_declaration_moves(self):
        """A value added to the module must reach the check without editing it."""
        extended = {
            **REQUIRED_SELECT_CHOICES,
            "Sales Activities": {
                **REQUIRED_SELECT_CHOICES["Sales Activities"],
                "Outcome": REQUIRED_SELECT_CHOICES["Sales Activities"]["Outcome"]
                | {"Ghosted"},
            },
        }
        with patch.object(checker, "REQUIRED_SELECT_CHOICES", extended):
            self.assertIn("Ghosted", required_choices()["Sales Activities"]["Outcome"])

    def test_status_target_follows_queue_state(self):
        with patch.object(
            checker, "PROSPECT_STATUS_CHOICES", (*PROSPECT_STATUS_CHOICES, "Archived")
        ):
            self.assertIn("Archived", required_choices()["Prospects"]["Status"])


class ProspectWritePathVocabularyTest(unittest.TestCase):
    """Every select the upload path can write has one owned vocabulary."""

    def test_every_prospect_single_select_has_a_required_vocabulary(self):
        schema_selects = {
            field
            for field, airtable_type, _notes in upload_airtable._FIELD_SCHEMA
            if airtable_type == "Single select"
        }
        self.assertEqual(schema_selects, set(upload_airtable.WRITE_PATH_SELECT_CHOICES))

    def test_select_schema_notes_are_derived_from_owned_vocabularies(self):
        schema_notes = {
            field: notes
            for field, airtable_type, notes in upload_airtable._FIELD_SCHEMA
            if airtable_type == "Single select"
        }
        expected = {
            field: "Options: " + ", ".join(sorted(choices))
            for field, choices in upload_airtable.WRITE_PATH_SELECT_CHOICES.items()
        }
        self.assertEqual(schema_notes, expected)

    def test_required_choices_unions_the_complete_prospect_write_path(self):
        required = required_choices()["Prospects"]
        for field, choices in upload_airtable.WRITE_PATH_SELECT_CHOICES.items():
            self.assertTrue(
                choices <= required[field],
                f"Prospects.{field} lost upload-path choices",
            )
        self.assertIn("skip", required["ICP Lane"])

    def test_writer_owned_sets_drive_the_map_notes_and_required_union(self):
        # `Packet Tier` and the four readiness selects are absent by design:
        # step 4 of the verdict cutover deletes those columns, and this gate
        # refuses on a field the base lacks — so listing one the write path no
        # longer sends would fail the gate over a column nobody writes.
        owned = {
            "Vertical": airtable_sync.VERTICAL_CHOICES,
            "Verdict": verdict.VERDICT_CHOICES,
            "Lead Priority": score_leads.LEAD_PRIORITY_CHOICES,
            "Offer Fit": airtable_sync.OFFER_FIT_CHOICES,
            "Site Status": airtable_sync.SITE_STATUS_CHOICES,
            "AI Confidence": airtable_sync.AI_CONFIDENCE_CHOICES,
            "ICP Lane": airtable_sync.ICP_LANE_CHOICES,
            "Status": queue_state.PROSPECT_STATUS_CHOICE_SET,
            "Outreach Channel": mark_sent.OUTREACH_CHANNEL_CHOICES,
        }
        notes = {
            field: note
            for field, airtable_type, note in upload_airtable._FIELD_SCHEMA
            if airtable_type == "Single select"
        }
        required = required_choices()["Prospects"]
        for field, choices in owned.items():
            with self.subTest(field=field):
                self.assertIs(upload_airtable.WRITE_PATH_SELECT_CHOICES[field], choices)
                self.assertEqual(notes[field], "Options: " + ", ".join(sorted(choices)))
                self.assertTrue(choices <= required[field])


class AuditChoicesTest(unittest.TestCase):
    def test_reports_exactly_what_the_live_base_lacks(self):
        statuses = statuses_for(live_tables())
        self.assertEqual(status_named(statuses, "Status").missing, ("Lost", "Won"))
        self.assertEqual(
            status_named(statuses, "Activity Type").missing, ("Close", "Reply")
        )
        self.assertEqual(
            status_named(statuses, "Outcome").missing,
            ("Lost", "Meeting Booked", "Replied", "Won"),
        )

    def test_a_satisfied_field_reports_nothing_missing(self):
        deal_status = status_named(statuses_for(live_tables()), "Deal Status")
        self.assertEqual(deal_status.missing, ())
        self.assertTrue(deal_status.satisfied)

    def test_a_retired_choice_is_never_reported_as_a_problem(self):
        """`Closed` is retired, but removing it is a hand step, not this check's call."""
        status = status_named(statuses_for(live_tables()), "Status")
        self.assertIn("Closed", status.live)
        self.assertNotIn("Closed", status.missing)

    def test_nothing_is_missing_once_the_base_is_complete(self):
        self.assertEqual(missing_choices(statuses_for(complete_tables())), [])

    def test_results_are_ordered_so_repeated_runs_agree(self):
        first = status_named(statuses_for(live_tables()), "Outcome").missing
        second = status_named(statuses_for(live_tables()), "Outcome").missing
        self.assertEqual(first, second)
        self.assertEqual(list(first), sorted(first))

    def test_a_non_select_field_stops_the_check_rather_than_passing_it(self):
        tables = live_tables()
        tables[0]["fields"][0] = {
            "id": "fldStatus",
            "name": "Status",
            "type": "singleLineText",
        }
        with self.assertRaises(ValueError) as caught:
            statuses_for(tables)
        self.assertIn("singleSelect", str(caught.exception))

    def test_a_missing_field_is_refused_rather_than_skipped(self):
        tables = live_tables()
        tables[1]["fields"] = [
            field for field in tables[1]["fields"] if field["name"] != "Outcome"
        ]
        with self.assertRaises(ValueError) as caught:
            statuses_for(tables)
        self.assertIn("Outcome", str(caught.exception))

    def test_live_choice_names_rejects_a_field_with_no_options(self):
        with self.assertRaises(ValueError):
            live_choice_names({"id": "f", "name": "Status", "type": "singleSelect"})


class HandStepsTest(unittest.TestCase):
    def test_every_missing_choice_appears_in_the_instructions(self):
        lines = "\n".join(hand_steps(statuses_for(live_tables()), "appTest"))
        for name in ("Won", "Lost", "Reply", "Close", "Replied", "Meeting Booked"):
            self.assertIn(name, lines)

    def test_instructions_link_the_table_that_holds_the_field(self):
        lines = "\n".join(hand_steps(statuses_for(live_tables()), "appTest"))
        self.assertIn("https://airtable.com/appTest/tblProspects", lines)
        self.assertIn("https://airtable.com/appTest/tblActivities", lines)

    def test_a_complete_base_produces_no_instructions(self):
        self.assertEqual(hand_steps(statuses_for(complete_tables()), "appTest"), [])


class ReadOnlyContractTest(unittest.TestCase):
    """This module must never mutate Airtable; the API cannot do it correctly."""

    def _module_ast(self) -> ast.Module:
        source = (REPO_ROOT / "airtable_required_choices.py").read_text(encoding="utf-8")
        return ast.parse(source)

    def test_module_issues_no_write_requests(self):
        writes = {"post", "patch", "put", "delete"}
        found = [
            node.func.attr
            for node in ast.walk(self._module_ast())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in writes
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
        ]
        self.assertEqual(found, [])

    def test_module_exposes_no_mutation_path(self):
        suspicious = [
            name
            for name in dir(checker)
            if any(
                word in name.lower()
                for word in ("apply", "patch", "delete", "remove", "create")
            )
        ]
        self.assertEqual(suspicious, [])


class TableResolutionTest(unittest.TestCase):
    def test_env_renames_a_table_without_changing_the_vocabulary(self):
        with patch.dict(
            checker.os.environ,
            {
                "AIRTABLE_PROSPECTS_TABLE": "Leads",
                "AIRTABLE_SALES_ACTIVITIES_TABLE": "Activity Log",
                "AIRTABLE_DEALS_TABLE": "Deals",
            },
            clear=False,
        ):
            resolved = resolve_table_names()
        self.assertEqual(resolved["Prospects"], "Leads")
        self.assertEqual(resolved["Sales Activities"], "Activity Log")
        self.assertIn("Prospects", required_choices())

    def test_an_unresolvable_table_is_refused(self):
        with patch.dict(
            checker.os.environ, {"AIRTABLE_PROSPECTS_TABLE": ""}, clear=False
        ):
            with self.assertRaises(ValueError) as caught:
                resolve_table_names()
        self.assertIn("Prospects", str(caught.exception))


class CommandExitCodeTest(unittest.TestCase):
    def _run(self, tables: list[dict]) -> tuple[int, str]:
        buffer = io.StringIO()
        with patch.object(
            checker, "fetch_base_tables", return_value=tables
        ), patch.object(
            checker, "_credentials", return_value=("appTest", "patTest")
        ), patch.object(
            checker, "resolve_table_names", return_value=TABLE_NAMES
        ):
            with contextlib.redirect_stdout(buffer):
                code = main(["check"])
        return code, buffer.getvalue()

    def test_check_exits_two_while_a_declared_choice_is_missing(self):
        code, output = self._run(live_tables())
        self.assertEqual(code, 2)
        self.assertIn("[MISSING] Prospects.Status", output)
        self.assertIn("Won", output)

    def test_check_exits_zero_once_the_base_offers_everything(self):
        code, output = self._run(complete_tables())
        self.assertEqual(code, 0)
        self.assertIn("[OK] Prospects.Status", output)

    def test_a_schema_shaped_wrong_exits_two_rather_than_tracebacks(self):
        tables = live_tables()
        tables[1]["fields"] = []
        code, output = self._run(tables)
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", output)


if __name__ == "__main__":
    unittest.main()
