"""What the pre-dial decision ledger has to guarantee.

The accept-rate KPI reads `Status`, and a completed dial moves a row to
`Follow-Up`, which `queue_state.ACCEPTED_STATUSES` counts as accepted. So a rep
who simply works the queue top to bottom manufactures a 100% accept rate: the
number measures that the workflow ran, not that a human judged the lead worth
calling. It cannot answer whether removing the commercial tribunal cost us
anything, which is the one question the strip needs answered.

This ledger records the judgement *before* the dial, where the rep can still
say no. The rules below are what keep it from degrading into the same
tautology — chiefly that a refusal without a reason is not a decision, and that
a lead already dialled can no longer be decided.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from lead_machine.dial_decisions import (
    DECISION_DECLINE,
    DECISION_DIAL,
    DialDecisionError,
    append_decision,
    approval_rate,
    load_decisions,
    read_ledger,
)


class LedgerWriting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def test_a_decision_lands_as_one_json_line(self) -> None:
        append_decision(
            self.path,
            decision=DECISION_DIAL,
            record_id="rec1",
            decided_at="2026-08-10T12:00:00+00:00",
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["decision"], DECISION_DIAL)

    def test_appending_never_rewrites_an_earlier_row(self) -> None:
        # Append-only for the same reason the suppression ledger is: a decision
        # already made is evidence, and a later run must not be able to erase it.
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        append_decision(self.path, decision=DECISION_DECLINE, record_id="rec2", reason="no owner")
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_a_decline_without_a_reason_is_refused(self) -> None:
        # The reason is the entire value of the row. "Not fit" with no why tells
        # us a lead was rejected but nothing about which signal was wrong, which
        # is the only thing that can retire or restore a demoted blocker.
        with self.assertRaises(DialDecisionError):
            append_decision(self.path, decision=DECISION_DECLINE, record_id="rec1")
        with self.assertRaises(DialDecisionError):
            append_decision(self.path, decision=DECISION_DECLINE, record_id="rec1", reason="   ")
        self.assertFalse(self.path.exists())

    def test_a_dial_needs_no_reason(self) -> None:
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        self.assertEqual(load_decisions(self.path)[0]["reason"], "")

    def test_an_unknown_decision_is_refused(self) -> None:
        with self.assertRaises(DialDecisionError):
            append_decision(self.path, decision="maybe", record_id="rec1")

    def test_a_row_without_a_record_id_is_refused(self) -> None:
        # A decision that names no lead cannot be joined to the rank that
        # produced it, so it can never answer whether the ranking was right.
        with self.assertRaises(DialDecisionError):
            append_decision(self.path, decision=DECISION_DIAL, record_id="  ")

    def test_the_timestamp_is_stamped_when_the_caller_omits_it(self) -> None:
        row = append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        self.assertTrue(row["decided_at"])
        self.assertIn("T", row["decided_at"])

    def test_the_rank_at_decision_time_is_carried(self) -> None:
        # The queue order is the thing under test. A decision recorded without
        # the rank it was served at cannot tell us whether rank predicts
        # approval, which is the ranking's own falsifier.
        row = append_decision(
            self.path,
            decision=DECISION_DECLINE,
            record_id="rec1",
            reason="wrong vertical",
            rank=61.5,
            business_name="Acme Epoxy",
        )
        self.assertEqual(row["rank"], 61.5)
        self.assertEqual(load_decisions(self.path)[0]["business_name"], "Acme Epoxy")


class AnUnwritableLedgerFailsAsOneKind(unittest.TestCase):
    """Every write failure must arrive as `DialDecisionError`, never a bare OSError.

    `mark_sent` records the dial *after* the Airtable mutation is confirmed, and
    catches this type in order to warn instead of failing. An OSError escaping
    that catch tracebacks the script after a real state change, which reads as
    "that did not work" and invites the rep to run it again.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_an_uncreatable_parent_directory_raises_the_ledger_error(self) -> None:
        # The parent must be *missing* to reach the mkdir — an existing-but-bad
        # parent is caught by the open() below it and proves nothing about the
        # mkdir. Here a plain file stands where a parent directory would go, so
        # `mkdir(parents=True)` raises NotADirectoryError.
        blocker = Path(self.tmp.name) / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        with self.assertRaises(DialDecisionError):
            append_decision(
                blocker / "nested" / "decisions.jsonl",
                decision=DECISION_DIAL,
                record_id="rec1",
            )

    def test_an_unwritable_parent_directory_raises_the_ledger_error(self) -> None:
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        if os.access(locked, os.W_OK):  # root ignores the mode; skip rather than lie
            self.skipTest("cannot make a directory unwritable as this user")
        with self.assertRaises(DialDecisionError):
            append_decision(
                locked / "nested" / "decisions.jsonl",
                decision=DECISION_DIAL,
                record_id="rec1",
            )

    def test_an_unwritable_ledger_file_raises_the_ledger_error(self) -> None:
        # The already-covered case, kept explicit: parent exists, open() fails.
        locked = Path(self.tmp.name) / "locked-file"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        if os.access(locked, os.W_OK):
            self.skipTest("cannot make a directory unwritable as this user")
        with self.assertRaises(DialDecisionError):
            append_decision(
                locked / "decisions.jsonl", decision=DECISION_DIAL, record_id="rec1"
            )


class ReadingTheLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def test_a_missing_ledger_reads_as_no_decisions(self) -> None:
        self.assertEqual(load_decisions(self.path), [])

    def test_a_corrupt_line_is_skipped_rather_than_losing_the_file(self) -> None:
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        append_decision(self.path, decision=DECISION_DECLINE, record_id="rec2", reason="closed")
        self.assertEqual(len(load_decisions(self.path)), 2)

    def test_the_skipped_lines_are_counted_rather_than_vanishing(self) -> None:
        # Skipping the line keeps the file readable; not counting it is what
        # makes the KPI a liar. A ledger whose declines are all malformed loads
        # as pure dials and reports a 100% approval rate — the go/no-go number
        # silently dropping every adverse observation it holds.
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
            handle.write("[]\n")
            handle.write(json.dumps({"decision": "shrug", "record_id": "rec9"}) + "\n")
            handle.write(json.dumps({"decision": DECISION_DIAL}) + "\n")
        read = read_ledger(self.path)
        self.assertEqual(len(read.decisions), 1)
        self.assertEqual(read.unreadable, 4)

    def test_a_clean_ledger_reports_nothing_unreadable(self) -> None:
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        append_decision(self.path, decision=DECISION_DECLINE, record_id="rec2", reason="closed")
        read = read_ledger(self.path)
        self.assertEqual(len(read.decisions), 2)
        self.assertEqual(read.unreadable, 0)

    def test_a_missing_ledger_is_not_an_unreadable_one(self) -> None:
        # No file means the rep has not started. That is an absence of
        # evidence, not evidence that was destroyed, and it must not raise the
        # alarm the corrupt-line count exists to raise.
        read = read_ledger(self.path)
        self.assertEqual(read.decisions, [])
        self.assertEqual(read.unreadable, 0)

    def test_the_rate_is_dials_over_decisions(self) -> None:
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec1")
        append_decision(self.path, decision=DECISION_DIAL, record_id="rec2")
        append_decision(self.path, decision=DECISION_DECLINE, record_id="rec3", reason="no phone")
        summary = approval_rate(load_decisions(self.path))
        self.assertEqual(summary["dialed"], 2)
        self.assertEqual(summary["declined"], 1)
        self.assertAlmostEqual(summary["rate"], 2 / 3)

    def test_no_decisions_reports_no_rate_rather_than_a_green_zero(self) -> None:
        # A rate of 0.0 over an empty ledger reads as a failing loop; None says
        # nobody has decided anything yet, which is the truth.
        self.assertIsNone(approval_rate([])["rate"])

    def test_a_lead_decided_twice_counts_once_and_the_later_call_wins(self) -> None:
        # A rep who declines a lead and is overruled must not appear in the
        # denominator twice, or the rate drifts with revisits rather than leads.
        append_decision(
            self.path,
            decision=DECISION_DECLINE,
            record_id="rec1",
            reason="looked thin",
            decided_at="2026-08-10T10:00:00+00:00",
        )
        append_decision(
            self.path,
            decision=DECISION_DIAL,
            record_id="rec1",
            decided_at="2026-08-10T11:00:00+00:00",
        )
        summary = approval_rate(load_decisions(self.path))
        self.assertEqual(summary["decided"], 1)
        self.assertEqual(summary["dialed"], 1)
        self.assertEqual(summary["declined"], 0)

    def test_decline_reasons_are_tallied(self) -> None:
        # The tally is the actionable half: "12 declined for no owner name" is a
        # fix, where "12 declined" is only a number.
        for i, reason in enumerate(["no owner", "no owner", "wrong vertical"]):
            append_decision(
                self.path, decision=DECISION_DECLINE, record_id=f"rec{i}", reason=reason
            )
        summary = approval_rate(load_decisions(self.path))
        self.assertEqual(summary["decline_reasons"]["no owner"], 2)
        self.assertEqual(summary["decline_reasons"]["wrong vertical"], 1)


if __name__ == "__main__":
    unittest.main()
