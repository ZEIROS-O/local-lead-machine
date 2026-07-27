"""The ICP closeability layer is dead on the path production actually runs.

`fit_ceiling` / `classify_priority` are reached only from `score_record`. In
`--mode api-llm` the grade is a bare threshold on the LLM total, so the ceiling
never executes and the two graders have never been compared. Batch 3 attaches
the ceiling as shadow columns and reports the disagreement rather than
enforcing or deleting it, because the measurement has to come first.

These tests protect two properties: the shadow never changes a grade, and it
never fails a scoring run.
"""
from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

import make_grade_disagreement_report as report
from score_leads import attach_icp_ceiling_shadow

csv.field_size_limit(sys.maxsize)


def _record(*, site_status: str = "Okay", rating: str = "4.9", reviews: str = "40") -> dict:
    return {
        "google": {
            "business_name": "Patio Pros",
            "city": "Cleveland",
            "rating": rating,
            "reviews": reviews,
            "vertical": "hardscaping",
        },
        "website": {
            "site_status": site_status,
            "site_issue_flags": [],
            "combined_text": "hardscape patio retaining wall",
            "high_ticket_terms": ["hardscape", "patio"],
        },
        "derived": {"has_high_ticket_terms": True},
    }


class CeilingShadowTest(unittest.TestCase):
    def test_shadow_records_the_ceiling_without_touching_the_grade(self) -> None:
        row = {"Lead Priority": "A", "Business Name": "Patio Pros"}

        attach_icp_ceiling_shadow(row, _record(reviews="400"), fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "A")
        self.assertIn(row["ICP Ceiling"], {"A", "B", "C", "Skip"})

    def test_a_grade_better_than_the_ceiling_is_flagged(self) -> None:
        row = {"Lead Priority": "A", "Business Name": "Patio Pros"}

        # A sub-4.4 rating is one of the ceiling's own reasons to cap at C.
        attach_icp_ceiling_shadow(row, _record(rating="4.2"), fallback_vertical="hardscaping")

        self.assertEqual(row["ICP Ceiling"], "C")
        self.assertEqual(row["Grade Disagreement"], "A_vs_C")
        self.assertIn("4.4", row["ICP Ceiling Flags"])

    def test_a_grade_at_or_below_the_ceiling_is_not_a_disagreement(self) -> None:
        # The ceiling is a cap. A grade it would not have changed is the ceiling
        # doing nothing, which is not a finding worth reporting.
        row = {"Lead Priority": "C", "Business Name": "Patio Pros"}

        attach_icp_ceiling_shadow(row, _record(rating="4.2"), fallback_vertical="hardscaping")

        self.assertEqual(row["ICP Ceiling"], "C")
        self.assertEqual(row["Grade Disagreement"], "")

    def test_a_broken_ceiling_computation_cannot_fail_the_run(self) -> None:
        row = {"Lead Priority": "A", "Business Name": "Patio Pros"}

        attach_icp_ceiling_shadow(row, {"google": None}, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Grade Disagreement"], "")
        self.assertIn("ceiling_unavailable", row["ICP Ceiling Flags"])


class DisagreementReportTest(unittest.TestCase):
    def _write_csv(self, directory: Path, rows: list[dict]) -> Path:
        path = directory / "scored.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_report_counts_disagreements_and_names_the_ceiling_reason(self) -> None:
        rows = [
            {
                "Business Name": "Over Graded",
                "Lead Priority": "A",
                "ICP Ceiling": "C",
                "ICP Ceiling Flags": "rating below preferred 4.4",
                "Grade Disagreement": "A_vs_C",
                "Sendable": "Yes",
            },
            {
                "Business Name": "Agrees",
                "Lead Priority": "C",
                "ICP Ceiling": "C",
                "ICP Ceiling Flags": "rating below preferred 4.4",
                "Grade Disagreement": "",
                "Sendable": "No",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(Path(tmp), rows)
            summary = report.summarise(report.read_scored_csv(path))
            text = report.render_report(summary, input_labels=[str(path)])

        self.assertEqual(summary["judged"], 2)
        self.assertEqual(summary["disagreements"], 1)
        self.assertEqual(summary["sendable_disagreements"], 1)
        self.assertIn("A_vs_C", text)
        self.assertIn("rating below preferred 4.4", text)

    def test_a_csv_without_shadow_columns_is_refused_rather_than_reported_as_zero(self) -> None:
        # Silently reporting "0 disagreements" for a CSV that never carried the
        # columns would read as agreement between the graders.
        rows = [{"Business Name": "Old Row", "Lead Priority": "A", "Sendable": "No"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_csv(Path(tmp), rows)
            with self.assertRaises(ValueError):
                report.read_scored_csv(path)

    def test_main_reports_a_missing_input_without_a_traceback(self) -> None:
        stderr = io.StringIO()
        with unittest.mock.patch(
            "sys.argv", ["make_grade_disagreement_report.py", "--input", "does/not/exist.csv"]
        ), redirect_stdout(io.StringIO()):
            with unittest.mock.patch("sys.stderr", stderr):
                code = report.main()

        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
