from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import manual_lead_insert
import upload_airtable
from lead_machine import manual_lead_insert as mli


RAW_LEAD = """Business: ABC Concrete Coatings
Owner maybe Mike
Cleveland / Strongsville
Phone: 440-555-1234
Email: mike@abcconcrete.com
Website: https://abcconcrete.com
Google: https://maps.google.com/?cid=12345
Reviews 4.8 stars 37 reviews
No estimate form, old looking site, does garage floors and epoxy coatings
"""

MARKDOWN_RAW_LEAD = """Business: Flawless Ceramic Auto Detailing
Owner Name: Not found
City: Cleveland
Phone: 216-555-0199
Email: [detailer@example.com](mailto:detailer@example.com)
Website: [https://flawlesscad.com](https://flawlesscad.com)
Google: https://maps.google.com/?cid=987
Reviews 4.9 stars 88 reviews
Weak site, ceramic coating and PPF mentioned
"""

BOUNDLESS_RAW_LEAD = """Business: Boundless Car Care
City: Cleveland
Phone: 216-555-0144
Website: No standalone website — Google Business Profile links to Facebook
Booking Link: https://detailing-101090.square.site
Google Maps: Boundless Car Care – Detailing & Ceramic Coatings
Reviews 4.9 stars 42 reviews
Ceramic coatings and paint correction mentioned
"""


