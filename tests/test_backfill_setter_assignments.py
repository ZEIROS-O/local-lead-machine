from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

import backfill_setter_assignments as backfill


ROSTER = ("Michael", "Setter Two", "Setter Three")


def prospect(
    record_id: str,
    place_key: str,
    *,
    assigned_setter: object = "",
    business_name: str = "Acme Services",
    phone: str = "",
) -> dict:
    fields = {
        "Business Name": business_name,
        "Place Key": place_key,
    }
    if phone:
        fields["Phone"] = phone
    if assigned_setter != "":
        fields["Assigned Setter"] = assigned_setter
    return {"id": record_id, "fields": fields}


class BackfillPlannerTest(unittest.TestCase):
    def test_only_blank_assignments_are_patched(self) -> None:
        planned, _ = backfill.plan_setter_assignment_backfill(
            [
                prospect("recBlank", "ChIJLANE"),
                prospect("recManual", "ChIJ123", assigned_setter="Setter Two"),
            ],
            ROSTER,
        )

        self.assertEqual(
            planned,
            [
                {
                    "method": "PATCH",
                    "record_id": "recBlank",
                    "place_key": "ChIJLANE",
                    "upsert_key": "place:ChIJLANE",
                    "payload": {"fields": {"Assigned Setter": "Setter Three"}},
                }
            ],
        )

    def test_manual_reassignment_survives_even_when_hash_would_choose_another_rep(self) -> None:
        planned, _ = backfill.plan_setter_assignment_backfill(
            [prospect("recManual", "ChIJLANE", assigned_setter="Michael")],
            ROSTER,
        )
        self.assertEqual(planned, [])

    def test_select_object_from_schema_tools_also_counts_as_assigned(self) -> None:
        planned, _ = backfill.plan_setter_assignment_backfill(
            [
                prospect(
                    "recManual",
                    "ChIJLANE",
                    assigned_setter={"id": "sel1", "name": "Michael"},
                )
            ],
            ROSTER,
        )
        self.assertEqual(planned, [])

    def test_a_phone_only_row_is_backfilled_on_its_phone_identity(self) -> None:
        planned, skipped = backfill.plan_setter_assignment_backfill(
            [prospect("recPhone", "", phone="(440) 555-0100")],
            ROSTER,
        )
        self.assertEqual(skipped, [])
        self.assertEqual(planned[0]["upsert_key"], "phone:4405550100")
        self.assertEqual(
            planned[0]["payload"]["fields"]["Assigned Setter"],
            backfill.assign_setter("phone:4405550100", ROSTER),
        )

    def test_an_unidentifiable_row_is_skipped_without_withholding_the_rest(self) -> None:
        # The whole point of the change: one legacy row with no identifier used
        # to abort the plan for every other blank assignment in the base.
        planned, skipped = backfill.plan_setter_assignment_backfill(
            [
                prospect("recMissing", "", business_name="No Identity Co"),
                prospect("recOk", "ChIJLANE"),
            ],
            ROSTER,
        )
        self.assertEqual([item["record_id"] for item in planned], ["recOk"])
        self.assertEqual(skipped[0]["record_id"], "recMissing")
        self.assertEqual(skipped[0]["business_name"], "No Identity Co")
        self.assertIn("no Place Key or Phone", skipped[0]["reason"])

    def test_an_already_assigned_row_without_identity_is_neither_planned_nor_skipped(self) -> None:
        planned, skipped = backfill.plan_setter_assignment_backfill(
            [prospect("recManual", "", assigned_setter="Michael")],
            ROSTER,
        )
        self.assertEqual(planned, [])
        self.assertEqual(skipped, [])

    def test_duplicate_place_keys_refuse_the_entire_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate Place Key"):
            backfill.plan_setter_assignment_backfill(
                [prospect("rec1", "ChIJDup"), prospect("rec2", "ChIJDup")],
                ROSTER,
            )

    def test_summary_reports_actual_distribution_without_claiming_exact_balance(self) -> None:
        planned, _ = backfill.plan_setter_assignment_backfill(
            [
                prospect("rec1", "ChIJLANE"),
                prospect("rec2", "ChIJ123"),
            ],
            ROSTER,
        )
        self.assertEqual(
            backfill.assignment_counts(planned),
            {"Michael": 1, "Setter Three": 1},
        )


