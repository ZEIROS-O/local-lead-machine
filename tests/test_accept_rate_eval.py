from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def base_record(
    record_id: str,
    status: str,
    *,
    verdict: str = "Sendable Packet",
    name: str = "Acme Services",
) -> dict[str, object]:
    return {
        "id": record_id,
        "fields": {
            "Place Key": record_id,
            "Business Name": name,
            "Packet Tier": verdict,
            "Status": status,
        },
    }


def decided(accepted: int, rejected: int) -> list[dict[str, object]]:
    records = [base_record(f"acc-{index}", "Sent") for index in range(accepted)]
    records += [base_record(f"rej-{index}", "Not Fit") for index in range(rejected)]
    return records


class AcceptRateEvalTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("evals.accept_rate")
        except ModuleNotFoundError:
            self.fail("evals.accept_rate must exist")

    def evaluate(self, records, **kwargs):
        return self.module().evaluate_accept_rate(records, **kwargs)

    # --- what the rate is measured over -------------------------------------

    def test_only_machine_proposed_leads_are_judged(self) -> None:
        """A lead the machine never proposed cannot accept or reject it."""
        report = self.evaluate(
            [
                base_record("sendable", "Sent"),
                base_record("watchlist", "Not Fit", verdict="Watchlist"),
                base_record("none", "Not Fit", verdict="No Packet"),
            ],
            minimum_decided=1,
        )

        self.assertEqual(report["counts"]["proposed"], 1)
        self.assertEqual(report["counts"]["accepted"], 1)
        self.assertEqual(report["counts"]["rejected"], 0)

    def test_queued_leads_are_undecided_not_accepted(self) -> None:
        report = self.evaluate(
            [
                base_record("new", "New"),
                base_record("mockup", "Mockup Needed"),
                base_record("manual", "Manual Review"),
                base_record("sent", "Sent"),
            ],
            minimum_decided=1,
        )

        self.assertEqual(report["counts"]["undecided"], 3)
        self.assertEqual(report["counts"]["decided"], 1)
        self.assertEqual(report["accept_rate"], 1.0)

    def test_a_sent_lead_that_lost_still_counts_as_accepted(self) -> None:
        """Accepted means taken into outreach; conversion is a separate metric."""
        report = self.evaluate(
            [base_record("lost", "Lost"), base_record("won", "Won")],
            minimum_decided=1,
        )

        self.assertEqual(report["counts"]["accepted"], 2)
        self.assertEqual(report["accept_rate"], 1.0)

    def test_the_partition_comes_from_queue_state_not_a_local_copy(self) -> None:
        from lead_machine import queue_state

        module = self.module()

        self.assertIs(module.ACCEPTED_STATUSES, queue_state.ACCEPTED_STATUSES)
        self.assertIs(module.REJECTED_STATUSES, queue_state.REJECTED_STATUSES)
        self.assertIs(module.UNDECIDED_STATUSES, queue_state.UNDECIDED_STATUSES)

    # --- the verdict on the rate --------------------------------------------

    def test_a_rate_above_the_floor_passes(self) -> None:
        report = self.evaluate(decided(15, 5), minimum_decided=20)

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["accept_rate"], 0.75)

    def test_a_rate_below_the_floor_fails(self) -> None:
        report = self.evaluate(decided(5, 15), minimum_decided=20)

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "below-floor")
        self.assertEqual(report["accept_rate"], 0.25)
        self.assertIn("50", report["summary"])

    def test_the_floor_is_inclusive(self) -> None:
        report = self.evaluate(decided(10, 10), minimum_decided=20)

        self.assertTrue(report["ok"])
        self.assertEqual(report["accept_rate"], 0.5)

    def test_too_few_decisions_reports_insufficient_never_pass(self) -> None:
        """A KPI with no data must not print PASS — that is the shape of a check
        that cannot fail."""
        report = self.evaluate(decided(0, 3), minimum_decided=20)

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "insufficient-sample")
        self.assertNotIn("PASS", report["summary"])
        self.assertIn("3", report["summary"])
        # The rate over three decisions is still real and worth reporting; it is
        # `status` that withholds the verdict, not a blanked-out number.
        self.assertEqual(report["accept_rate"], 0.0)

    def test_no_decisions_at_all_leaves_the_rate_undefined(self) -> None:
        report = self.evaluate([base_record("queued", "New")])

        self.assertEqual(report["status"], "insufficient-sample")
        self.assertIsNone(report["accept_rate"])

    def test_an_unknown_status_fails_rather_than_leaving_the_denominator(self) -> None:
        report = self.evaluate(
            decided(10, 10) + [base_record("odd", "Nurturing")],
            minimum_decided=20,
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "unknown-status")
        self.assertEqual(
            report["findings"]["unknown_statuses"],
            [
                {
                    "status": "Nurturing",
                    "business_name": "Acme Services",
                    "record_id": "odd",
                }
            ],
        )

    def test_a_blank_status_is_unknown_not_undecided(self) -> None:
        report = self.evaluate(decided(10, 10) + [base_record("blank", "")])

        self.assertFalse(report["ok"])
        self.assertEqual(report["findings"]["unknown_statuses"][0]["status"], "")

    def test_summary_is_one_markdown_bullet(self) -> None:
        report = self.evaluate(decided(15, 5), minimum_decided=20)

        self.assertTrue(report["summary"].startswith("- "))
        self.assertEqual(report["summary"].count("\n"), 0)

    # --- CLI ------------------------------------------------------------------

    def test_main_writes_the_summary_file_and_exits_zero(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "base.json"
            records.write_text(json.dumps(decided(15, 5)), encoding="utf-8")
            summary = Path(tmp) / "summary.md"

            code = module.main(
                [
                    "--base-records-json",
                    str(records),
                    "--summary-file",
                    str(summary),
                ]
            )

            self.assertEqual(code, 0)
            self.assertIn("75.0%", summary.read_text(encoding="utf-8"))

    def test_main_exits_non_zero_below_the_floor(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "base.json"
            records.write_text(json.dumps(decided(1, 25)), encoding="utf-8")

            code = module.main(["--base-records-json", str(records)])

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
