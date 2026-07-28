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