class ManualLeadInsertTest(unittest.TestCase):
    def _run_cli(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("sys.argv", argv),
            patch("sys.stdin", io.StringIO(stdin)),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = manual_lead_insert.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _dry_run_plan(self, stdout: str) -> list[dict[str, object]]:
        marker = "[DRY-RUN] Airtable plan follows"
        self.assertIn(marker, stdout)
        payload = stdout.split(marker, 1)[1].strip()
        return json.loads(payload)

    def test_regex_parser_extracts_identity_fields_from_messy_text(self) -> None:
        parsed = mli.parse_raw_lead_regex(RAW_LEAD)

        self.assertEqual(parsed["Business Name"], "ABC Concrete Coatings")
        self.assertEqual(parsed["Owner Name"], "Mike")
        self.assertEqual(parsed["City"], "Cleveland")
        self.assertEqual(parsed["Phone"], "440-555-1234")
        self.assertEqual(parsed["Email"], "mike@abcconcrete.com")
        self.assertEqual(parsed["Website URL"], "https://abcconcrete.com")
        self.assertEqual(parsed["Google Maps URL"], "https://maps.google.com/?cid=12345")
        self.assertEqual(parsed["Rating"], "4.8")
        self.assertEqual(parsed["Reviews"], "37")

    def test_regex_parser_normalizes_markdown_urls_emails_and_placeholders(self) -> None:
        parsed = mli.parse_raw_lead_regex(MARKDOWN_RAW_LEAD)

        self.assertEqual(parsed["Website URL"], "https://flawlesscad.com")
        self.assertEqual(parsed["Email"], "detailer@example.com")
        self.assertNotIn("Owner Name", parsed)

    def test_boundless_style_descriptive_url_labels_are_cleaned_before_validation(self) -> None:
        card = mli.build_manual_lead_card(BOUNDLESS_RAW_LEAD, vertical="detailing")
        fields = card["airtable_fields"]

        self.assertEqual(fields["Website URL"], "https://detailing-101090.square.site")
        self.assertNotIn("Google Maps URL", fields)
        self.assertNotIn("Current Website Quality", fields)
        self.assertEqual(fields["Site Status"], "Social Only")
        self.assertIn("No standalone website", fields["Specific Observation"])
        self.assertIn("Boundless Car Care – Detailing & Ceramic Coatings", fields["Specific Observation"])
        self.assertEqual(mli.validate_manual_lead_preflight(BOUNDLESS_RAW_LEAD, card), [])

    def test_llm_response_validation_rejects_invalid_json_shape(self) -> None:
        with self.assertRaises(ValueError) as caught:
            mli.validate_llm_fields({"Business Name": "Invented", "Unexpected": "bad"})

        self.assertIn("unexpected field", str(caught.exception).lower())

    def test_default_manual_row_sets_safe_defaults(self) -> None:
        card = mli.build_manual_lead_card(RAW_LEAD, vertical="epoxy")
        fields = card["airtable_fields"]

        self.assertEqual(fields["Status"], "Manual Review")
        self.assertEqual(fields["Sendable"], "No")
        self.assertEqual(fields["Packet Eligible"], "Yes")
        self.assertEqual(fields["Lead Priority"], "C")
        self.assertEqual(fields["AI Confidence"], "low")

    def test_weak_website_quality_is_not_synced(self) -> None:
        # Current Website Quality was removed from the Airtable surface (PR #9):
        # syncing it here would silently drop before ever reaching the base.
        card = mli.build_manual_lead_card(MARKDOWN_RAW_LEAD, vertical="detailing")

        self.assertNotIn("Current Website Quality", card["airtable_fields"])

    def test_vertical_validation_uses_shared_registry_for_new_lanes(self) -> None:
        for raw, slug in (
            ("garage doors", "garage_doors"),
            ("water restoration", "water_restoration"),
            ("epoxy", "epoxy"),
            ("plumbing", "plumbing"),
        ):
            card = mli.build_manual_lead_card(RAW_LEAD, vertical=raw)
            self.assertEqual(card["airtable_fields"]["Vertical"], slug)
            self.assertEqual(mli.validate_manual_lead_preflight(RAW_LEAD, card), [])

    def test_unknown_vertical_fails_before_any_processing(self) -> None:
        with self.assertRaises(ValueError) as caught:
            mli.build_manual_lead_card(RAW_LEAD, vertical="quantum remediation")

        message = str(caught.exception)
        self.assertIn("Unknown vertical 'quantum remediation'", message)
        self.assertIn("water_restoration", message)

    def test_place_key_falls_back_to_phone_domain_then_name_city(self) -> None:
        self.assertEqual(
            mli.deterministic_place_key({"Phone": "(440) 555-1234"}),
            "phone:4405551234",
        )
        self.assertEqual(
            mli.deterministic_place_key({"Website URL": "https://www.example.com/path"}),
            "domain:example.com",
        )
        self.assertEqual(
            mli.deterministic_place_key({"Business Name": "ABC Coatings", "City": "Strongsville"}),
            "name_city:abc-coatings:strongsville",
        )

    def test_google_place_id_wins_place_key_when_present(self) -> None:
        fields = {"Google Maps URL": "https://www.google.com/maps/place/?q=place_id:ChIJabc123"}
        self.assertEqual(mli.deterministic_place_key(fields), "place:ChIJabc123")

    def test_dry_run_does_not_call_airtable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("lead_machine.manual_lead_insert.fetch_existing_records") as fetch,
                patch("lead_machine.manual_lead_insert.send_airtable_plan") as send,
                patch("lead_machine.manual_lead_insert.validate_airtable_fields") as validate,
            ):
                code, stdout, stderr = self._run_cli(
                    [
                        "manual_lead_insert.py",
                        "--vertical",
                        "detailing",
                        "--no-llm",
                        "--output-dir",
                        tmp,
                    ],
                    stdin=RAW_LEAD,
                )

        self.assertEqual(code, 0, stderr)
        fetch.assert_not_called()
        send.assert_not_called()
        validate.assert_not_called()
        planned = self._dry_run_plan(stdout)
        self.assertEqual(planned[0]["method"], "POST")

    def test_dry_run_prints_validation_errors(self) -> None:
        bad = RAW_LEAD.replace("https://abcconcrete.com", "https://bad url")

        with tempfile.TemporaryDirectory() as tmp:
            code, stdout, stderr = self._run_cli(
                [
                    "manual_lead_insert.py",
                    "--vertical",
                    "detailing",
                    "--no-llm",
                    "--output-dir",
                    tmp,
                ],
                stdin=bad,
            )

        self.assertEqual(code, 1)
        self.assertIn("[VALIDATION]", stdout)
        self.assertIn("Website URL", stderr)

    def test_default_cli_does_not_call_openai_when_key_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True),
                patch("lead_machine.manual_lead_insert.extract_with_openai") as extract,
            ):
                code, _stdout, stderr = self._run_cli(
                    ["manual_lead_insert.py", "--vertical", "detailing", "--output-dir", tmp],
                    stdin=RAW_LEAD,
                )

        self.assertEqual(code, 0, stderr)
        extract.assert_not_called()

    def test_live_mode_requires_live_flag(self) -> None:
        parser = mli.build_arg_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--vertical", "detailing", "--write"])

    def test_live_mode_requires_env_before_network(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("lead_machine.manual_lead_insert.load_dotenv", return_value=False),
            patch("lead_machine.manual_lead_insert.fetch_existing_records") as fetch,
            patch("lead_machine.manual_lead_insert.send_airtable_plan") as send,
        ):
            code, _stdout, stderr = self._run_cli(
                ["manual_lead_insert.py", "--vertical", "landscaping", "--no-llm", "--live"],
                stdin=RAW_LEAD,
            )

        self.assertEqual(code, 1)
        fetch.assert_not_called()
        send.assert_not_called()
        self.assertIn("AIRTABLE_BASE_ID", stderr)

    def test_live_mode_preflight_rejects_invalid_local_values_before_airtable(self) -> None:
        bad = RAW_LEAD.replace("https://abcconcrete.com", "https://bad url")
        with (
            patch.dict(
                os.environ,
                {
                    "AIRTABLE_BASE_ID": "base",
                    "AIRTABLE_PROSPECTS_TABLE": "Prospects",
                    "AIRTABLE_PAT": "token",
                },
                clear=True,
            ),
            patch("lead_machine.manual_lead_insert.load_dotenv", return_value=False),
            patch("lead_machine.manual_lead_insert.validate_airtable_fields") as validate,
            patch("lead_machine.manual_lead_insert.fetch_existing_records") as fetch,
            patch("lead_machine.manual_lead_insert.send_airtable_plan") as send,
        ):
            code, stdout, stderr = self._run_cli(
                ["manual_lead_insert.py", "--vertical", "detailing", "--no-llm", "--live"],
                stdin=bad,
            )

        self.assertEqual(code, 1)
        self.assertIn("[VALIDATION]", stdout)
        self.assertIn("Website URL", stderr)
        validate.assert_not_called()
        fetch.assert_not_called()
        send.assert_not_called()

    def test_live_mode_rejects_multiple_business_sections_before_airtable(self) -> None:
        raw = """Business: First Detailer
Phone: 216-555-0101
Website: https://first.example

Business: Second Detailer
Phone: 216-555-0102
Website: https://second.example
"""
        with (
            patch.dict(
                os.environ,
                {
                    "AIRTABLE_BASE_ID": "base",
                    "AIRTABLE_PROSPECTS_TABLE": "Prospects",
                    "AIRTABLE_PAT": "token",
                },
                clear=True,
            ),
            patch("lead_machine.manual_lead_insert.load_dotenv", return_value=False),
            patch("lead_machine.manual_lead_insert.validate_airtable_fields") as validate,
            patch("lead_machine.manual_lead_insert.fetch_existing_records") as fetch,
            patch("lead_machine.manual_lead_insert.send_airtable_plan") as send,
        ):
            code, stdout, stderr = self._run_cli(
                ["manual_lead_insert.py", "--vertical", "detailing", "--no-llm", "--live"],
                stdin=raw,
            )

        self.assertEqual(code, 1)
        self.assertIn("[VALIDATION]", stdout)
        self.assertIn("multiple distinct Business sections", stderr)
        validate.assert_not_called()
        fetch.assert_not_called()
        send.assert_not_called()

    def test_preflight_rejects_invalid_email(self) -> None:
        card = mli.build_manual_lead_card(RAW_LEAD.replace("mike@abcconcrete.com", "mike@bad"), vertical="epoxy")

        errors = mli.validate_manual_lead_preflight(RAW_LEAD, card)

        self.assertTrue(any("Email" in error and "valid email" in error for error in errors), errors)

    def test_preflight_rejects_unsupported_single_select_value(self) -> None:
        card = mli.build_manual_lead_card(RAW_LEAD, vertical="epoxy")
        card["airtable_fields"]["Status"] = "New"

        errors = mli.validate_manual_lead_preflight(RAW_LEAD, card)

        self.assertTrue(any("Status" in error and "unsupported" in error for error in errors), errors)

    def test_preflight_rejects_missing_business_name(self) -> None:
        card = mli.build_manual_lead_card(RAW_LEAD, vertical="epoxy")
        card["airtable_fields"]["Business Name"] = ""

        errors = mli.validate_manual_lead_preflight(RAW_LEAD, card)

        self.assertIn("Business Name is required.", errors)

    def test_manual_lead_insert_does_not_require_packet_files(self) -> None:
        card = mli.build_manual_lead_card(RAW_LEAD, vertical="detailing")

        self.assertNotIn("Packet Path", card["airtable_fields"])
        self.assertEqual(card["airtable_fields"]["Packet Eligible"], "Yes")

    def test_live_plan_uses_existing_airtable_upsert_helpers(self) -> None:
        card = mli.build_manual_lead_card(RAW_LEAD, vertical="epoxy")
        plan = mli.plan_manual_airtable_sync(card, existing_records_by_key={card["upsert_key"]: "rec123"})

        self.assertEqual(plan[0]["method"], "PATCH")
        self.assertEqual(plan[0]["record_id"], "rec123")

    def test_existing_airtable_fetch_indexes_website_domains_for_manual_upsert(self) -> None:
        first = unittest.mock.MagicMock()
        first.status_code = 200
        first.json.return_value = {
            "records": [
                {
                    "id": "rec-domain",
                    "fields": {
                        "Business Name": "ABC Concrete Coatings",
                        "Website URL": "https://www.abcconcrete.com/services",
                    },
                }
            ]
        }

        with patch("upload_airtable.requests.get", return_value=first) as get:
            existing = upload_airtable.fetch_existing_records("base", "Prospects", "token")

        get.assert_called_once()
        self.assertEqual(existing["domain:abcconcrete.com"], "rec-domain")


if __name__ == "__main__":
    unittest.main()
