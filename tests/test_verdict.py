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
    contact_readiness_failures,
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
    # The MVP is phone-only, so a lead with no dialable number is now refused
    # outright. Every fixture that is meant to reach `send` has to carry one.
    "Phone": "(440) 555-0142",
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
    # The tiers no longer split on grade, confidence or rating — those are rank
    # inputs. They split on how a lead fails: a claim a human can fix is
    # `review`, a good business with no leak found yet is `watch`, and only a
    # hard eligibility failure is `reject`.
    return [
        {},
        dict(SENDABLE_ROW),  # send
        # Claim integrity fails, business is fine — a human can fix the copy.
        {**SENDABLE_ROW, "Lead Priority": "B", "Safe Outreach Claims": ""},  # review
        # No concrete leak found, but a business worth keeping an eye on.
        {
            **SENDABLE_ROW,
            "Current Website Quality": "Good",
            "Site Status": "loaded",
            "best_angle": "Website Rebuild",
            "visible_pain_score": "0",
            "Visible Pain Reasons": "",
            "send_trigger": "",
        },  # watch
        # No dialable number: nothing a human can do with it.
        {**SENDABLE_ROW, "Phone": ""},  # reject
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
        # The load-bearing test for the base cutover: the Call Queue filter is
        # `Verdict='send'` alone, so anything the filter cannot express has to be
        # implied by `send` or the queue and the gate disagree.
        #
        # The commercially-sendable / contact-now assertions are gone with the
        # commercial tribunal. `is_contact_ready` is the one that was always
        # doing the work — `query_ready_to_send` applies it as a defensive drop
        # over every row the filter returns — and it is kept, and now unguarded
        # by the two weaker assertions that used to fail first.
        for index, row in enumerate(rows_across_the_tier_space()):
            with self.subTest(row=index):
                if not evaluate_verdict(row).is_send:
                    continue
                self.assertTrue(is_contact_ready(apply_sendability(row)))

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

    def test_the_vocabulary_has_exactly_one_declaration_site(self) -> None:
        # The four strings live in `sendability`, beside the tiers they rename,
        # so there is one declaration and `readiness_flags` can read them without
        # importing the module that imports it. A second module assigning them to
        # its own constants would be the drift this whole collapse removes.
        import lead_machine.sendability as sendability_module

        declaring = Path(sendability_module.__file__).resolve()
        offenders: list[str] = []
        for path in self.production_sources():
            if path.resolve() == declaring:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not (
                    isinstance(node.value, ast.Constant)
                    and node.value.value in VERDICT_CHOICES
                ):
                    continue
                for target in node.targets:
                    # Module constants only. `scoring_v3` has its own unrelated
                    # disposition vocabulary that happens to include the word
                    # "reject"; a local in a different vocabulary is a collision,
                    # not a second declaration of this one.
                    if isinstance(target, ast.Name) and target.id.isupper():
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT).as_posix()}:{target.id}"
                        )

        self.assertEqual(offenders, [])

    def test_that_scan_catches_a_second_declaration(self) -> None:
        smuggled = "VERDICT_SEND = 'send'\nVERDICT_REJECT = 'reject'\n"
        tree = ast.parse(smuggled)
        found = [
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and node.value.value in VERDICT_CHOICES
            for target in node.targets
            if isinstance(target, ast.Name) and target.id.isupper()
        ]

        self.assertEqual(found, ["VERDICT_SEND", "VERDICT_REJECT"])

    def test_the_verdict_module_re_exports_the_vocabulary(self) -> None:
        import lead_machine.sendability as sendability_module

        self.assertIs(verdict_module.VERDICT_CHOICES, sendability_module.VERDICT_CHOICES)
        self.assertIs(verdict_module.TIER_VERDICTS, sendability_module.TIER_VERDICTS)
        for name in ("VERDICT_SEND", "VERDICT_REVIEW", "VERDICT_WATCH", "VERDICT_REJECT"):
            self.assertEqual(
                getattr(verdict_module, name), getattr(sendability_module, name)
            )

    def test_the_tier_mapping_and_its_inverse_stay_consistent(self) -> None:
        import lead_machine.sendability as sendability_module

        self.assertEqual(
            sendability_module.VERDICT_PACKET_TIERS,
            {verdict: tier for tier, verdict in TIER_VERDICTS.items()},
        )
        for tier, verdict_value in TIER_VERDICTS.items():
            self.assertEqual(
                sendability_module.VERDICT_PACKET_TIERS[verdict_value], tier
            )


