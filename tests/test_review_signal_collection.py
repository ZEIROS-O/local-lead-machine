from __future__ import annotations

import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import collect_review_signals as reviews


class ReviewCollectionSummaryTest(unittest.TestCase):
    def test_summary_distinguishes_complete_partial_pending_and_cost(self) -> None:
        self.assertEqual(
            reviews.collection_summary([]),
            {
                "status": "empty",
                "total": 0,
                "complete": 0,
                "pending": 0,
                "cost_usd": 0.0,
            },
        )
        partial = reviews.collection_summary(
            [
                {"state": "complete", "cost_usd": 0.17},
                {"state": "pending"},
            ]
        )
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["complete"], 1)
        self.assertEqual(partial["pending"], 1)
        self.assertEqual(partial["cost_usd"], 0.17)

    def test_merge_refuses_complete_signals_that_attach_to_no_lead(self) -> None:
        records = [
            {"google": {"place_id": "place-1", "business_name": "Lead A"}},
        ]
        signals = [
            {
                "task_id": "task-2",
                "state": "complete",
                "place_id": "place-2",
                "items": [{"review_id": "review-2"}],
            }
        ]
        with self.assertRaisesRegex(ValueError, "did not attach"):
            reviews.merge_review_records(records, signals)

    def test_merge_reports_exact_complete_signal_attachments(self) -> None:
        records = [
            {"google": {"place_id": "place-1", "business_name": "Lead A"}},
        ]
        signals = [
            {
                "task_id": "task-1",
                "state": "complete",
                "place_id": "place-1",
                "items": [{"review_id": "review-1"}],
                "source": "dataforseo_google_reviews_newest",
            }
        ]
        merged, summary = reviews.merge_review_records(records, signals)
        self.assertEqual(summary["complete_signals"], 1)
        self.assertEqual(summary["attached"], 1)
        self.assertEqual(len(merged[0]["review_items"]), 1)


class ReviewSubmitAccountingTest(unittest.TestCase):
    def test_submit_receipt_records_count_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "scored.csv"
            output_path = root / "receipt.json"
            with input_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "Business Name",
                        "Place Key",
                        "City",
                        "Scoring V3 Disposition",
                        "Scoring V3 Rank",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "Business Name": "Lead A",
                        "Place Key": "place-1",
                        "City": "Cleveland",
                        "Scoring V3 Disposition": "auto_eligible",
                        "Scoring V3 Rank": "90",
                    }
                )
            client = MagicMock()
            client.estimated_usd_per_task = 0.20
            client.budget.used_usd = 0.17
            client.submit_reviews.return_value = [{"id": "task-1", "cost": 0.17}]
            args = argparse.Namespace(
                input=str(input_path),
                output=str(output_path),
                limit=1,
                reject_sample=0,
                depth=60,
            )
            with patch.object(reviews, "client_from_env", return_value=client):
                self.assertEqual(reviews.submit(args), 0)

            receipt = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(receipt["selected_count"], 1)
        self.assertEqual(receipt["submitted_count"], 1)
        self.assertEqual(receipt["estimated_usd"], 0.20)
        self.assertEqual(receipt["cost_usd"], 0.17)


if __name__ == "__main__":
    unittest.main()
