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


class TheRankComparisonIsWithheldWhenItWouldMislead(unittest.TestCase):
    """The two sides are written by different commands, so coverage can diverge.

    `hermes_queue` invokes mark_sent without `--rank` while declines are logged
    by hand, so the dials can be almost entirely unranked while the declines are
    fully ranked. Averaging across that gap compares two populations and prints
    one number — the same defect as the accept-rate this eval replaces.
    """

    def test_a_thin_ranked_side_withholds_the_comparison(self) -> None:
        rows = decisions(dialed=15, declined=5, dial_rank=70.0, decline_rank=30.0)
        # Strip the rank from all but two dials, as the automated path does.
        for row in [r for r in rows if r["decision"] == "dial"][2:]:
            row["rank"] = None
        result = evaluate_pre_dial_approval(rows)
        self.assertIsNone(result["rank_separation"])
        self.assertIn("withheld", result["rank_summary"])
        self.assertFalse(result["rank_coverage"]["comparable"])

    def test_lopsided_coverage_withholds_even_with_enough_ranked_rows(self) -> None:
        # 8 of 40 dials ranked (20%) against 8 of 8 declines (100%): both sides
        # clear the minimum, but they are not the same population.
        rows = decisions(dialed=40, declined=8, dial_rank=70.0, decline_rank=30.0)
        for row in [r for r in rows if r["decision"] == "dial"][8:]:
            row["rank"] = None
        result = evaluate_pre_dial_approval(rows)
        self.assertIsNone(result["rank_separation"])
        self.assertIn("coverage", result["rank_summary"])

    def test_even_coverage_reports_the_comparison(self) -> None:
        result = evaluate_pre_dial_approval(
            decisions(dialed=15, declined=8, dial_rank=70.0, decline_rank=30.0)
        )
        self.assertTrue(result["rank_coverage"]["comparable"])
        self.assertAlmostEqual(result["rank_separation"], 40.0)

    def test_equally_sparse_coverage_is_still_withheld(self) -> None:
        # 5 of 100 ranked on each side. The skew check sees 0% difference and
        # the per-side minimum is met, so the old rule called this comparable —
        # but 5% of a population is a sample somebody chose, and the two 5%
        # slices were chosen by different commands on different days. Matching
        # sparsity is not representativeness.
        rows = decisions(dialed=100, declined=100, dial_rank=90.0, decline_rank=10.0)
        for side in ("dial", "decline"):
            for row in [r for r in rows if r["decision"] == side][5:]:
                row["rank"] = None
        result = evaluate_pre_dial_approval(rows)
        self.assertIsNone(result["rank_separation"])
        self.assertFalse(result["rank_coverage"]["comparable"])
        self.assertIn("withheld", result["rank_summary"])


class AnUnreadableLedgerCannotPass(unittest.TestCase):
    """Rows the loader could not parse are potentially the adverse ones.

    A go/no-go number computed over the survivors of a silent filter is the
    self-grading defect this eval exists to replace, one layer down.
    """

    def test_unreadable_lines_block_the_pass(self) -> None:
        result = evaluate_pre_dial_approval(decisions(dialed=25, declined=5), unreadable=20)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unreadable-ledger")
        self.assertIn("20", result["summary"])

    def test_unreadable_lines_block_a_failing_verdict_too(self) -> None:
        # Not just the pass. A rate read off a partial file is not evidence in
        # either direction, and reporting FAIL would credit the number with an
        # authority it does not have.
        result = evaluate_pre_dial_approval(decisions(dialed=2, declined=28), unreadable=20)
        self.assertEqual(result["status"], "unreadable-ledger")

    def test_a_fully_readable_ledger_is_unaffected(self) -> None:
        result = evaluate_pre_dial_approval(decisions(dialed=25, declined=5), unreadable=0)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
