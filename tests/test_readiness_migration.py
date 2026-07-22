from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from refresh_packet_tiers import (
    SchemaMigrationError,
    check_csv,
    classify_schema,
    refresh_csv,
    refresh_row,
)


def complete_contract() -> dict[str, str]:
    return {
        "Place Key": "place-ready",
        "Business Name": "Ready Co",
        "City": "Cleveland",
        "Score Schema Version": "2",
        "Commercial Fit Source": "llm-schema-v2",
        "Commercial Fit State": "commercially-sendable",
        "Commercial Materiality": "Material consequence",
        "Recurring Consequence": "Recurring consequence",
        "Root Cause Confidence": "High confidence",
        "LPM Offer Component": "Get the Lead",
        "Packageable Solution": "Conversion repair",
        "Value Mechanism": "More completed inquiries",
        "Beyond Free Fix": "Multi-step repair",
        "Self Fix Risk": "Low",
        "Intentionality": "Not intentional",
        "Contact Eligibility": "contact-now",
        "Asset State": "none",
        "Promise State": "asset-free",
        "Asset ID": "",
        "Asset Verifier Verdict": "",
        "Sendable": "No",
        "Packet Eligible": "No",
    }


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class ReadinessMigrationTest(unittest.TestCase):
    def test_current_v2_contract_is_preserved(self) -> None:
        original = complete_contract()
        schema_class, errors = classify_schema(original)
        self.assertEqual(schema_class, "v2")
        self.assertEqual(errors, [])
        refreshed = refresh_row(original)
        self.assertEqual(refreshed["Score Schema Version"], 2)
        self.assertEqual(refreshed["Commercial Fit Source"], "llm-schema-v2")
        self.assertEqual(refreshed["Commercial Materiality"], original["Commercial Materiality"])
        self.assertEqual(refreshed["commercial_materiality"], original["Commercial Materiality"])

    def test_partial_contract_refuses_atomically_with_identity_and_fields(self) -> None:
        partial = complete_contract()
        partial.pop("Score Schema Version")
        partial.pop("Commercial Fit Source")
        partial.pop("Promise State")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            destination = Path(tmp) / "tiered.csv"
            write_rows(source, [partial])
            destination.write_text("sentinel\n", encoding="utf-8")
            before = destination.read_bytes()
            with self.assertRaisesRegex(SchemaMigrationError, "place-ready"):
                refresh_csv(source, destination)
            self.assertEqual(destination.read_bytes(), before)
            report = check_csv(source)
            self.assertFalse(report["safe_to_write"])
            self.assertEqual(report["schema_classes"], {"partial_v2": 1})
            self.assertIn("missing field promise_state", report["errors"][0])

    def test_conflicting_aliases_refuse_without_overwriting_destination(self) -> None:
        conflicting = complete_contract()
        conflicting["commercial_fit_state"] = "minor-fix/nurture"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            destination = Path(tmp) / "tiered.csv"
            write_rows(source, [conflicting])
            destination.write_text("sentinel\n", encoding="utf-8")
            before = destination.read_bytes()
            with self.assertRaisesRegex(SchemaMigrationError, "conflicting aliases commercial_fit_state"):
                refresh_csv(source, destination)
            self.assertEqual(destination.read_bytes(), before)
            report = check_csv(source)
            self.assertFalse(report["safe_to_write"])
            self.assertEqual(report["schema_classes"], {"invalid_v2": 1})

    def test_conflicting_schema_metadata_aliases_refuse_atomically(self) -> None:
        cases = (
            (
                {"Score Schema Version": "2", "score_schema_version": "1"},
                "score_schema_version",
            ),
            (
                {
                    "Score Schema Version": "2",
                    "Commercial Fit Source": "llm-schema-v2",
                    "commercial_fit_source": "deterministic-backfill",
                },
                "commercial_fit_source",
            ),
        )
        for metadata, conflict_key in cases:
            with self.subTest(conflict_key=conflict_key), tempfile.TemporaryDirectory() as tmp:
                conflicting = {**complete_contract(), **metadata}
                source = Path(tmp) / "source.csv"
                destination = Path(tmp) / "tiered.csv"
                write_rows(source, [conflicting])
                destination.write_text("sentinel\n", encoding="utf-8")
                before = destination.read_bytes()

                with self.assertRaisesRegex(SchemaMigrationError, f"conflicting aliases {conflict_key}"):
                    refresh_csv(source, destination)

                self.assertEqual(destination.read_bytes(), before)
                report = check_csv(source)
                self.assertFalse(report["safe_to_write"])
                self.assertEqual(report["schema_classes"], {"invalid_v2": 1})

    def test_equivalent_aliases_are_normalized_without_data_loss(self) -> None:
        equivalent = complete_contract()
        equivalent["commercial_fit_state"] = "  COMMERCIALLY-SENDABLE  "
        equivalent["commercial_materiality"] = " material consequence "
        schema_class, errors = classify_schema(equivalent)
        self.assertEqual(schema_class, "v2")
        self.assertEqual(errors, [])
        refreshed = refresh_row(equivalent)
        self.assertEqual(refreshed["Commercial Fit State"], "commercially-sendable")
        self.assertEqual(refreshed["commercial_fit_state"], "commercially-sendable")
        self.assertEqual(refreshed["Commercial Materiality"], "Material consequence")
        self.assertEqual(refreshed["commercial_materiality"], "Material consequence")

    def test_current_contract_preserves_existing_equivalent_source(self) -> None:
        equivalent = complete_contract()
        equivalent["Commercial Fit Source"] = "manual-reviewed"
        equivalent["commercial_fit_source"] = "  MANUAL-REVIEWED  "
        schema_class, errors = classify_schema(equivalent)
        self.assertEqual(schema_class, "v2")
        self.assertEqual(errors, [])

        refreshed = refresh_row(equivalent)

        self.assertEqual(refreshed["Commercial Fit Source"], "manual-reviewed")
        self.assertEqual(refreshed["commercial_fit_source"], "manual-reviewed")

    def test_complete_unversioned_contract_is_reported_but_refused(self) -> None:
        unversioned = complete_contract()
        unversioned.pop("Score Schema Version")
        unversioned.pop("Commercial Fit Source")
        schema_class, errors = classify_schema(unversioned)
        self.assertEqual(schema_class, "complete_unversioned_v2")
        self.assertEqual(errors, [])
        with self.assertRaisesRegex(SchemaMigrationError, "current schema v2 required"):
            refresh_row(unversioned)

    def test_refresh_refuses_legacy_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy.csv"
            destination = Path(tmp) / "tiered.csv"
            write_rows(source, [{"Place Key": "old", "Sendable": "Yes", "Packet Eligible": "Yes"}])
            destination.write_text("sentinel\n", encoding="utf-8")
            before = destination.read_bytes()
            with self.assertRaisesRegex(SchemaMigrationError, "current schema v2 required"):
                refresh_csv(source, destination)
            self.assertEqual(destination.read_bytes(), before)

    def test_check_reports_removed_legacy_sendable_without_writing(self) -> None:
        legacy = {
            "Place Key": "place-legacy",
            "Business Name": "Legacy Co",
            "City": "Cleveland",
            "Sendable": "Yes",
            "Packet Eligible": "Yes",
            "Site Status": "Loaded",
            "Best Service Angle": "Estimate Funnel",
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy.csv"
            write_rows(source, [legacy])
            before = source.read_bytes()
            report = check_csv(source)
            self.assertEqual(report["schema_classes"], {"legacy": 1})
            self.assertEqual(report["removed_sendable_identities"], ["place-legacy"])
            self.assertFalse(report["safe_to_write"])
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