class BackfillReadBoundaryTest(unittest.TestCase):
    def test_fetch_is_paginated_and_requests_only_backfill_fields(self) -> None:
        first = MagicMock()
        first.status_code = 200
        first.json.return_value = {
            "records": [prospect("rec1", "ChIJLANE")],
            "offset": "page-2",
        }
        second = MagicMock()
        second.status_code = 200
        second.json.return_value = {
            "records": [prospect("rec2", "ChIJ123")],
        }

        with patch("backfill_setter_assignments.requests.get", side_effect=[first, second]) as get:
            records = backfill.fetch_prospects("appBase", "Prospects", "patToken")

        self.assertEqual([record["id"] for record in records], ["rec1", "rec2"])
        first_params = get.call_args_list[0].kwargs["params"]
        second_params = get.call_args_list[1].kwargs["params"]
        self.assertEqual(
            first_params,
            [
                ("pageSize", 100),
                ("fields[]", "Assigned Setter"),
                ("fields[]", "Business Name"),
                ("fields[]", "Place Key"),
                ("fields[]", "Phone"),
            ],
        )
        self.assertIn(("offset", "page-2"), second_params)


class BackfillApplySafetyTest(unittest.TestCase):
    def test_manual_assignment_added_after_planning_is_not_overwritten(self) -> None:
        current = MagicMock(status_code=200)
        current.json.return_value = {
            "id": "rec1",
            "fields": {"Assigned Setter": "Human Choice"},
        }
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = current
        with patch("backfill_setter_assignments.requests.Session", return_value=session):
            applied = backfill.apply_blank_assignment_backfill(
                "appBase",
                "Prospects",
                "patToken",
                [
                    {
                        "method": "PATCH",
                        "record_id": "rec1",
                        "place_key": "ChIJ123",
                        "upsert_key": "place:ChIJ123",
                        "payload": {"fields": {"Assigned Setter": "Michael"}},
                    }
                ],
            )

        self.assertEqual(applied, 0)
        session.request.assert_not_called()

    def test_blank_record_is_patched_and_response_is_verified(self) -> None:
        current = MagicMock(status_code=200)
        current.json.return_value = {"id": "rec1", "fields": {}}
        written = MagicMock(status_code=200)
        written.json.return_value = {
            "id": "rec1",
            "fields": {"Assigned Setter": "Michael"},
        }
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = current
        session.request.return_value = written
        with patch("backfill_setter_assignments.requests.Session", return_value=session):
            applied = backfill.apply_blank_assignment_backfill(
                "appBase",
                "Prospects",
                "patToken",
                [
                    {
                        "method": "PATCH",
                        "record_id": "rec1",
                        "place_key": "ChIJ123",
                        "upsert_key": "place:ChIJ123",
                        "payload": {"fields": {"Assigned Setter": "Michael"}},
                    }
                ],
            )

        self.assertEqual(applied, 1)
        self.assertEqual(session.request.call_args.args[0], "PATCH")
        self.assertNotIn("typecast", session.request.call_args.kwargs["json"])

    def test_recheck_sends_no_list_only_parameters(self) -> None:
        # `fields[]` is a list-records parameter. Sending it to the
        # single-record endpoint 422s the request as INVALID_REQUEST_UNKNOWN,
        # which took the whole backfill down on its first row against the live
        # base. Mocked sessions cannot see that, so pin the call shape.
        current = MagicMock(status_code=200)
        current.json.return_value = {"id": "rec1", "fields": {}}
        written = MagicMock(status_code=200)
        written.json.return_value = {"id": "rec1", "fields": {"Assigned Setter": "Michael"}}
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = current
        session.request.return_value = written
        with patch("backfill_setter_assignments.requests.Session", return_value=session):
            backfill.apply_blank_assignment_backfill(
                "appBase",
                "Prospects",
                "patToken",
                [
                    {
                        "method": "PATCH",
                        "record_id": "rec1",
                        "place_key": "ChIJ123",
                        "upsert_key": "place:ChIJ123",
                        "payload": {"fields": {"Assigned Setter": "Michael"}},
                    }
                ],
            )

        self.assertNotIn("params", session.get.call_args.kwargs)
        self.assertTrue(session.get.call_args.args[0].endswith("/rec1"))

    def test_patch_response_must_confirm_the_assignment(self) -> None:
        current = MagicMock(status_code=200)
        current.json.return_value = {"id": "rec1", "fields": {}}
        written = MagicMock(status_code=200)
        written.json.return_value = {"id": "rec1", "fields": {}}
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = current
        session.request.return_value = written
        with (
            patch("backfill_setter_assignments.requests.Session", return_value=session),
            self.assertRaisesRegex(RuntimeError, "did not confirm Assigned Setter"),
        ):
            backfill.apply_blank_assignment_backfill(
                "appBase",
                "Prospects",
                "patToken",
                [
                    {
                        "method": "PATCH",
                        "record_id": "rec1",
                        "place_key": "ChIJ123",
                        "upsert_key": "place:ChIJ123",
                        "payload": {"fields": {"Assigned Setter": "Michael"}},
                    }
                ],
            )


