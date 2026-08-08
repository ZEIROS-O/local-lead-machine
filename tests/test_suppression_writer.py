"""Tests for the suppression ledger *writer* — compliance.py's first write path.

Until this shipped, `lead_machine/compliance.py` could only read a suppression
ledger. Every `opt_out` row in the repo lived in a test fixture: no shipped code
path could produce one, so a rep who heard "take me off your list" had nowhere
to put it. These tests pin the writer that closes that hole, and in particular
that what it writes is exactly what `load_suppression_index` reads back.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lead_machine.compliance import (
    ComplianceError,
    SUPPRESSION_EVENTS,
    append_suppression_event,
    configured_suppression_source,
    is_suppressed,
    load_suppression_index,
    suppression_identifier,
)


class SuppressionWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = Path(self._tmp.name) / "outcomes.jsonl"
        self.ledger.write_text("", encoding="utf-8")

    def _rows(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_what_the_writer_writes_is_what_the_reader_reads(self) -> None:
        """The whole point: a round trip through the two halves of the module."""
        receipt = append_suppression_event(
            self.ledger,
            record_id="rec42",
            phone="(216) 555-0100",
            email="Owner@Example.COM",
            source="mark_outcome",
        )
        self.assertEqual(receipt["result"], "appended")

        index = load_suppression_index(self.ledger)
        self.assertIn("record_id:rec42", index)
        self.assertIn("phone:2165550100", index)
        self.assertIn("email:owner@example.com", index)

    def test_phone_and_email_are_captured_not_only_the_record_id(self) -> None:
        """`record_matches_suppression` matches on contact channels.

        A suppressed prospect re-discovered under a *new* Airtable record has a
        different record id and the same phone, so a record-id-only ledger row
        would let the pipeline re-admit it.
        """
        append_suppression_event(self.ledger, record_id="rec42", phone="216-555-0100", email="a@b.com")
        index = load_suppression_index(self.ledger)

        rediscovered = {"record_id": "recBRANDNEW", "Phone": "+1 (216) 555-0100"}
        self.assertTrue(is_suppressed(rediscovered, index))

        by_email = {"record_id": "recALSONEW", "Email": "A@B.com"}
        self.assertTrue(is_suppressed(by_email, index))

    def test_repeat_is_idempotent_and_appends_nothing(self) -> None:
        first = append_suppression_event(self.ledger, record_id="rec42", phone="2165550100")
        second = append_suppression_event(self.ledger, record_id="rec42", phone="(216) 555-0100")
        self.assertEqual(first["result"], "appended")
        self.assertEqual(second["result"], "already_present")
        self.assertEqual(len(self._rows()), 1)

    def test_a_new_channel_on_a_known_record_still_appends(self) -> None:
        """Idempotency must not swallow information the ledger does not have."""
        append_suppression_event(self.ledger, record_id="rec42", phone="2165550100")
        receipt = append_suppression_event(
            self.ledger, record_id="rec42", phone="2165550100", email="late@example.com"
        )
        self.assertEqual(receipt["result"], "appended")
        self.assertEqual(len(self._rows()), 2)
        self.assertIn("email:late@example.com", load_suppression_index(self.ledger))

    def test_append_only_never_rewrites_existing_rows(self) -> None:
        self.ledger.write_text(
            json.dumps({"event": "sent", "record_id": "recOLD", "channel": "Phone"}) + "\n",
            encoding="utf-8",
        )
        append_suppression_event(self.ledger, record_id="rec42", phone="2165550100")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"event": "sent", "record_id": "recOLD", "channel": "Phone"})

    def test_a_ledger_missing_its_trailing_newline_is_not_corrupted(self) -> None:
        self.ledger.write_text(
            json.dumps({"event": "sent", "record_id": "recOLD"}),  # no "\n"
            encoding="utf-8",
        )
        append_suppression_event(self.ledger, record_id="rec42")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["record_id"], "rec42")

    def test_the_row_is_fsynced_before_the_writer_returns(self) -> None:
        """A rep-reported opt-out that only lives in the page cache is not recorded."""
        with patch("lead_machine.compliance.os.fsync", wraps=os.fsync) as fsync:
            append_suppression_event(self.ledger, record_id="rec42")
        self.assertTrue(fsync.called)

    def test_an_event_the_reader_would_skip_is_refused(self) -> None:
        """Writing an unrecognised event would suppress nobody, silently."""
        with self.assertRaisesRegex(ComplianceError, "not a suppression event"):
            append_suppression_event(self.ledger, event="replied", record_id="rec42")
        self.assertEqual(self._rows(), [])

    def test_every_declared_suppression_event_can_be_written(self) -> None:
        for event in sorted(SUPPRESSION_EVENTS):
            with self.subTest(event=event):
                append_suppression_event(self.ledger, event=event, record_id=f"rec-{event}")
        index = load_suppression_index(self.ledger)
        for event in SUPPRESSION_EVENTS:
            self.assertIn(suppression_identifier("record_id", f"rec-{event}"), index)

    def test_a_row_with_no_identifier_is_refused(self) -> None:
        """`load_suppression_index` raises on such a row: writing one bricks the ledger."""
        with self.assertRaisesRegex(ComplianceError, "identifier"):
            append_suppression_event(self.ledger, notes="they said stop")
        self.assertEqual(self._rows(), [])

    def test_a_missing_ledger_is_refused_rather_than_created(self) -> None:
        """Fail closed on the path, too.

        Silently creating the file would record the opt-out into somewhere
        nothing reads — the rep believes they are covered and they are not.
        """
        missing = Path(self._tmp.name) / "nope" / "outcomes.jsonl"
        with self.assertRaisesRegex(ComplianceError, "missing or unreadable"):
            append_suppression_event(missing, record_id="rec42")
        self.assertFalse(missing.exists())

    def test_a_malformed_ledger_is_refused_before_appending(self) -> None:
        self.ledger.write_text("{not json\n", encoding="utf-8")
        with self.assertRaisesRegex(ComplianceError, "malformed JSON"):
            append_suppression_event(self.ledger, record_id="rec42")

    def test_provenance_is_recorded_on_the_row(self) -> None:
        append_suppression_event(
            self.ledger,
            record_id="rec42",
            occurred_at="2026-08-07T12:00:00+00:00",
            source="mark_outcome",
            notes="asked to be removed on the first dial",
        )
        row = self._rows()[0]
        self.assertEqual(row["event"], "opt_out")
        self.assertEqual(row["source"], "mark_outcome")
        self.assertEqual(row["occurred_at"], "2026-08-07T12:00:00+00:00")
        self.assertEqual(row["notes"], "asked to be removed on the first dial")
        # Empty optional keys are omitted rather than written as blanks.
        self.assertNotIn("phone", row)
        self.assertNotIn("email", row)


class ConfiguredSuppressionSourceTests(unittest.TestCase):
    def test_resolution_order_matches_the_reader(self) -> None:
        with patch.dict(os.environ, {"SUPPRESSION_SOURCE": "/a", "LPM_OUTCOMES_JSONL": "/b"}, clear=True):
            self.assertEqual(configured_suppression_source(), "/a")
        with patch.dict(os.environ, {"LPM_OUTCOMES_JSONL": "/b"}, clear=True):
            self.assertEqual(configured_suppression_source(), "/b")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_suppression_source(), "")


if __name__ == "__main__":
    unittest.main()
