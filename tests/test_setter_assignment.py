from __future__ import annotations

import os
import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import airtable_required_choices
import upload_airtable
from lead_machine.airtable_sync import plan_airtable_sync
from lead_machine.setter_assignment import (
    assign_setter,
    assignment_identity,
    configured_setter_roster,
)


class SetterRosterTest(unittest.TestCase):
    def test_roster_preserves_declared_order_and_trims_names(self) -> None:
        self.assertEqual(
            configured_setter_roster(" Michael, Setter Two ,Setter Three "),
            ("Michael", "Setter Two", "Setter Three"),
        )

    def test_roster_rejects_case_insensitive_duplicates(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate setter"):
            configured_setter_roster("Michael, michael")

    def test_assignment_uses_stable_sha256_modulo(self) -> None:
        roster = ("Michael", "Setter Two", "Setter Three")
        # Hand-checked against SHA-256 integer modulo 3. A salted builtin hash
        # changes between Python processes and would not satisfy these literals.
        self.assertEqual(assign_setter("ChIJLANE", roster), "Setter Three")
        self.assertEqual(assign_setter("ChIJ123", roster), "Michael")
        self.assertEqual(assign_setter("ChIJLANE", roster), "Setter Three")

    def test_assignment_refuses_a_blank_key_or_roster(self) -> None:
        with self.assertRaisesRegex(ValueError, "Place Key"):
            assign_setter("", ("Michael",))
        with self.assertRaisesRegex(ValueError, "roster"):
            assign_setter("ChIJ123", ())

    def test_identity_is_the_place_key_untouched_when_there_is_one(self) -> None:
        # Not `place:ChIJLANE`: the SOP states the formula as sha256(Place Key)
        # and every hand-checked literal above depends on it staying that.
        self.assertEqual(assignment_identity("ChIJLANE", "(440) 555-0100"), "ChIJLANE")

    def test_identity_falls_back_to_the_same_phone_key_the_upsert_uses(self) -> None:
        self.assertEqual(assignment_identity("", "(440) 555-0100"), "phone:4405550100")
        self.assertEqual(assignment_identity("", "440-555-0100"), "phone:4405550100")

    def test_identity_is_blank_only_when_the_record_has_no_identifier(self) -> None:
        self.assertEqual(assignment_identity("", ""), "")


class UploadSetterAssignmentTest(unittest.TestCase):
    def _card(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return upload_airtable.csv_row_to_record(
                {
                    "Business Name": "A+ Patio Pros",
                    "Place Key": "ChIJLANE",
                    "Phone": "(440) 555-0100",
                },
                Path(tmp),
            )

    def test_configured_assignment_is_seeded_on_create_only(self) -> None:
        with patch.dict(
            os.environ,
            {"LPM_SETTER_ROSTER": "Michael,Setter Two,Setter Three"},
            clear=False,
        ):
            card = self._card()

        self.assertEqual(card["airtable_fields"]["Assigned Setter"], "Setter Three")
        self.assertIn("Assigned Setter", card["defaulted_airtable_fields"])

        posted = plan_airtable_sync([card])[0]
        self.assertEqual(posted["payload"]["fields"]["Assigned Setter"], "Setter Three")

        patched = plan_airtable_sync(
            [card], existing_records_by_key={"place:ChIJLANE": "recExisting"}
        )[0]
        self.assertEqual(patched["method"], "PATCH")
        self.assertNotIn("Assigned Setter", patched["payload"]["fields"])

    def test_unconfigured_roster_leaves_assignment_absent(self) -> None:
        with patch.dict(os.environ, {"LPM_SETTER_ROSTER": ""}, clear=False):
            card = self._card()
        self.assertNotIn("Assigned Setter", card["airtable_fields"])

    def test_csv_cannot_inject_an_assignment_when_roster_is_unconfigured(self) -> None:
        with patch.dict(os.environ, {"LPM_SETTER_ROSTER": ""}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                card = upload_airtable.csv_row_to_record(
                    {
                        "Business Name": "Injected Setter Lead",
                        "Place Key": "ChIJ123",
                        "Assigned Setter": "Unregistered Person",
                    },
                    Path(tmp),
                )
        self.assertNotIn("Assigned Setter", card["airtable_fields"])

    def test_a_phone_only_row_is_assigned_rather_than_aborting_the_run(self) -> None:
        # `output/scored_detailing_columbus_medium_001.csv` is 140 rows with no
        # Place Key and a phone. Refusing them would have made the roster and
        # the verdict re-upload mutually exclusive.
        with patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Michael"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                card = upload_airtable.csv_row_to_record(
                    {
                        "Business Name": "Phone Only Lead",
                        "Phone": "(440) 555-0100",
                    },
                    Path(tmp),
                )
        self.assertEqual(card["upsert_key"], "phone:4405550100")
        self.assertEqual(card["airtable_fields"]["Assigned Setter"], "Michael")
        self.assertIn("Assigned Setter", card["defaulted_airtable_fields"])

    def test_a_phone_only_row_hashes_the_key_it_upserts_on(self) -> None:
        roster = ("Michael", "Setter Two", "Setter Three")
        with patch.dict(
            os.environ, {"LPM_SETTER_ROSTER": ",".join(roster)}, clear=False
        ):
            with tempfile.TemporaryDirectory() as tmp:
                card = upload_airtable.csv_row_to_record(
                    {"Business Name": "Phone Only Lead", "Phone": "(440) 555-0100"},
                    Path(tmp),
                )
        self.assertEqual(
            card["airtable_fields"]["Assigned Setter"],
            assign_setter(card["upsert_key"], roster),
        )

    def test_a_row_with_no_identifier_still_fails_by_business_name(self) -> None:
        with patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Michael"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "Nameless Co"):
                    upload_airtable.csv_row_to_record(
                        {"Business Name": "Nameless Co"},
                        Path(tmp),
                    )

    def test_required_choice_gate_reads_the_same_configured_roster(self) -> None:
        with patch.dict(
            os.environ,
            {"LPM_SETTER_ROSTER": "Michael,Setter Two,Setter Three"},
            clear=False,
        ):
            choices = airtable_required_choices.required_choices()
        self.assertEqual(
            choices["Prospects"]["Assigned Setter"],
            frozenset({"Michael", "Setter Two", "Setter Three"}),
        )

    def test_schema_report_declares_the_assignment_field(self) -> None:
        report = upload_airtable.build_schema_report()
        assignment = next(row for row in report if row["field"] == "Assigned Setter")
        self.assertEqual(assignment["type"], "Single select")

    def test_jsonl_assignment_is_seeded_on_create_and_removed_from_patch(self) -> None:
        source = {
            "place_id_or_unique_key": "ChIJLANE",
            "upsert_key": "place:ChIJLANE",
            "business_name": "JSONL Lead",
            "airtable_fields": {
                "Business Name": "JSONL Lead",
                "Place Key": "ChIJLANE",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.jsonl"
            path.write_text(json.dumps(source) + "\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"LPM_SETTER_ROSTER": "Michael,Setter Two,Setter Three"},
                clear=False,
            ):
                card = upload_airtable.read_input_records(path, packet_dir=Path(tmp))[0]

        self.assertEqual(card["airtable_fields"]["Assigned Setter"], "Setter Three")
        self.assertIn("Assigned Setter", card["defaulted_airtable_fields"])
        posted = plan_airtable_sync([card])[0]
        self.assertEqual(posted["payload"]["fields"]["Assigned Setter"], "Setter Three")
        patched = plan_airtable_sync(
            [card], existing_records_by_key={"place:ChIJLANE": "recExisting"}
        )[0]
        self.assertNotIn("Assigned Setter", patched["payload"]["fields"])

    def test_jsonl_phone_only_card_is_assigned_not_refused(self) -> None:
        # output/airtable_detailing_columbus_visual_pass_2026-07-12.jsonl is
        # seven such cards; the JSONL door has the same fallback as the CSV one.
        source = {
            "business_name": "Phone Only Card",
            "phone": "(440) 555-0100",
            "upsert_key": "phone:4405550100",
            "airtable_fields": {
                "Business Name": "Phone Only Card",
                "Phone": "(440) 555-0100",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.jsonl"
            path.write_text(json.dumps(source) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Michael"}, clear=False):
                card = upload_airtable.read_input_records(path, packet_dir=Path(tmp))[0]
        self.assertEqual(card["airtable_fields"]["Assigned Setter"], "Michael")
        self.assertIn("Assigned Setter", card["defaulted_airtable_fields"])

    def test_jsonl_card_with_no_identity_is_left_to_the_upsert_key_error(self) -> None:
        source = {"business_name": "Nameless", "airtable_fields": {"Business Name": "Nameless"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.jsonl"
            path.write_text(json.dumps(source) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Michael"}, clear=False):
                card = upload_airtable.read_input_records(path, packet_dir=Path(tmp))[0]
        self.assertNotIn("Assigned Setter", card["airtable_fields"])
        with self.assertRaisesRegex(ValueError, "missing upsert key"):
            plan_airtable_sync([card])

    def test_jsonl_cannot_supply_a_different_configured_assignment(self) -> None:
        source = {
            "place_id_or_unique_key": "ChIJLANE",
            "upsert_key": "place:ChIJLANE",
            "business_name": "JSONL Lead",
            "airtable_fields": {
                "Business Name": "JSONL Lead",
                "Place Key": "ChIJLANE",
                "Assigned Setter": "Michael",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.jsonl"
            path.write_text(json.dumps(source) + "\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"LPM_SETTER_ROSTER": "Michael,Setter Two,Setter Three"},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "does not match deterministic assignment"):
                    upload_airtable.read_input_records(path, packet_dir=Path(tmp))

    def test_jsonl_assignment_is_removed_when_roster_is_unconfigured(self) -> None:
        source = {
            "place_id_or_unique_key": "ChIJ123",
            "upsert_key": "place:ChIJ123",
            "business_name": "JSONL Lead",
            "airtable_fields": {
                "Business Name": "JSONL Lead",
                "Place Key": "ChIJ123",
                "Assigned Setter": "Unregistered Person",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.jsonl"
            path.write_text(json.dumps(source) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"LPM_SETTER_ROSTER": ""}, clear=False):
                card = upload_airtable.read_input_records(path, packet_dir=Path(tmp))[0]
        self.assertNotIn("Assigned Setter", card["airtable_fields"])

    def test_live_upload_refuses_missing_choices_before_any_record_lookup_or_write(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cards.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            argv = [
                "upload_airtable.py",
                "--input", str(path),
                "--live",
                "--base-id", "appBase",
                "--table", "Prospects",
                "--token", "patToken",
            ]
            with (
                patch("sys.argv", argv),
                patch("upload_airtable.read_input_records", return_value=[]),
                patch(
                    "upload_airtable.assert_required_choices_ready",
                    side_effect=ValueError("missing Assigned Setter: Michael"),
                    create=True,
                ) as preflight,
                patch("upload_airtable.fetch_existing_records") as fetch,
                patch("upload_airtable.send_airtable_plan") as send,
                redirect_stderr(stderr),
            ):
                code = upload_airtable.main()

        self.assertEqual(code, 1)
        self.assertIn("missing Assigned Setter: Michael", stderr.getvalue())
        preflight.assert_called_once_with("appBase", "patToken", "Prospects")
        fetch.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
