from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import upload_airtable as ua
from lead_machine.airtable_sync import AIRTABLE_ALLOWED_FIELDS, normalize_airtable_value


# Every label choose_offer_fit() can return (score_leads.py:784-869). A typed
# select field whose value fails normalization is dropped from the payload
# silently, so the column goes blank in Airtable for that whole branch.
SCORER_OFFER_FITS = [
    "Website Rebuild",
    "Portfolio Rebuild",
    "Premium Perception Gap",
    "Mobile Quote Path Cleanup",
    "Review/Trust Proof Upgrade",
    "Service Page Cleanup",
    "Website Cleanup",
    "Advanced Optimization",
    "Estimate Funnel",
    "Tracking / Missed Call",
]
# Every site_status enrich_leads.py assigns.
ENRICHER_SITE_STATUSES = [
    "No Website",
    "Broken Website",
    "Broken",
    "Jobber Only",
    "Social Only",
    "Loaded",
    "Unknown",
]


LEGACY_FIELDS = {
    "Legacy Current Website Quality",
    "Legacy Send Trigger",
    "Legacy Last Contacted",
    "Legacy Recommended Action",
    "Legacy Form Strength",
    "Legacy CTA Strength",
    "Legacy Skip Reason",
}
CANONICAL_FIELDS = {
    "Current Website Quality",
    "Send Trigger",
    "Last Contacted",
    "Recommended Action",
    "Form Strength",
    "CTA Strength",
    "Skip Reason",
}


class SchemaReportContentTest(unittest.TestCase):
    def test_active_schema_has_canonical_fields_and_no_legacy_fields(self):
        schema_fields = {field for field, _, _ in ua._FIELD_SCHEMA}
        for active_fields in (schema_fields, set(ua.CSV_AIRTABLE_FIELDS), set(AIRTABLE_ALLOWED_FIELDS)):
            self.assertTrue(CANONICAL_FIELDS.issubset(active_fields))
            self.assertTrue(LEGACY_FIELDS.isdisjoint(active_fields))

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


class TypedFieldNormalizationCoverageTest(unittest.TestCase):
    """Every value the pipeline produces must survive the typed-field normalizer.

    These are the fields where a miss is invisible: normalize_airtable_value
    returns None, build_airtable_payload_with_drops discards the key, and the
    column reads as empty in Airtable rather than wrong.
    """

    def test_every_scorer_offer_fit_normalizes(self):
        for value in SCORER_OFFER_FITS:
            with self.subTest(offer_fit=value):
                self.assertIsNotNone(
                    normalize_airtable_value("Offer Fit", value),
                    f"choose_offer_fit returns {value!r} but it would be dropped from the payload",
                )

    def test_every_enricher_site_status_normalizes(self):
        for value in ENRICHER_SITE_STATUSES:
            with self.subTest(site_status=value):
                self.assertIsNotNone(
                    normalize_airtable_value("Site Status", value),
                    f"enrich_leads emits {value!r} but it would be dropped from the payload",
                )


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
