from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lead_machine.sendability import VERDICT_PACKET_TIERS, evaluate_sendability
from tests.test_verdict import SENDABLE_ROW


OPERATIONAL = {
    "Status": "Follow-Up",
    "Last Contacted": "2026-08-10",
    "Next Follow-Up": "2026-08-14",
    "Assigned Setter": "Lou",
    "Outreach Channel": "Phone",
}


class RefreshVerdictsTests(unittest.TestCase):
    def test_refreshes_stale_send_verdict_from_current_authority(self) -> None:
        from refresh_verdicts import refresh_row

        row = {**SENDABLE_ROW, **OPERATIONAL, "Verdict": "reject", "Verdict Reasons": "old"}
        updated = refresh_row(row)

        self.assertEqual(updated["Verdict"], "send")
        self.assertEqual(updated["Verdict Reasons"], "; ".join(evaluate_sendability(updated)["send_blockers"]))
        self.assertEqual(updated["Packet Tier"], VERDICT_PACKET_TIERS["send"])
        self.assertTrue(evaluate_sendability(updated)["sendable"])
        for field, value in OPERATIONAL.items():
            self.assertEqual(updated[field], value)

    def test_refreshes_stale_send_on_rejected_row(self) -> None:
        from refresh_verdicts import refresh_row

        row = {**SENDABLE_ROW, "Phone": "", "Verdict": "send", "Verdict Reasons": "old"}
        updated = refresh_row(row)

        self.assertEqual(updated["Verdict"], "reject")
        self.assertEqual(updated["Packet Tier"], VERDICT_PACKET_TIERS["reject"])
        self.assertFalse(evaluate_sendability(updated)["sendable"])

    def test_csv_round_trip_handles_large_raw_evidence_and_is_idempotent(self) -> None:
        from refresh_verdicts import refresh_csv

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            output = Path(directory) / "output.csv"
            fieldnames = [*SENDABLE_ROW.keys(), *OPERATIONAL.keys(), "Raw Evidence", "Verdict"]
            fieldnames = list(dict.fromkeys(fieldnames))
            row = {**SENDABLE_ROW, **OPERATIONAL, "Raw Evidence": "x" * 131_073, "Verdict": "reject"}
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerow(row)

            first = refresh_csv(source, output)
            first_bytes = output.read_bytes()
            second = refresh_csv(output, output)
            second_bytes = output.read_bytes()

            self.assertEqual(first["verdict_counts"], {"send": 1})
            self.assertEqual(second["verdict_counts"], {"send": 1})
            self.assertEqual(first_bytes, second_bytes)
            with output.open(newline="", encoding="utf-8") as handle:
                result = next(csv.DictReader(handle))
            self.assertEqual(len(result["Raw Evidence"]), 131_073)

    def test_check_reports_without_writing(self) -> None:
        from refresh_verdicts import main

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Business Name", "Phone", "Verdict"])
                writer.writeheader()
                writer.writerow({"Business Name": "x", "Phone": "", "Verdict": "send"})
            before = source.read_bytes()
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([str(source), "--check"]), 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertIn('"verdict_counts"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
