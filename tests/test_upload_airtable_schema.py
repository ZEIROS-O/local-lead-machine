from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import upload_airtable as ua
from airtable_schema_cutover import load_fields_config
from lead_machine.airtable_sync import (
    AIRTABLE_ALLOWED_FIELDS,
    build_airtable_payload,
    normalize_airtable_value,
)
from lead_machine.icp_lanes import LANES
from lead_machine.queue_state import PROSPECT_STATUS_CHOICES

REPO_ROOT = Path(__file__).resolve().parent.parent
CUTOVER_CONFIG = REPO_ROOT / "airtable_cutover_fields_2026-07.json"


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
# Every site_status enrich_leads.py assigns (deterministic path) plus every
# value evidence._status_from_site returns, which manual_scores writes straight
# into the same column on the LLM path. The two use different vocabularies.
ENRICHER_SITE_STATUSES = [
    "No Website",
    "Broken Website",
    "Broken",
    "Jobber Only",
    "Social Only",
    "Loaded",
    "Unknown",
]
EVIDENCE_SITE_STATUSES = [
    "missing",
    "broken",
    "weak",
    "okay",
    "social_only",
    "jobber_only",
    "unknown",
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
# Survivors of the Legacy* cutover that are still part of the active schema.
CANONICAL_FIELDS = {
    "Send Trigger",
    "Last Contacted",
}
# `Skip Reason` was the canonical half of a Legacy* pair the 2026-07-30 cutover
# migrated onto. The verdict collapse then superseded it outright — the reason a
# lead is not sendable lives in `Verdict Reasons` now — so step 4 deletes the
# column and the write path stopped naming it first.
SUPERSEDED_BY_VERDICT = {"Skip Reason"}
# The 13 columns the writer used to fill that the 2026-07 CRM rebuild deletes.
# Airtable answers a write to a deleted field with a 422 for the whole record,
# so the code has to stop writing them before the base loses them — never the
# other way round.
CUTOVER_2026_07_WRITTEN_FIELDS = {
    "Form Strength",
    "Commercial Materiality",
    "Recurring Consequence",
    "Root Cause Confidence",
    "Packageable Solution",
    "Value Mechanism",
    "Beyond Free Fix",
    "Self Fix Risk",
    "Intentionality",
    "Asset ID",
    "Asset Verifier Verdict",
    "Evidence Snapshot",
    "Recommended Action",
}
# Columns the rebuild adds. Both are derived from icp_lanes at upload time.
NEW_CRM_FIELDS = ("ICP Lane", "Opportunity Score")
# Explicitly deferred until a producer and taxonomy are approved.
DEFERRED_CRM_FIELDS = {"Selected Opportunity"}
# Canonical halves of a Legacy* pair that have since been demoted off the
# Airtable surface entirely. Their Legacy* counterparts are tracked as
# airtable_schema_cutover.LEGACY_ORPHAN_FIELDS — deleted outright, not migrated.
DEMOTED_CANONICAL_FIELDS = {
    "Current Website Quality",
    "CTA Strength",
}


class SchemaReportContentTest(unittest.TestCase):
    def test_active_schema_has_canonical_fields_and_no_legacy_fields(self):
        schema_fields = {field for field, _, _ in ua._FIELD_SCHEMA}
        for active_fields in (schema_fields, set(ua.CSV_AIRTABLE_FIELDS), set(AIRTABLE_ALLOWED_FIELDS)):
            self.assertTrue(CANONICAL_FIELDS.issubset(active_fields))
            self.assertTrue(LEGACY_FIELDS.isdisjoint(active_fields))
            self.assertTrue(DEMOTED_CANONICAL_FIELDS.isdisjoint(active_fields))
            self.assertTrue(DEFERRED_CRM_FIELDS.isdisjoint(active_fields))

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
            "Specific Observation",
            "Lead Priority", "Offer Fit",
            "Site Status",
            "Visible Pain Score", "Visible Pain Reasons", "Send Trigger",
            "Buyer Intent Signals", "AI Confidence", "Score Total",
            "ICP Lane", "Opportunity Score",
            "Opener",
            # `Packet Tier`, `Send Blockers` and `Skip Reason` are gone from the
            # report on purpose — step 4 of the verdict cutover deletes them, and
            # the report is the list of columns the base is required to have.
            "Place Key", "Status",
            "Outreach Channel", "Last Contacted", "Next Follow-Up",
            "Mockup URL", "Video URL", "Packet Path",
        }
        report_fields = {row["field"] for row in ua.build_schema_report()}
        self.assertTrue(expected.issubset(report_fields), f"Missing from report: {expected - report_fields}")

    def test_status_schema_report_uses_the_canonical_vocabulary(self):
        status = next(row for row in ua.build_schema_report() if row["field"] == "Status")
        self.assertEqual(
            status["notes"],
            "Options: " + ", ".join(sorted(PROSPECT_STATUS_CHOICES)),
        )


