"""The dial rank: what replaced the demoted gates.

Nine blockers came out of `sendability` because they answered "is this lead
worth dialling" rather than "is what we would say true". The claim made when
they were removed was that they become ranking signals instead — these tests are
what makes that claim checkable rather than a comment.

The properties pinned here are ordinal, not absolute. No test asserts that a
lead scores 62; they assert that a lead with a worse signal sorts below one with
a better signal, all else equal. Absolute thresholds are how a rank quietly
turns back into a gate.
"""

from __future__ import annotations

import unittest

from lead_machine.dial_rank import RANK_MAX, dial_rank, dial_sort_key


def row(**overrides) -> dict:
    base = {
        "Business Name": "Control Lead",
        "Lead Priority": "B",
        "Score Total": "70",
        "Reviews": "40",
        "Rating": "4.7",
        "AI Confidence": "medium",
        "Visible Pain Score": "35",
        "Visible Pain Reasons": "No estimate form detected",
        "Money Signal": "unknown",
        "Evidence Refs": "ev-1, ev-2",
    }
    base.update(overrides)
    return base


class TheRankIsBounded(unittest.TestCase):
    def test_an_empty_row_ranks_low_rather_than_raising(self) -> None:
        # The rank runs over base rows read back from Airtable, which may be
        # missing any column. A KeyError here would take down the morning queue.
        #
        # Not zero: an absent money signal is `unknown`, and the tri-state rule
        # forbids scoring that as `no`. So a row we know nothing about keeps the
        # half-credit every unanswered question gets, and sinks on everything
        # else — which is the property that actually matters.
        self.assertGreaterEqual(dial_rank({}), 0.0)
        self.assertLess(dial_rank({}), dial_rank(row()) / 2)

    def test_every_component_maxed_lands_on_the_ceiling(self) -> None:
        best = row(
            Reviews="250",
            Rating="5.0",
            **{"AI Confidence": "high"},
            **{"Visible Pain Score": "100"},
            **{"Money Signal": "yes"},
            **{"Score Total": "100"},
            **{"Evidence Refs": "ev-1, ev-2, ev-3"},
        )
        self.assertEqual(dial_rank(best), RANK_MAX)

    def test_the_rank_never_leaves_its_range_on_absurd_input(self) -> None:
        # Values arrive from a CSV and a model, so neither bound can be assumed.
        for absurd in ("-500", "99999", "not a number", ""):
            with self.subTest(value=absurd):
                score = dial_rank(row(**{"Visible Pain Score": absurd, "Score Total": absurd}))
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, RANK_MAX)


class EachDemotedGateStillMovesTheOrder(unittest.TestCase):
    """One test per signal removed from the gate.

    If a signal stops affecting the order, it has not been demoted — it has been
    discarded, and the rep loses the ordering the gate used to enforce by
    exclusion.
    """

    def assert_orders(self, better: dict, worse: dict, label: str) -> None:
        self.assertGreater(dial_rank(better), dial_rank(worse), label)

    def test_visible_pain_orders(self) -> None:
        self.assert_orders(
            row(**{"Visible Pain Score": "70"}),
            row(**{"Visible Pain Score": "10"}),
            "a worse leak should dial first",
        )

    def test_review_count_orders(self) -> None:
        self.assert_orders(row(Reviews="180"), row(Reviews="6"), "demand proof")

    def test_rating_orders(self) -> None:
        self.assert_orders(row(Rating="4.9"), row(Rating="4.0"), "reputation")

    def test_confidence_orders(self) -> None:
        self.assert_orders(
            row(**{"AI Confidence": "high"}),
            row(**{"AI Confidence": "low"}),
            "model confidence",
        )

    def test_money_signal_orders_and_unknown_sits_between(self) -> None:
        # The tri-state rule from AGENTS.md survives the move into the rank:
        # `unknown` is an unanswered question, so it must not score as `no`.
        yes = dial_rank(row(**{"Money Signal": "yes"}))
        unknown = dial_rank(row(**{"Money Signal": "unknown"}))
        no = dial_rank(row(**{"Money Signal": "no"}))
        self.assertGreater(yes, unknown)
        self.assertGreater(unknown, no)

    def test_score_total_orders(self) -> None:
        self.assert_orders(row(**{"Score Total": "95"}), row(**{"Score Total": "52"}), "score")

    def test_grade_orders_through_the_sort_key(self) -> None:
        # Grade is the model's holistic summary and is deliberately not a rank
        # component — it is built from the same subscores, so adding it would
        # double-count. It survives as the tiebreak in the sort key.
        a = dial_sort_key(row(**{"Lead Priority": "A"}))
        c = dial_sort_key(row(**{"Lead Priority": "C"}))
        self.assertLess(a, c)


class TheSortKeyPutsTheBestLeadFirst(unittest.TestCase):
    def test_sorting_is_descending_by_rank(self) -> None:
        # `sorted` is ascending, so the key negates. Getting this backwards
        # would hand the rep the worst leads first and look like nothing at all.
        weak = row(**{"Visible Pain Score": "5"}, Reviews="6", Rating="4.1")
        strong = row(**{"Visible Pain Score": "80"}, Reviews="200", Rating="5.0")

        self.assertEqual(sorted([weak, strong], key=dial_sort_key)[0], strong)

    def test_a_ranked_lead_outranks_an_unranked_one(self) -> None:
        # Base rows can be missing every signal. Those must sink, not float:
        # a row we know nothing about is not a row to dial first.
        self.assertLess(dial_sort_key(row()), dial_sort_key({}))

    def test_the_key_is_total_over_rows_that_share_a_rank(self) -> None:
        # Two leads scoring identically must still have a defined order, or the
        # queue reshuffles between runs and "call the top ten" means something
        # different each morning.
        first = dial_sort_key({**row(), "Business Name": "Alpha"})
        second = dial_sort_key({**row(), "Business Name": "Beta"})
        self.assertNotEqual(first, second)
        self.assertLess(first, second)


if __name__ == "__main__":
    unittest.main()
