from __future__ import annotations

import csv
import importlib
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_scored(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["Place Key"])
        writer.writeheader()
        writer.writerows(rows)


class GateLivenessEvalTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("evals.gate_liveness")
        except ModuleNotFoundError:
            self.fail("evals.gate_liveness must exist")

    # --- what counts as a gate ---------------------------------------------

    def test_blocker_constants_are_discovered_not_listed(self) -> None:
        """The roster comes from the module, so a new blocker joins the eval
        the moment it is declared."""
        module = self.module()
        from lead_machine import sendability

        constants = module.blocker_constants(sendability)

        self.assertIn("CONVERSION_ISSUE_BLOCKER", constants)
        self.assertEqual(
            constants["CONVERSION_ISSUE_BLOCKER"], sendability.CONVERSION_ISSUE_BLOCKER
        )
        self.assertTrue(all(name.endswith("_BLOCKER") for name in constants))
        self.assertEqual(
            set(constants),
            {name for name in dir(sendability) if name.endswith("_BLOCKER")},
        )

    def test_reachable_blockers_follows_calls_not_just_the_entry_body(self) -> None:
        """`HARD_ICP_SKIP_BLOCKER` is emitted two calls deep from the entry
        point; a shallow scan would call it dead."""
        module = self.module()
        source = (REPO_ROOT / "lead_machine" / "sendability.py").read_text(
            encoding="utf-8"
        )

        reachable = module.reachable_blockers(source, "evaluate_sendability")

        self.assertIn("CONVERSION_ISSUE_BLOCKER", reachable)
        self.assertIn("HARD_ICP_SKIP_BLOCKER", reachable)
        self.assertNotIn("SCORER_WITHHELD_BLOCKER", reachable)
        self.assertNotIn("PACKET_TIER_NOT_SENDABLE_BLOCKER", reachable)

    def test_suffixed_blocker_messages_still_count_as_a_fire(self) -> None:
        """ICP-skip and out-of-country blockers ship as `"<blocker>: <detail>"`;
        matching the bare string would undercount them to zero."""
        module = self.module()

        fires = module.count_fires(
            [
                {"send_blockers": ["Hard ICP gate skipped this lead: franchise"]},
                {"send_blockers": ["No dialable phone number"]},
            ],
            {
                "HARD_ICP_SKIP_BLOCKER": "Hard ICP gate skipped this lead",
                "MISSING_PHONE_BLOCKER": "No dialable phone number",
            },
        )

        self.assertEqual(fires["HARD_ICP_SKIP_BLOCKER"], 1)
        self.assertEqual(fires["MISSING_PHONE_BLOCKER"], 1)

    def test_a_blocker_fires_at_most_once_per_row(self) -> None:
        module = self.module()

        fires = module.count_fires(
            [
                {
                    "send_blockers": [
                        "Hard ICP gate skipped this lead: franchise",
                        "Hard ICP gate skipped this lead: category denylist",
                    ]
                }
            ],
            {"HARD_ICP_SKIP_BLOCKER": "Hard ICP gate skipped this lead"},
        )

        self.assertEqual(fires["HARD_ICP_SKIP_BLOCKER"], 1)

    # --- the categories ------------------------------------------------------

    def evaluate(self, tmp: str, rows, **kwargs):
        module = self.module()
        path = Path(tmp) / "scored_a.csv"
        write_scored(path, rows)
        return module.evaluate_gate_liveness([path], **kwargs)

    def test_every_in_scope_gate_firing_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [{"Place Key": "place-1"}],
                constants={"SCORE_BLOCKER": "Score below B threshold"},
                in_scope={"SCORE_BLOCKER"},
                references={"SCORE_BLOCKER": ["lead_machine/sendability.py"]},
                replay=lambda row: ["Score below B threshold"],
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["fires"]["SCORE_BLOCKER"], 1)
        self.assertEqual(report["findings"]["never_fires"], [])

    def test_an_in_scope_gate_that_never_fires_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [{"Place Key": "place-1"}],
                constants={"ANGLE_BLOCKER": "Missing clear outreach angle"},
                in_scope={"ANGLE_BLOCKER"},
                references={"ANGLE_BLOCKER": ["lead_machine/sendability.py"]},
                replay=lambda row: [],
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"]["never_fires"],
            [
                {
                    "blocker": "ANGLE_BLOCKER",
                    "message": "Missing clear outreach angle",
                    "referenced_by": ["lead_machine/sendability.py"],
                }
            ],
        )

    def test_a_blocker_nothing_references_fails_as_dead_code(self) -> None:
        """The shape of the deleted G3 pair: fully written, wired to nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [{"Place Key": "place-1"}],
                constants={"ORPHAN_BLOCKER": "Nobody reads this"},
                in_scope=set(),
                references={"ORPHAN_BLOCKER": []},
                replay=lambda row: [],
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"]["unreferenced"],
            [{"blocker": "ORPHAN_BLOCKER", "message": "Nobody reads this"}],
        )
        self.assertEqual(report["findings"]["never_fires"], [])

    def test_a_gate_emitted_by_another_module_is_reported_not_failed(self) -> None:
        """The corpus replay runs one entry point; blockers minted by the upload
        and manual-insert paths are out of its reach, not dead."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [{"Place Key": "place-1"}],
                constants={"SCORER_WITHHELD_BLOCKER": "Scorer did not mark this lead sendable"},
                in_scope=set(),
                references={"SCORER_WITHHELD_BLOCKER": ["upload_airtable.py"]},
                replay=lambda row: [],
            )

        self.assertTrue(report["ok"])
        self.assertEqual(
            report["findings"]["outside_replay"],
            [
                {
                    "blocker": "SCORER_WITHHELD_BLOCKER",
                    "message": "Scorer did not mark this lead sendable",
                    "referenced_by": ["upload_airtable.py"],
                }
            ],
        )

    def test_counts_report_the_corpus_the_verdict_rests_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [{"Place Key": "place-1"}, {"Place Key": "place-2"}],
                constants={"SCORE_BLOCKER": "Score below B threshold"},
                in_scope={"SCORE_BLOCKER"},
                references={"SCORE_BLOCKER": ["lead_machine/sendability.py"]},
                replay=lambda row: ["Score below B threshold"],
            )

        self.assertEqual(report["counts"]["scored_rows"], 2)
        self.assertEqual(report["counts"]["scored_files"], 1)
        self.assertEqual(report["counts"]["gates"], 1)

    def test_an_empty_corpus_cannot_certify_anything(self) -> None:
        """Zero rows would report every gate as never-firing, which is a corpus
        failure dressed as 27 code failures."""
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scored_a.csv"
            write_scored(path, [])
            with self.assertRaises(ValueError):
                module.evaluate_gate_liveness(
                    [path],
                    constants={"SCORE_BLOCKER": "Score below B threshold"},
                    in_scope={"SCORE_BLOCKER"},
                    references={"SCORE_BLOCKER": ["lead_machine/sendability.py"]},
                    replay=lambda row: [],
                )

    # --- CLI -----------------------------------------------------------------

    def test_main_exits_non_zero_when_a_gate_is_dead(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scored_a.csv"
            write_scored(path, [{"Place Key": "place-1"}])
            code = module.main(["--scored-file", str(path)])

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