class BackfillCommandSafetyTest(unittest.TestCase):
    def test_backfill_has_no_receipt_resume_mode_that_can_override_fresh_blank_state(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            backfill.parse_args(["--resume"])

    def test_live_rerun_uses_current_roster_for_a_currently_blank_record(self) -> None:
        with (
            patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Replacement Setter"}, clear=False),
            patch("backfill_setter_assignments.assert_required_choices_ready"),
            patch(
                "backfill_setter_assignments.fetch_prospects",
                return_value=[prospect("rec1", "ChIJ123")],
            ),
            patch(
                "backfill_setter_assignments.apply_blank_assignment_backfill",
                return_value=1,
            ) as apply,
            redirect_stdout(io.StringIO()),
        ):
            code = backfill.main(
                [
                    "--live",
                    "--base-id", "appBase",
                    "--table", "Prospects",
                    "--token", "patToken",
                ]
            )

        self.assertEqual(code, 0)
        planned = apply.call_args.args[3]
        self.assertEqual(
            planned[0]["payload"]["fields"]["Assigned Setter"],
            "Replacement Setter",
        )

    def test_live_schema_refusal_happens_before_record_read_or_write(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Michael"}, clear=False),
            patch(
                "backfill_setter_assignments.assert_required_choices_ready",
                side_effect=ValueError("missing Do Not Contact"),
            ),
            patch("backfill_setter_assignments.fetch_prospects") as fetch,
            patch("backfill_setter_assignments.apply_blank_assignment_backfill") as send,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = backfill.main(
                [
                    "--live",
                    "--base-id",
                    "appBase",
                    "--table",
                    "Prospects",
                    "--token",
                    "patToken",
                ]
            )

        self.assertEqual(code, 1)
        self.assertIn("missing Do Not Contact", stderr.getvalue())
        fetch.assert_not_called()
        send.assert_not_called()

    def test_dry_run_prints_the_plan_without_calling_live_preflight_or_write(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"LPM_SETTER_ROSTER": "Michael"}, clear=False),
            patch(
                "backfill_setter_assignments.fetch_prospects",
                return_value=[prospect("rec1", "ChIJ123")],
            ),
            patch("backfill_setter_assignments.assert_required_choices_ready") as preflight,
            patch("backfill_setter_assignments.apply_blank_assignment_backfill") as send,
            redirect_stdout(stdout),
        ):
            code = backfill.main(
                [
                    "--dry-run",
                    "--base-id",
                    "appBase",
                    "--table",
                    "Prospects",
                    "--token",
                    "patToken",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn('"blank_assignments": 1', stdout.getvalue())
        self.assertIn('"Assigned Setter": "Michael"', stdout.getvalue())
        preflight.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
