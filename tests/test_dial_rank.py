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

from lead_machine.dial_rank import RANK_MAX, WEIGHTS, dial_rank, dial_sort_key
from query_ready_to_send import DISPLAY_FIELDS


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


def queue_row(**overrides) -> dict:
    """A row shaped the way `query_ready_to_send` builds one.

    That query projects each Airtable record onto DISPLAY_FIELDS, so a column
    absent from that list does not reach the ranker no matter what the base
    holds. `row()` above hands dial_rank a full scored CSV row instead, which is
    how a dead component passed every test in this file: `Visible Pain Score`
    was missing from DISPLAY_FIELDS, so the largest weight contributed zero to
    every live lead while the unit tests exercised it happily.
    """
    return {field: str(overrides.get(field, "") or "") for field in DISPLAY_FIELDS}


# Each rank component and the DISPLAY_FIELDS columns it can be moved by. Money
# is read from the `Buyer Intent Signals` capsule because the base has no
# `Money Signal` column for it to read directly.
_COMPONENT_INPUTS = {
    "pain": ({"Visible Pain Score": "80"}, {"Visible Pain Score": "5"}),
    "demand": ({"Reviews": "200", "Rating": "5.0"}, {"Reviews": "3", "Rating": "4.0"}),
    "money": (
        {"Buyer Intent Signals": "money_signal=yes; demand_signal=yes"},
        {"Buyer Intent Signals": "money_signal=no; demand_signal=no"},
    ),
    "confidence": ({"AI Confidence": "high"}, {"AI Confidence": "low"}),
    "evidence": ({"Evidence Refs": "ev-1, ev-2, ev-3"}, {"Evidence Refs": ""}),
    "score": ({"Score Total": "95"}, {"Score Total": "10"}),
}


class EveryWeightIsLiveInTheQueue(unittest.TestCase):
    """The fetch and the ranker are one contract; this is what joins them.

    A weight that no DISPLAY_FIELDS column can move is not a ranking signal, it
    is a constant — it shifts every lead by the same amount and orders nothing.
    """

    def test_every_weighted_component_responds_to_a_queue_row(self) -> None:
        for name, (better, worse) in _COMPONENT_INPUTS.items():
            with self.subTest(component=name):
                self.assertGreater(
                    dial_rank(queue_row(**better)),
                    dial_rank(queue_row(**worse)),
                    f"the {name!r} weight is dead in the live queue: no column in "
                    "DISPLAY_FIELDS moves it",
                )

    def test_the_coverage_check_names_every_weight(self) -> None:
        # Without this, adding a seventh weight with no queue-visible input
        # would pass the loop above by simply not being in it.
        self.assertEqual(set(WEIGHTS), set(_COMPONENT_INPUTS))


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
