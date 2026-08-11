"""What the pre-dial approval eval must and must not claim.

The failure this KPI was built to avoid is a number that reads green without
evidence behind it, so most of these tests are about what it refuses to say.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from pre_dial_approval import evaluate_pre_dial_approval  # noqa: E402


def decisions(dialed=0, declined=0, *, dial_rank=None, decline_rank=None, reason="thin"):
    rows = []
    for i in range(dialed):
        rows.append({"decision": "dial", "record_id": f"d{i}", "rank": dial_rank})
    for i in range(declined):
        rows.append(
            {"decision": "decline", "record_id": f"x{i}", "rank": decline_rank, "reason": reason}
        )
    return rows


class WhatItRefusesToSay(unittest.TestCase):
    def test_an_empty_ledger_is_no_verdict_not_a_zero_rate(self) -> None:
        # An unconfigured ledger and a rep who has not started look identical
        # from here. Neither is "the loop is losing".
        result = evaluate_pre_dial_approval([])
        self.assertEqual(result["status"], "no-decisions")
        self.assertIsNone(result["approval_rate"])
        self.assertTrue(result["ok"])

    def test_a_thin_sample_never_passes(self) -> None:
        result = evaluate_pre_dial_approval(decisions(dialed=5))
        self.assertEqual(result["status"], "insufficient-sample")
        self.assertNotIn("PASS", result["summary"])

    def test_a_perfect_thin_sample_still_never_passes(self) -> None:
        # 100% of 5 is the shape a manufactured metric takes. It must not read
        # as evidence just because the number is flattering.
        result = evaluate_pre_dial_approval(decisions(dialed=5, declined=0))
        self.assertEqual(result["status"], "insufficient-sample")


class WhatItDoesSay(unittest.TestCase):
    def test_a_healthy_sample_passes(self) -> None:
        result = evaluate_pre_dial_approval(decisions(dialed=15, declined=5))
        self.assertEqual(result["status"], "pass")
        self.assertAlmostEqual(result["approval_rate"], 0.75)

    def test_below_the_floor_fails(self) -> None:
        result = evaluate_pre_dial_approval(decisions(dialed=8, declined=15))
        self.assertEqual(result["status"], "below-floor")
        self.assertFalse(result["ok"])

    def test_refusal_reasons_are_reported(self) -> None:
        result = evaluate_pre_dial_approval(
            decisions(dialed=15, declined=5, reason="no owner name")
        )
        self.assertEqual(result["decline_reasons"]["no owner name"], 5)


class TheRankFalsifier(unittest.TestCase):
    def test_a_rank_that_does_not_separate_warns_even_when_approval_passes(self) -> None:
        # The whole case for demoting nine gates is that ranking preserves what
        # they protected. If approved and refused leads rank the same, it does
        # not, and a passing approval rate must not hide that.
        result = evaluate_pre_dial_approval(
            decisions(dialed=15, declined=5, dial_rank=50.0, decline_rank=49.0)
        )
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["rank_ok"])
        self.assertIn("RANK WARNING", result["rank_summary"])

    def test_a_separating_rank_reports_clean(self) -> None:
        result = evaluate_pre_dial_approval(
            decisions(dialed=15, declined=5, dial_rank=62.0, decline_rank=40.0)
        )
        self.assertTrue(result["rank_ok"])
        self.assertAlmostEqual(result["rank_separation"], 22.0)

    def test_unranked_decisions_report_no_comparison_rather_than_zero(self) -> None:
        result = evaluate_pre_dial_approval(decisions(dialed=15, declined=5))
        self.assertIsNone(result["rank_ok"])
        self.assertIsNone(result["rank_separation"])


if __name__ == "__main__":
    unittest.main()