class AStoredVerdictIsNotAClearanceCertificate(unittest.TestCase):
    """`Verdict=send` in the base is a snapshot, and snapshots go stale.

    `readiness_flags` re-runs the admission over a stored value, but `admit`
    only re-reads the ICP skip and the out-of-country check. The gates this
    branch newly made blocking — chiefly a number the rep can actually dial —
    live in `evaluate_sendability` and are not re-read from there. So a row that
    was legitimately `send` when it was written, and has since lost its phone,
    reconstructs to `Sendable=Yes` and reaches the dial queue.

    These rows are shaped like what the base actually returns: the projection
    onto `query_ready_to_send.DISPLAY_FIELDS`, which is the only shape a
    readback can ever produce. A row carrying `Raw Evidence` would not prove
    anything, because the queue never sees one.
    """

    BASE_ROW = {
        "Verdict": "send",
        "Status": "New",
        "Business Name": "Stale Co",
        "Phone": "(216) 555-0123",
        "Reviews": "42",
        "Rating": "4.6",
    }

    def test_the_healthy_readback_row_is_still_contact_ready(self) -> None:
        # Guards every test below: if the projection failed the contract on its
        # own, the withdrawals asserted below would prove nothing.
        self.assertTrue(is_contact_ready(dict(self.BASE_ROW)))

    def test_a_stored_send_that_lost_its_phone_is_withdrawn(self) -> None:
        row = dict(self.BASE_ROW, Phone="")
        self.assertFalse(is_contact_ready(row))
        self.assertTrue(
            any("phone" in failure.casefold() for failure in contact_readiness_failures(row)),
            contact_readiness_failures(row),
        )

    def test_a_stored_send_with_an_undialable_number_is_withdrawn(self) -> None:
        # The shape that is already sitting in the corpus: a non-US listing
        # number that normalizes to fewer than ten digits. Nothing downstream
        # can call it, and the opt-out ledger cannot match on it either.
        self.assertFalse(is_contact_ready(dict(self.BASE_ROW, Phone="091230 69950")))

    def test_a_stored_send_with_no_review_count_is_withdrawn(self) -> None:
        # An absent count is an unanswered question, not a business nobody has
        # reviewed — the same direction `has_review_data` fixed at score time.
        self.assertFalse(is_contact_ready(dict(self.BASE_ROW, Reviews="")))

    def test_a_hand_edited_no_is_still_honoured_over_a_stored_send(self) -> None:
        # The tightening direction never inverted: a human withdrawing a lead
        # does not need the gate's permission.
        self.assertFalse(is_contact_ready(dict(self.BASE_ROW, Sendable="No")))

    def test_every_send_row_survives_its_own_projection(self) -> None:
        """The property the Airtable formula cannot express.

        The queue filter selects on `Verdict` and `Status`; the local gate now
        additionally demands the projection carry a phone and a review count.
        Correspondence between them is therefore data-dependent, and the failure
        mode is silent and total: a lead the gate passed at score time gets
        dropped at the queue behind one `[WARN]` per record.

        The tests above prove the gate *can* refuse a projection. This proves it
        does not refuse the ones it just admitted, which is the direction that
        empties the morning queue.
        """
        from query_ready_to_send import DISPLAY_FIELDS

        checked = 0
        for index, row in enumerate(rows_across_the_tier_space()):
            scored = apply_sendability(dict(row))
            if not evaluate_verdict(scored).is_send:
                continue
            checked += 1
            projection = {field: scored.get(field, "") for field in DISPLAY_FIELDS}
            with self.subTest(row=index):
                self.assertTrue(
                    is_contact_ready(projection),
                    f"a send row failed its own readback projection: "
                    f"{contact_readiness_failures(projection)}",
                )
        self.assertGreater(checked, 0, "no send rows reached the projection check")


class TheQueueProjectionCannotSilentlyLoseAGateInput(unittest.TestCase):
    """A column the gate reads must be one the query refuses to run without.

    `REQUIRED_READINESS_FIELDS` exists because the schema probe drops any
    display field Airtable 422s on, and then rebuilds every row from
    `DISPLAY_FIELDS` — so a dropped column comes back as `""` for every record
    rather than being absent. While the dropped column only fed the rank that
    degraded the sort. Now it can decide the gate, and `""` fails it for every
    record at once: an empty queue behind a warning the code itself describes as
    reading "as cosmetic".

    Derived rather than listed, so a future gate input cannot be added without
    either protecting it or failing here.
    """

    def test_any_field_that_can_flip_the_gate_is_required(self) -> None:
        from query_ready_to_send import DISPLAY_FIELDS, REQUIRED_READINESS_FIELDS

        healthy = {
            "Verdict": "send",
            "Status": "New",
            "Business Name": "Projection Co",
            "Phone": "(216) 555-0123",
            "Reviews": "42",
            "Rating": "4.6",
        }
        self.assertTrue(is_contact_ready(healthy), "control row must pass")

        decisive = {
            field
            for field in DISPLAY_FIELDS
            if field in healthy and not is_contact_ready({**healthy, field: ""})
        }
        self.assertTrue(decisive, "no field was decisive; the control row proves nothing")
        self.assertLessEqual(
            decisive,
            set(REQUIRED_READINESS_FIELDS),
            "these columns decide the gate but the schema probe may drop them, "
            "which blanks them for every record and empties the queue",
        )


if __name__ == "__main__":
    unittest.main()
