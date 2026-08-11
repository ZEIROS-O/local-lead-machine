"""Parity tests for the Airtable Queue State formula generator."""
from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lead_machine import queue_state
from lead_machine.sendability import contact_readiness_failures


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestQueueStateFormulaCli(unittest.TestCase):
    def test_module_cli_prints_an_airtable_formula(self):
        result = subprocess.run(
            [sys.executable, "-m", "lead_machine.queue_state", "--formula"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().startswith("IF("))
        self.assertIn('"Ready to Dial"', result.stdout)


class TestQueueStateFormula(unittest.TestCase):
    def test_filter_formula_is_the_canonical_static_queue_expression(self):
        self.assertEqual(
            getattr(queue_state, "QUEUE_FILTER_FORMULA", None),
            "AND({Verdict}='send',{Status}='New')",
        )

    def test_generated_formula_reports_the_first_failed_clause(self):
        self.assertEqual(
            queue_state.queue_state_formula(),
            "IF({Verdict}='send',"
            "IF({Status}='New',\"Ready to Dial\","
            "\"Blocked: Status must be New\"),"
            "\"Blocked: Verdict must be send\")",
        )

    def test_clause_list_is_the_single_source_for_base_readiness(self):
        clauses = getattr(queue_state, "READINESS_CLAUSES", ())
        self.assertEqual(
            tuple((clause.field, clause.expected, clause.failure) for clause in clauses),
            (("Verdict", "send", "Verdict must be send"),),
        )

    def test_the_readiness_clause_is_pinned_to_the_verdict_vocabulary(self):
        # The clause spells "send" as a literal because importing `verdict` here
        # would cycle (verdict -> sendability -> queue_state). This is the pin
        # that keeps the literal honest, the same way the queue status clause is
        # pinned against PROSPECT_STATUS_CHOICES rather than derived from it.
        from lead_machine import verdict

        clause = queue_state.READINESS_CLAUSES[0]

        self.assertEqual(clause.field, verdict.VERDICT_FIELD)
        self.assertEqual(clause.expected, verdict.VERDICT_SEND)
        self.assertIn(clause.expected, verdict.VERDICT_CHOICES)

    def test_a_send_row_is_never_dropped_by_the_queues_defensive_check(self):
        # This used to assert that `send` implied commercially-sendable and
        # contact-now. It no longer does — the commercial tribunal was removed
        # from the gate — but the property those assertions were really
        # protecting still matters, and matters more now: `query_ready_to_send`
        # runs `is_contact_ready` over every row the filter returns and silently
        # discards the failures behind a warning. If the row-level check can
        # reject what the verdict admitted, the queue empties without the filter
        # ever looking wrong. Stripping the tribunal broke exactly this, via the
        # commercial-fit cross-checks, before they were removed too.
        from lead_machine.sendability import apply_sendability, is_contact_ready
        from lead_machine.verdict import evaluate_verdict
        from tests.test_verdict import rows_across_the_tier_space

        sends = 0
        for row in rows_across_the_tier_space():
            if not evaluate_verdict(row).is_send:
                continue
            sends += 1
            self.assertTrue(is_contact_ready(apply_sendability(row)))
        self.assertGreater(sends, 0)

    def test_prospect_status_vocabulary_includes_mvp_terminal_states(self):
        # Exact tuple, not a subset: without typecast every one of these has to
        # exist as a choice in the base, so adding or renaming one is a schema
        # change that needs the Airtable runbook, not an edit.
        self.assertEqual(
            getattr(queue_state, "PROSPECT_STATUS_CHOICES", None),
            (
                "New",
                "Manual Review",
                "Not Fit",
                "Follow-Up",
                "Won",
                "Lost",
                "Booked",
            ),
        )
        # The split is the point: `Closed` could not say which of the two
        # happened, so re-introducing it silently destroys conversion data again.
        self.assertNotIn("Closed", queue_state.PROSPECT_STATUS_CHOICES)

    def test_queue_gate_is_a_live_subset_of_the_status_vocabulary(self):
        # The hazard the Won/Lost split exposed: the gate names Status values as
        # bare strings, so a vocabulary edit that renames or drops one leaves the
        # gate referencing a choice no record can ever hold — an empty morning
        # queue that reports no error, because "nobody is ready to dial" is a
        # legitimate answer. Fail here instead.
        orphans = [
            value
            for value in queue_state.QUEUE_STATUS_CLAUSE.allowed
            if value not in queue_state.PROSPECT_STATUS_CHOICES
        ]
        self.assertEqual(orphans, [])

    def test_accept_reject_undecided_partition_covers_every_status(self):
        # The accept-rate KPI divides by (accepted + rejected). A Status that
        # belongs to no bucket does not raise — it silently leaves the
        # denominator, and the rate drifts up as the unclassified value spreads.
        # Force the classification at declaration time instead.
        buckets = {
            "accepted": queue_state.ACCEPTED_STATUSES,
            "rejected": queue_state.REJECTED_STATUSES,
            "undecided": queue_state.UNDECIDED_STATUSES,
        }
        self.assertEqual(
            set().union(*buckets.values()),
            queue_state.PROSPECT_STATUS_CHOICE_SET,
        )
        names = sorted(buckets)
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                with self.subTest(pair=(first, second)):
                    self.assertEqual(buckets[first] & buckets[second], frozenset())

    def test_booked_is_accepted_and_not_fit_is_rejected(self):
        self.assertIn("Booked", queue_state.ACCEPTED_STATUSES)
        self.assertIn("Not Fit", queue_state.REJECTED_STATUSES)
        self.assertNotIn("Not Fit", queue_state.ACCEPTED_STATUSES)

    def test_undecided_statuses_contain_the_whole_dial_queue(self):
        # A queued lead has not been judged yet. If one ever counted as accepted,
        # the KPI would report the machine's own proposals back as approvals.
        for status in queue_state.QUEUE_STATUS_CLAUSE.allowed:
            with self.subTest(status=status):
                self.assertIn(status, queue_state.UNDECIDED_STATUSES)

    def test_queue_gate_only_accepts_new(self):
        self.assertEqual(queue_state.QUEUE_STATUS_CLAUSE.allowed, ("New",))
        for closed in ("Won", "Lost"):
            with self.subTest(status=closed):
                self.assertNotIn(closed, queue_state.QUEUE_STATUS_CLAUSE.allowed)
                self.assertEqual(
                    queue_state.queue_state_for_row(
                        {
                            "Verdict": "send",
                            "Status": closed,
                        }
                    ),
                    "Blocked: Status must be New",
                )

    def test_queue_status_clause_is_canonical(self):
        clause = getattr(queue_state, "QUEUE_STATUS_CLAUSE", None)
        if clause is None:
            self.fail("QUEUE_STATUS_CLAUSE is required for static Airtable queue eligibility")
        self.assertEqual(
            (clause.field, clause.allowed, clause.failure, clause.airtable_condition),
            (
                "Status",
                ("New",),
                "Status must be New",
                "{Status}='New'",
            ),
        )

    def test_literal_queue_state_and_readiness_failure_matrix(self):
        evaluator = getattr(queue_state, "queue_state_for_row", None)
        if not callable(evaluator):
            self.fail("queue_state_for_row is required to evaluate formula logic in Python")

        # A base row after the collapse: one verdict column, plus the
        # human-owned Status. The commercial-fit and contact-eligibility columns
        # are gone, and the verdict already carries what they asserted.
        # `Phone` and `Reviews` are part of the base row, not decoration:
        # `contact_readiness_failures` re-derives reachability from them because
        # the stored verdict is a snapshot that can go stale. Without them every
        # case below would carry two extra failures that have nothing to do with
        # the clause logic under test.
        ready = {
            "Verdict": "send",
            "Commercial Fit State": "commercially-sendable",
            "Contact Eligibility": "contact-now",
            "Status": "New",
            "Phone": "(216) 555-0123",
            "Reviews": "42",
        }
        cases = (
            (
                # `review` still carries a packet, so packet eligibility holds
                # and only the send clause fails. `reject` fails both.
                "verdict review",
                {"Verdict": "review"},
                ["Verdict must be send"],
                "Blocked: Verdict must be send",
            ),
            (
                "verdict reject",
                {"Verdict": "reject"},
                ["Verdict must be send", "Packet Eligible must be Yes"],
                "Blocked: Verdict must be send",
            ),
            (
                "status",
                {"Status": "Follow-Up"},
                [],
                "Blocked: Status must be New",
            ),
            ("all pass", {}, [], "Ready to Dial"),
        )
        for name, overrides, readiness_failures, queue_state_value in cases:
            with self.subTest(name=name):
                row = {**ready, **overrides}
                self.assertEqual(contact_readiness_failures(row), readiness_failures)
                self.assertEqual(evaluator(row), queue_state_value)

    def test_sendability_reads_the_clause_reason_rather_than_its_own_copy(self):
        # `contact_readiness_failures` must report the clause's own failure
        # string, not a duplicate literal that could drift from the formula the
        # base evaluates.
        clauses = (replace(queue_state.READINESS_CLAUSES[0], failure="Shared readiness failure"),)
        row = {
            "Verdict": "reject",
            "Packet Eligible": "Yes",
            "Phone": "(216) 555-0123",
            "Reviews": "42",
        }

        with patch.object(queue_state, "READINESS_CLAUSES", clauses):
            self.assertEqual(contact_readiness_failures(row), ["Shared readiness failure"])
