from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lead_machine.external_signals import (
    BudgetExceeded,
    DataForSEOReviewsClient,
    RunBudget,
    SemrushMapRankClient,
    select_deep_enrichment,
)
from lead_machine.scoring_calibration import (
    PILOT_CELLS,
    attach_predictions,
    build_blind_sample,
    cohens_kappa,
    evaluate_calibration,
)
from lead_machine.signals import SignalObservation


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


class ExternalSignalTest(unittest.TestCase):
    def test_unknown_signal_cannot_silently_assert_a_value(self) -> None:
        with self.assertRaises(ValueError):
            SignalObservation(signal_id="lsa", kind="ad_activity", state="unknown", value=False)

    def test_budget_fails_closed_before_external_request(self) -> None:
        session = _Session({"status_code": 20000, "tasks": []})
        client = DataForSEOReviewsClient(
            login="user",
            password="secret",
            budget=RunBudget(max_usd=0.10),
            estimated_usd_per_task=0.20,
            session=session,
        )

        with self.assertRaises(BudgetExceeded):
            client.submit_reviews([{"place_id": "ChIJ1"}], depth=60)
        self.assertEqual(session.calls, [])

    def test_dataforseo_tasks_are_newest_first_and_bounded(self) -> None:
        session = _Session(
            {"status_code": 20000, "tasks": [{"id": "task-1", "status_code": 20100, "data": {}}]}
        )
        client = DataForSEOReviewsClient(
            login="user",
            password="secret",
            budget=RunBudget(max_usd=10),
            session=session,
        )

        tasks = client.submit_reviews([{"place_id": "ChIJ1", "tag": "lead-1"}], depth=60)

        self.assertEqual(tasks[0]["id"], "task-1")
        payload = session.calls[0][1]["json"][0]
        self.assertEqual(payload["sort_by"], "newest")
        self.assertEqual(payload["depth"], 60)

    def test_deep_selector_reserves_five_deterministic_reject_audits(self) -> None:
        rows = [
            {
                "Place Key": f"p{index}",
                "Scoring V3 Disposition": "reject" if index >= 25 else "manual-review",
                "Scoring V3 Rank": str(100 - index),
            }
            for index in range(40)
        ]

        selected = select_deep_enrichment(rows)

        self.assertEqual(len(selected), 25)
        self.assertEqual(sum(row["Scoring V3 Disposition"] == "reject" for row in selected), 5)

    def test_semrush_observation_preserves_required_collection_context(self) -> None:
        session = _Session(
            {
                "data": {
                    "average_positions": {"2026-07-19T12:00:00Z": 12.0},
                    "share_of_voices": {"2026-07-19T12:00:00Z": 3.5},
                }
            }
        )
        client = SemrushMapRankClient(token="token", budget=RunBudget(), session=session)

        result = client.keyword_metrics(
            campaign_id="c1",
            keyword_id="k1",
            place_id="p1",
            query="ceramic coating",
            grid="7x7@1mi",
            device="mobile",
            location="Cleveland, OH",
        )

        self.assertEqual(result["observed_at"], "2026-07-19T12:00:00Z")
        self.assertEqual(result["observation"]["metadata"]["query"], "ceramic coating")
        self.assertEqual(result["observation"]["metadata"]["grid"], "7x7@1mi")


class CalibrationTest(unittest.TestCase):
    def test_blind_sample_is_160_unique_plus_40_audit_rows(self) -> None:
        rows = []
        for cell in PILOT_CELLS:
            for index in range(40):
                rows.append(
                    {
                        "Scoring Profile ID": cell,
                        "Place Key": f"{cell}-{index}",
                        "Business Name": f"Lead {index}",
                        "Scoring V3 Rank": "75",
                        "Sendable": "Yes",
                    }
                )

        primary, audit = build_blind_sample(rows)

        self.assertEqual(len(primary), 160)
        self.assertEqual(len(audit), 40)
        self.assertEqual(sum(row["Calibration Split"] == "holdout" for row in primary), 40)
        self.assertNotIn("Scoring V3 Rank", primary[0])
        self.assertNotIn("Sendable", primary[0])

    def test_hidden_predictions_are_restored_by_stable_lead_key(self) -> None:
        labeled = [{"Place Key": "p1", "Human Disposition": "auto-eligible"}]
        predictions = [
            {
                "Place Key": "p1",
                "Scoring Profile ID": "detailing__cleveland",
                "Scoring V3 Disposition": "auto-eligible",
            }
        ]

        restored = attach_predictions(labeled, predictions)

        self.assertEqual(restored[0]["Scoring V3 Disposition"], "auto-eligible")

    def test_acceptance_report_enforces_aggregate_and_cell_gates(self) -> None:
        rows = []
        for cell in PILOT_CELLS:
            for index in range(10):
                rows.append(
                    {
                        "Scoring Profile ID": cell,
                        "Scoring V3 Disposition": "auto-eligible",
                        "Human Disposition": "auto-eligible" if index < 9 else "manual-review",
                    }
                )
            rows.append(
                {
                    "Scoring Profile ID": cell,
                    "Scoring V3 Disposition": "reject",
                    "Human Disposition": "reject",
                }
            )

        audit_pairs = [("auto-eligible", "auto-eligible")] * 8 + [("reject", "reject")] * 2
        report = evaluate_calibration(rows, audit_pairs=audit_pairs)

        self.assertTrue(report["launch_passed"])
        self.assertEqual(report["aggregate"]["auto_eligible_approval_rate"], 0.9)
        self.assertTrue(report["audit_kappa_passed"])
        self.assertIn("unknown", report["by_offer_lane"])

    def test_kappa_failure_blocks_launch_even_when_metric_gates_pass(self) -> None:
        rows = []
        for cell in PILOT_CELLS:
            for index in range(10):
                rows.append(
                    {
                        "Scoring Profile ID": cell,
                        "Scoring V3 Disposition": "auto-eligible",
                        "Human Disposition": "auto-eligible" if index < 9 else "manual-review",
                    }
                )
            rows.append(
                {
                    "Scoring Profile ID": cell,
                    "Scoring V3 Disposition": "reject",
                    "Human Disposition": "reject",
                }
            )

        report = evaluate_calibration(
            rows,
            audit_pairs=[("auto-eligible", "reject"), ("reject", "auto-eligible")],
        )

        self.assertTrue(report["metric_gates_passed"])
        self.assertFalse(report["audit_kappa_passed"])
        self.assertFalse(report["launch_passed"])

    def test_kappa_target_can_be_measured(self) -> None:
        pairs = [("auto-eligible", "auto-eligible")] * 8 + [("reject", "reject")] * 2
        self.assertEqual(cohens_kappa(pairs), 1.0)


if __name__ == "__main__":
    unittest.main()