class CutoverFieldSwapTest(unittest.TestCase):
    """The write path and the deletion checklist must not overlap.

    A field in both is the 422 the whole cutover exists to avoid: the runbook
    deletes the column, the next upload still sends it, and Airtable rejects the
    entire record rather than the one key.
    """

    def test_code_writes_nothing_the_shipped_cutover_config_deletes(self):
        doomed = set(load_fields_config(CUTOVER_CONFIG))
        schema_fields = {field for field, _, _ in ua._FIELD_SCHEMA}
        for written in (
            schema_fields,
            set(ua.CSV_AIRTABLE_FIELDS),
            set(AIRTABLE_ALLOWED_FIELDS),
        ):
            self.assertEqual(doomed & written, set())

    def test_shipped_cutover_config_still_covers_every_field_the_writer_dropped(self):
        # The other direction: a field pulled out of the writer with nothing
        # deleting it in Airtable leaves a stale column nobody maintains.
        self.assertTrue(
            CUTOVER_2026_07_WRITTEN_FIELDS.issubset(set(load_fields_config(CUTOVER_CONFIG)))
        )

    def test_new_crm_fields_are_in_both_the_write_path_and_the_schema_report(self):
        schema_fields = {field for field, _, _ in ua._FIELD_SCHEMA}
        for field in NEW_CRM_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, ua.CSV_AIRTABLE_FIELDS)
                self.assertIn(field, schema_fields)


def scored_row(**overrides) -> dict:
    """A minimal scored CSV row, including the Raw Evidence record."""
    record = {
        "google": {
            "business_name": "A+ Patio Pros",
            "website_url": "",
            "phone": "(440) 555-0100",
            "rating": "4.9",
            "reviews": "19",
            "category": "landscaper",
            "source_query": "hardscaping Cleveland OH",
            "vertical": "hardscaping",
            "place_id": "ChIJLANE",
        },
        "website": {
            "site_status": "missing",
            "combined_text": "hardscape patio retaining wall estimate",
            "high_ticket_terms": ["hardscape", "patio"],
        },
        "evidence": {
            "site_status": "missing",
            "rendered_site_status": "Unknown",
            "raw_flags": [],
            "visible_pain_categories": ["no_site_or_social_only_pain"],
            "visible_pain_score": 40,
            "visible_pain_reasons": ["No owned website found"],
            "send_trigger": "No owned website found",
            "form_strength": "None",
            "phone_visible": True,
        },
    }
    row = {
        "Business Name": "A+ Patio Pros",
        "City": "Cleveland",
        "Phone": "(440) 555-0100",
        "Place Key": "ChIJLANE",
        "Lead Priority": "A",
        "Packet Tier": "Sendable Packet",
        "Raw Evidence": json.dumps(record),
    }
    row.update(overrides)
    return row


