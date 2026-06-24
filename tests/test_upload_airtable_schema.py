from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import upload_airtable as ua


class SchemaReportContentTest(unittest.TestCase):
    def test_schema_report_includes_buyer_intent_signals(self):
        report = ua.build_schema_report()
        fields = [row["field"] for row in report]
        self.assertIn("Buyer Intent Signals", fields)

    def test_schema_report_marks_missing_fields(self):
        existing = {"Business Name", "Phone", "Email"}
        report = ua.build_schema_report(existing_field_names=existing)
        buyer_row = next(r for r in report if r["field"] == "Buyer Intent Signals")
        business_row = next(r for r in report if r["field"] == "Business Name")
        self.assertFalse(buyer_row["exists"])
        self.assertTrue(business_row["exists"])

    def test_schema_report_unknown_when_no_credentials(self):
        report = ua.build_schema_report(existing_field_names=None)
        buyer_row = next(r for r in report if r["field"] == "Buyer Intent Signals")
        self.assertIsNone(buyer_row["exists"])

    def test_schema_report_has_required_keys(self):
        report = ua.build_schema_report()
        for row in report:
            self.assertIn("field", row)
            self.assertIn("type", row)
            self.assertIn("exists", row)
            self.assertIn("notes", row)

    def test_schema_report_includes_all_required_fields(self):
        expected = {
            "Business Name", "Owner Name", "Vertical", "City", "Phone", "Email",
            "Website URL", "Google Maps URL", "Reviews", "Rating",
            "Current Website Quality", "Best Service Angle", "Specific Observation",
            "Packet Eligible", "Sendable", "Lead Priority", "Offer Fit",
            "Site Status", "Contact Form Detected", "Estimate Form Detected",
            "Form Strength", "Form Detection Method", "CTA Strength",
            "Visible Pain Score", "Visible Pain Reasons", "Send Trigger",
            "Money Signal", "Buyer Intent Signals", "Review Demand Band",
            "AI Confidence", "Score Total", "Opener", "Recommended Action",
            "Send Blockers", "Skip Reason", "Place Key", "Status",
            "Outreach Channel", "Next Action", "Last Contacted", "Next Follow-Up",
            "Assigned To", "Mockup URL", "Video URL", "Packet Path",
        }
        report_fields = {row["field"] for row in ua.build_schema_report()}
        self.assertTrue(expected.issubset(report_fields), f"Missing from report: {expected - report_fields}")


class SchemaReportNoUploadTest(unittest.TestCase):
    def test_schema_report_does_not_attempt_record_upload(self):
        captured = io.StringIO()
        with patch.object(ua, "fetch_existing_records") as mock_fetch, \
             patch.object(ua, "send_airtable_plan") as mock_send, \
             patch.object(ua, "validate_airtable_fields") as mock_validate, \
             patch("sys.stdout", captured):
            with patch("sys.argv", ["upload_airtable.py", "--schema-report"]):
                result = ua.main()
        self.assertFalse(mock_fetch.called, "fetch_existing_records must not be called in schema-report mode")
        self.assertFalse(mock_send.called, "send_airtable_plan must not be called in schema-report mode")
        self.assertFalse(mock_validate.called, "validate_airtable_fields must not be called in schema-report mode")
        self.assertEqual(result, 0)

    def test_schema_report_prints_field_names(self):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            with patch("sys.argv", ["upload_airtable.py", "--schema-report"]):
                ua.main()
        output = captured.getvalue()
        self.assertIn("Buyer Intent Signals", output)
        self.assertIn("Business Name", output)


class SchemaReportOutputTest(unittest.TestCase):
    def test_schema_report_output_writes_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "schema.md"
            with patch("sys.argv", [
                "upload_airtable.py", "--schema-report",
                "--schema-report-output", str(out_path),
            ]):
                with patch("sys.stdout", io.StringIO()):
                    ua.main()
            content = out_path.read_text(encoding="utf-8")
            self.assertIn("Buyer Intent Signals", content)
            self.assertIn("Business Name", content)

    def test_schema_report_output_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "schema.csv"
            with patch("sys.argv", [
                "upload_airtable.py", "--schema-report",
                "--schema-report-output", str(out_path),
            ]):
                with patch("sys.stdout", io.StringIO()):
                    ua.main()
            content = out_path.read_text(encoding="utf-8")
            self.assertIn("Buyer Intent Signals", content)
            self.assertIn("field", content.lower())


class LiveSyncFieldValidationTest(unittest.TestCase):
    def test_live_sync_refuses_when_fields_missing(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.text = 'UNKNOWN_FIELD_NAME: "Buyer Intent Signals"'
        with patch("requests.get", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                ua.validate_airtable_fields(
                    "base123", "Prospects", "tok123", ["Business Name", "Buyer Intent Signals"]
                )

    def test_live_sync_passes_when_all_fields_exist(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"records": []}
        with patch("requests.get", return_value=mock_resp):
            ua.validate_airtable_fields("base123", "Prospects", "tok123", ["Business Name"])


if __name__ == "__main__":
    unittest.main()
