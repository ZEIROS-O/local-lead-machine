"""The collapse must change no decision, and must stay a projection.

`Verdict` replaces nine surfaces that each encoded a slice of "should we contact
this lead". The only thing that makes that safe is that it re-decides nothing:
it renames the packet tier the existing authority already produced. These tests
pin the rename as a bijection, pin the implication the Call Queue filter is
about to start relying on, and refuse a second module the right to spell one.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from lead_machine import verdict as verdict_module
from lead_machine.sendability import (
    PACKET_TIER_CHOICES,
    PACKET_TIER_MANUAL,
    PACKET_TIER_NONE,
    PACKET_TIER_SENDABLE,
    PACKET_TIER_WATCHLIST,
    apply_sendability,
    evaluate_packet_tier,
    evaluate_sendability,
    is_contact_ready,
)
from lead_machine.verdict import (
    TIER_VERDICTS,
    VERDICT_CHOICES,
    VERDICT_REJECT,
    VERDICT_REVIEW,
    VERDICT_SEND,
    VERDICT_WATCH,
    apply_verdict,
    evaluate_verdict,
    verdict_for_tier,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

# Reduced from a real `Sendable Packet` row in the scored corpus down to the
# fields the gate actually reads. Hand-built rows kept looking sendable while
# tripping a blocker, which is the vacuity this file's non-vacuity test exists
# to catch.
SENDABLE_ROW = {
    "Business Name": "Lorain Garage Door Repair Co",
    "Place Key": "place-send-1",
    # `Lead Priority`, not `Grade`: `manual_review_reason` reads only
    # ("Lead Priority", "grade"), so a row carrying the grade under `Grade`
    # alone can never reach the manual-review tier.
    "Lead Priority": "A",
    "Review Demand Band": "ideal_growth",
    "total_score": "92",
    "confidence": "high",
    "Reviews": "51",
    "Rating": "5.0",
    "Current Website Quality": "N/A",
    "best_angle": "No-Site Launch",
    "visible_pain_score": "62",
    "send_trigger": "No owned website found",
    "Opener": (
        "I found the business on Google with 51 reviews and a 5-star rating, "
        "but no owned website where customers can request an estimate."
    ),
    "Evidence Refs": (
        '["ev-google_rating-1", "ev-site_status-2", "ev-site_issue_flags-3"]'
    ),
    "Safe Outreach Claims": (
        "The business has a 5-star Google rating across 51 reviews. "
        "[ev-google_rating-1]; No owned website was found for the business in "
        "this review. [ev-site_status-2,ev-site_issue_flags-3]"
    ),
    "commercial_fit_state": "commercially-sendable",
    "commercial_materiality": (
        "A missing owned site is a durable acquisition and conversion gap."
    ),
    "recurring_consequence": (
        "Prospects repeatedly lack an owned path from discovery to inquiry."
    ),
    "root_cause_confidence": "High confidence that no owned site was found.",
    "offer_component": "Get the Lead",
    "packageable_solution": (
        "Launch an owned, mobile-first site with an estimate-request workflow."
    ),
    "value_mechanism": (
        "Convert demonstrated Google reputation into a controlled inquiry path."
    ),
    "beyond_free_fix": (
        "Requires an owned domain, service content and lead routing, not a "
        "listing fix."
    ),
    "self_fix_risk": (
        "Moderate; a credible service structure is a broader implementation."
    ),
    "intentionality": (
        "May be intentional, but the packet shows enough demand proof to raise it."
    ),
    "contact_eligibility": "contact-now",
    "asset_state": "none",
    "promise_state": "asset-free",
}


def rows_across_the_tier_space() -> list[dict[str, str]]:
    """A spread of rows, not a curated pass — the projection must hold on all."""
    return [
        {},
        dict(SENDABLE_ROW),  # send
        {**SENDABLE_ROW, "Lead Priority": "C", "Site Status": "missing"},  # review
        {**SENDABLE_ROW, "confidence": "low"},  # watch
        {**SENDABLE_ROW, "Safe Outreach Claims": ""},  # reject
        {**SENDABLE_ROW, "commercial_fit_state": "unverified"},
        {**SENDABLE_ROW, "contact_eligibility": "hold-commercial-case"},
        {**SENDABLE_ROW, "Rating": "3.1", "Reviews": "2"},
        {**SENDABLE_ROW, "send_trigger": "", "visible_pain_score": "0"},
        {"Business Name": "Bare Row"},
    ]


class VerdictIsABijectionOnTheTiers(unittest.TestCase):
    def test_every_packet_tier_maps_to_a_verdict(self) -> None:
        # A fifth tier must be classified here rather than KeyError in
        # production or, worse, defaulting quietly into `reject`.
        self.assertEqual(set(TIER_VERDICTS), set(PACKET_TIER_CHOICES))

    def test_the_mapping_is_one_to_one(self) -> None:
        # Two tiers sharing a verdict would make the collapse lossy, and the
        # reproducibility diff could no longer prove equivalence.
        self.assertEqual(len(set(TIER_VERDICTS.values())), len(TIER_VERDICTS))
        self.assertEqual(VERDICT_CHOICES, set(TIER_VERDICTS.values()))

    def test_the_four_verdicts_are_the_documented_ones(self) -> None:
        self.assertEqual(
            VERDICT_CHOICES,
            {VERDICT_SEND, VERDICT_REVIEW, VERDICT_WATCH, VERDICT_REJECT},
        )
        self.assertEqual(verdict_for_tier(PACKET_TIER_SENDABLE), VERDICT_SEND)
        self.assertEqual(verdict_for_tier(PACKET_TIER_MANUAL), VERDICT_REVIEW)
        self.assertEqual(verdict_for_tier(PACKET_TIER_WATCHLIST), VERDICT_WATCH)
        self.assertEqual(verdict_for_tier(PACKET_TIER_NONE), VERDICT_REJECT)

    def test_an_undeclared_tier_raises_rather_than_defaulting(self) -> None:
        with self.assertRaises(ValueError):
            verdict_for_tier("Some New Tier")


class VerdictReDecidesNothing(unittest.TestCase):
    def test_the_verdict_is_the_tier_under_another_name(self) -> None:
        for index, row in enumerate(rows_across_the_tier_space()):
            with self.subTest(row=index):
                result = evaluate_sendability(row)
                tier, _reason = evaluate_packet_tier(row, result)
                self.assertEqual(
                    evaluate_verdict(row).verdict, TIER_VERDICTS[tier]
                )

    def test_reasons_are_the_existing_blocker_list(self) -> None:
        # `Send Blockers` and `Sendability Blockers` were already byte-identical
        # columns; this is where the two become one.
        for index, row in enumerate(rows_across_the_tier_space()):
            with self.subTest(row=index):
                self.assertEqual(
                    list(evaluate_verdict(row).reasons),
                    evaluate_sendability(row)["send_blockers"],
                )

    def test_send_agrees_with_the_existing_sendable_decision(self) -> None:
        for index, row in enumerate(rows_across_the_tier_space()):
            with self.subTest(row=index):
                result = evaluate_verdict(row)
                self.assertEqual(
                    result.is_send, evaluate_sendability(row)["sendable"]
                )

    def test_send_implies_the_readiness_clauses_the_queue_filter_drops(self) -> None:
        # The load-bearing test for the base cutover. The Call Queue filter is
        # about to become `Verdict='send'` alone, dropping the compound
        # commercially-sendable / contact-now clauses. That is only safe because
        # `send` already implies both — they are blockers inside the same
        # admission — so the shorter filter selects the same leads.
        for index, row in enumerate(rows_across_the_tier_space()):
            with self.subTest(row=index):
                if not evaluate_verdict(row).is_send:
                    continue
                scored = apply_sendability(row)
                self.assertEqual(
                    scored["Commercial Fit State"], "commercially-sendable"
                )
                self.assertEqual(scored["Contact Eligibility"], "contact-now")
                self.assertTrue(is_contact_ready(scored))

    def test_the_row_set_reaches_all_four_verdicts(self) -> None:
        # Guards every loop above: a row set that only ever reached `reject`
        # would certify nothing while passing, and the send-implies-readiness
        # test would be vacuously true.
        verdicts = {
            evaluate_verdict(row).verdict for row in rows_across_the_tier_space()
        }

        self.assertEqual(verdicts, VERDICT_CHOICES)

    def test_apply_verdict_writes_both_fields_and_mutates_nothing(self) -> None:
        row = dict(SENDABLE_ROW)
        updated = apply_verdict(row)

        self.assertEqual(updated["Verdict"], VERDICT_SEND)
        self.assertEqual(updated["Verdict Reasons"], "")
        self.assertNotIn("Verdict", row)

    def test_reasons_reach_the_row_as_one_joined_string(self) -> None:
        updated = apply_verdict({"Business Name": "Bare Row"})

        self.assertEqual(updated["Verdict"], VERDICT_REJECT)
        self.assertIn("; ", updated["Verdict Reasons"])


class OnlyVerdictModuleMintsAVerdict(unittest.TestCase):
    """The same invariant that protects `Sendable`, extended to `Verdict`.

    `sendable_flag` survived the cutover because exactly one function was
    allowed to spell it. A second module writing `Verdict = "send"` would
    reintroduce, on the new field, the disagreement the collapse removes.
    """

    VERDICT_KEYS = frozenset({"Verdict", "verdict"})

    def production_sources(self) -> list[Path]:
        paths = sorted(REPO_ROOT.glob("*.py"))
        paths.extend(sorted((REPO_ROOT / "lead_machine").rglob("*.py")))
        return paths

    def verdict_writes(self, source: str, label: str) -> list[str]:
        tree = ast.parse(source)
        offenders: list[str] = []

        def mints(value: ast.AST) -> bool:
            if isinstance(value, ast.Constant):
                return value.value in VERDICT_CHOICES
            if isinstance(value, ast.IfExp):
                return mints(value.body) or mints(value.orelse)
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value in self.VERDICT_KEYS
                        and mints(node.value)
                    ):
                        offenders.append(f"{label} line {node.lineno}")
            elif isinstance(node, ast.Dict):
                for key_node, value_node in zip(node.keys, node.values):
                    if (
                        isinstance(key_node, ast.Constant)
                        and key_node.value in self.VERDICT_KEYS
                        and mints(value_node)
                    ):
                        offenders.append(f"{label} line {node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in self.VERDICT_KEYS
                and len(node.args) > 1
                and mints(node.args[1])
            ):
                offenders.append(f"{label} line {node.lineno}")
        return offenders

    def test_no_production_module_mints_a_verdict(self) -> None:
        offenders = sorted(
            offender
            for path in self.production_sources()
            if path.resolve() != Path(verdict_module.__file__).resolve()
            for offender in self.verdict_writes(
                path.read_text(encoding="utf-8"),
                path.relative_to(REPO_ROOT).as_posix(),
            )
        )

        self.assertEqual(offenders, [])

    def test_the_scan_actually_catches_a_new_verdict_path(self) -> None:
        # Guards the guard: a scanner matching nothing would pass forever.
        smuggled = (
            "def ship(row):\n"
            "    row['Verdict'] = 'send'\n"
            "    row.update({'Verdict': 'send' if ok else 'reject'})\n"
            "    row.setdefault('Verdict', 'send')\n"
            "    return row\n"
        )

        self.assertEqual(len(self.verdict_writes(smuggled, "smuggled.py")), 3)

    def test_the_verdict_module_is_the_one_that_spells_them(self) -> None:
        source = Path(verdict_module.__file__).read_text(encoding="utf-8")

        for choice in sorted(VERDICT_CHOICES):
            self.assertIn(f'"{choice}"', source)


if __name__ == "__main__":
    unittest.main()
