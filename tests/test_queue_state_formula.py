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
            "AND({Packet Tier}='Sendable Packet',"
            "{Commercial Fit State}='commercially-sendable',"
            "{Contact Eligibility}='contact-now',"
            "OR({Status}='New',{Status}='Mockup Needed'))",
        )

    def test_generated_formula_reports_the_first_failed_clause(self):
        self.assertEqual(
            queue_state.queue_state_formula(),
            "IF({Packet Tier}='Sendable Packet',"
            "IF({Commercial Fit State}='commercially-sendable',"
            "IF({Contact Eligibility}='contact-now',"
            "IF(OR({Status}='New',{Status}='Mockup Needed'),\"Ready to Dial\","
            "\"Blocked: Status must be New or Mockup Needed\"),"
            "\"Blocked: Contact Eligibility must be contact-now\"),"
            "\"Blocked: Commercial Fit State must be commercially-sendable\"),"
            "\"Blocked: Sendable must be Yes\")",
        )

    def test_clause_list_is_the_single_source_for_base_readiness(self):
        clauses = getattr(queue_state, "READINESS_CLAUSES", ())
        self.assertEqual(
            tuple((clause.field, clause.expected, clause.failure) for clause in clauses),
            (
                ("Packet Tier", "Sendable Packet", "Sendable must be Yes"),
                (
                    "Commercial Fit State",
                    "commercially-sendable",
                    "Commercial Fit State must be commercially-sendable",
                ),
                (
                    "Contact Eligibility",
                    "contact-now",
                    "Contact Eligibility must be contact-now",
                ),
            ),
        )

    def test_prospect_status_vocabulary_is_the_post_split_eight(self):
        # Exact tuple, not a subset: without typecast every one of these has to
        # exist as a choice in the base, so adding or renaming one is a schema
        # change that needs the Airtable runbook, not an edit.
        self.assertEqual(
            getattr(queue_state, "PROSPECT_STATUS_CHOICES", None),
            (
                "New",
                "Mockup Needed",
                "Manual Review",
                "Not Fit",
                "Sent",
                "Follow-Up",
                "Won",
                "Lost",
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

    def test_undecided_statuses_contain_the_whole_dial_queue(self):
        # A queued lead has not been judged yet. If one ever counted as accepted,
        # the KPI would report the machine's own proposals back as approvals.
        for status in queue_state.QUEUE_STATUS_CLAUSE.allowed:
            with self.subTest(status=status):
                self.assertIn(status, queue_state.UNDECIDED_STATUSES)

    def test_queue_gate_is_unchanged_by_the_won_lost_split(self):
        # Evidence that splitting Closed did not move "Ready to Dial": the two
        # queued statuses are untouched, and neither new value joins them —
        # a closed lead, won or lost, is never dialled again.
        self.assertEqual(queue_state.QUEUE_STATUS_CLAUSE.allowed, ("New", "Mockup Needed"))
        for closed in ("Won", "Lost"):
            with self.subTest(status=closed):
                self.assertNotIn(closed, queue_state.QUEUE_STATUS_CLAUSE.allowed)
                self.assertEqual(
                    queue_state.queue_state_for_row(
                        {
                            "Packet Tier": "Sendable Packet",
                            "Commercial Fit State": "commercially-sendable",
                            "Contact Eligibility": "contact-now",
                            "Status": closed,
                        }
                    ),
                    "Blocked: Status must be New or Mockup Needed",
                )

    def test_queue_status_clause_is_canonical(self):
        clause = getattr(queue_state, "QUEUE_STATUS_CLAUSE", None)
        if clause is None:
            self.fail("QUEUE_STATUS_CLAUSE is required for static Airtable queue eligibility")
        self.assertEqual(
            (clause.field, clause.allowed, clause.failure, clause.airtable_condition),
            (
                "Status",
                ("New", "Mockup Needed"),
                "Status must be New or Mockup Needed",
                "OR({Status}='New',{Status}='Mockup Needed')",
            ),
        )

    def test_literal_queue_state_and_readiness_failure_matrix(self):
        evaluator = getattr(queue_state, "queue_state_for_row", None)
        if not callable(evaluator):
            self.fail("queue_state_for_row is required to evaluate formula logic in Python")

        ready = {
            "Packet Tier": "Sendable Packet",
            "Commercial Fit State": "commercially-sendable",
            "Contact Eligibility": "contact-now",
            "Status": "New",
        }
        cases = (
            (
                "packet tier",
                {"Packet Tier": "Manual Review Packet"},
                ["Sendable must be Yes"],
                "Blocked: Sendable must be Yes",
            ),
            (
                "commercial fit",
                {"Commercial Fit State": "minor-fix/nurture"},
                ["Commercial Fit State must be commercially-sendable"],
                "Blocked: Commercial Fit State must be commercially-sendable",
            ),
            (
                "contact eligibility",
                {"Contact Eligibility": "contact-after-asset"},
                ["Contact Eligibility must be contact-now"],
                "Blocked: Contact Eligibility must be contact-now",
            ),
            (
                "status",
                {"Status": "Sent"},
                [],
                "Blocked: Status must be New or Mockup Needed",
            ),
            ("all pass", {}, [], "Ready to Dial"),
        )
        for name, overrides, readiness_failures, queue_state_value in cases:
            with self.subTest(name=name):
                row = {**ready, **overrides}
                self.assertEqual(contact_readiness_failures(row), readiness_failures)
                self.assertEqual(evaluator(row), queue_state_value)

    def test_sendability_uses_the_shared_commercial_clause_reason(self):
        clauses = list(queue_state.READINESS_CLAUSES)
        clauses[1] = replace(clauses[1], failure="Shared commercial readiness failure")
        row = {
            "Packet Tier": "Sendable Packet",
            "Commercial Fit State": "minor-fix/nurture",
            "Contact Eligibility": "contact-now",
        }
        with patch.object(queue_state, "READINESS_CLAUSES", tuple(clauses)):
            self.assertEqual(contact_readiness_failures(row), ["Shared commercial readiness failure"])