class WritePathFieldSwapTest(unittest.TestCase):
    def _fields(self, row: dict) -> dict:
        return ua.csv_row_to_record(row, Path("packets_does_not_exist"))["airtable_fields"]

    def _final_fields(self, row: dict) -> dict:
        card = ua.csv_row_to_record(row, Path("packets_does_not_exist"))
        return build_airtable_payload(card)["fields"]

    def test_icp_lane_and_opportunity_score_are_filled_from_the_record(self):
        # The point of the two new columns: a real producer, not a blank column.
        fields = self._fields(scored_row())
        self.assertIn(fields["ICP Lane"], LANES)
        self.assertEqual(fields["ICP Lane"], "no_site_launch")
        self.assertGreater(fields["Opportunity Score"], 0)

    def test_opportunity_score_is_written_as_a_number(self):
        # No write path sets typecast, so a stringified number into an Airtable
        # Number field is a 422, not a coercion.
        fields = self._fields(scored_row())
        self.assertIsInstance(fields["Opportunity Score"], int)
        self.assertNotIsInstance(fields["Opportunity Score"], bool)

    def test_new_crm_fields_survive_the_shared_final_payload_builder(self):
        fields = self._final_fields(scored_row())
        self.assertEqual(fields["ICP Lane"], "no_site_launch")
        self.assertIsInstance(fields["Opportunity Score"], int)
        self.assertGreater(fields["Opportunity Score"], 0)

    def test_a_derived_lane_overrides_a_stale_column_on_the_row(self):
        fields = self._fields(scored_row(**{"ICP Lane": "estimate_funnel"}))
        self.assertEqual(fields["ICP Lane"], "no_site_launch")

    def test_no_lane_is_invented_when_the_record_cannot_be_recovered(self):
        for label, raw in (("absent", None), ("blank", ""), ("unparseable", "{nope")):
            with self.subTest(raw_evidence=label):
                row = scored_row()
                if raw is None:
                    del row["Raw Evidence"]
                else:
                    row["Raw Evidence"] = raw
                fields = self._fields(row)
                self.assertNotIn("ICP Lane", fields)
                self.assertNotIn("Opportunity Score", fields)

    def test_a_row_still_carrying_the_dropped_columns_uploads_without_them(self):
        # Scored CSVs written before this cut still have all 13 columns, and
        # they keep flowing through the pipeline for months.
        stale = {field: f"legacy {field}" for field in CUTOVER_2026_07_WRITTEN_FIELDS}
        with_stale = self._fields(scored_row(**stale))
        without_stale = self._fields(scored_row())
        self.assertEqual(CUTOVER_2026_07_WRITTEN_FIELDS & set(with_stale), set())
        # Dropping them changes nothing else about the record.
        self.assertEqual(with_stale, without_stale)

    def test_a_field_absent_from_the_row_is_absent_from_the_payload(self):
        # Absent, not blank: a blank would overwrite a value already in Airtable
        # on the PATCH half of the upsert.
        fields = self._fields(scored_row())
        self.assertNotIn("Consent Posture", fields)
        self.assertNotIn("Mockup URL", fields)


class LegacyOrphanRegistrationTest(unittest.TestCase):
    def test_every_demoted_canonical_has_its_legacy_half_registered_as_an_orphan(self):
        from airtable_schema_cutover import LEGACY_FIELD_PAIRS, LEGACY_ORPHAN_FIELDS

        for canonical in DEMOTED_CANONICAL_FIELDS:
            with self.subTest(canonical=canonical):
                self.assertNotIn(canonical, set(LEGACY_FIELD_PAIRS.values()))
                self.assertIn(f"Legacy {canonical}", LEGACY_ORPHAN_FIELDS)

    def test_every_surviving_pair_still_points_at_a_live_column(self):
        """A migration must not strand its target — unless a later one retires it.

        `LEGACY_FIELD_PAIRS` is the record of the completed 2026-07-30 migration,
        so it keeps naming `Skip Reason` even though the verdict collapse has
        since superseded that column. Both halves of the pair are gone from the
        base; what must stay true is that no *other* pair points at nothing.
        """
        from airtable_schema_cutover import LEGACY_FIELD_PAIRS

        still_expected = set(LEGACY_FIELD_PAIRS.values()) - SUPERSEDED_BY_VERDICT
        self.assertTrue(still_expected.issubset(set(AIRTABLE_ALLOWED_FIELDS)))
        self.assertTrue(SUPERSEDED_BY_VERDICT.isdisjoint(set(AIRTABLE_ALLOWED_FIELDS)))


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

    def test_every_evidence_site_status_normalizes(self):
        for value in EVIDENCE_SITE_STATUSES:
            with self.subTest(site_status=value):
                self.assertIsNotNone(
                    normalize_airtable_value("Site Status", value),
                    f"evidence._status_from_site returns {value!r} but it would be "
                    "dropped from the payload",
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
