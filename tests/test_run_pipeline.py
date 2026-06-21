from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import run_pipeline


class RunPipelineTest(unittest.TestCase):
    def _write_enriched(self, path: Path, count: int = 2) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"google": {"business_name": f"Lead {idx}", "place_id": f"place-{idx}"}, "evidence": {}}
            for idx in range(1, count + 1)
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def _write_scored(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"Business Name": "Lead A", "Lead Priority": "A", "Sendable": "Yes", "Packet Eligible": "Yes"},
            {"Business Name": "Lead B", "Lead Priority": "B", "Sendable": "No", "Packet Eligible": "No"},
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def _write_packet_index(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        with (path / "packet_index.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Packet Path", "Business Name"])
            writer.writeheader()
            writer.writerow({"Packet Path": "packets_test/A/001-lead-a.md", "Business Name": "Lead A"})

    def test_default_api_llm_mode_scores_without_manual_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            old_cwd = Path.cwd()
            os.chdir(root)
            commands: list[list[str]] = []

            def fake_run(cmd: list[str]) -> None:
                commands.append(cmd)
                if "score_leads.py" in cmd:
                    self._write_scored(Path("output/scored_test.csv"))
                if "make_packets.py" in cmd:
                    self._write_packet_index(Path("packets_test"))

            try:
                stdout = io.StringIO()
                argv = [
                    "run_pipeline.py",
                    "--batch",
                    "test",
                    "--vertical",
                    "detailing",
                    "--skip-search",
                    "--skip-enrich",
                    "--skip-airtable",
                ]
                with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=fake_run), redirect_stdout(stdout):
                    code = run_pipeline.main()
            finally:
                os.chdir(old_cwd)

        score_cmd = next(cmd for cmd in commands if "score_leads.py" in cmd)
        self.assertEqual(code, 0)
        self.assertIn("--mode", score_cmd)
        self.assertIn("api-llm", score_cmd)
        self.assertNotIn("make_llm_queue.py", [part for cmd in commands for part in cmd])
        self.assertNotIn("[WAITING] Paste manual LLM JSON scores", stdout.getvalue())

    def test_manual_llm_mode_preserves_manual_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            old_cwd = Path.cwd()
            os.chdir(root)
            commands: list[list[str]] = []

            try:
                stdout = io.StringIO()
                argv = [
                    "run_pipeline.py",
                    "--batch",
                    "test",
                    "--vertical",
                    "detailing",
                    "--mode",
                    "manual-llm",
                    "--skip-search",
                    "--skip-enrich",
                    "--skip-airtable",
                ]
                with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=commands.append), redirect_stdout(stdout):
                    code = run_pipeline.main()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0)
        self.assertTrue(any("make_llm_queue.py" in cmd for cmd in commands))
        self.assertFalse(any("score_leads.py" in cmd for cmd in commands))
        self.assertIn("[WAITING] Paste manual LLM JSON scores", stdout.getvalue())

    def test_summary_includes_llm_usage_caps_and_key_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            old_cwd = Path.cwd()
            os.chdir(root)

            def fake_run(cmd: list[str]) -> None:
                if "score_leads.py" in cmd:
                    self._write_scored(Path("output/scored_test.csv"))
                    Path("output/llm_usage_test.json").write_text(
                        json.dumps(
                            {
                                "calls": 2,
                                "input_tokens": 100,
                                "output_tokens": 50,
                                "total_tokens": 150,
                                "estimated_usd": 0.00105,
                                "caps": {
                                    "MAX_LLM_CALLS_PER_RUN": 5,
                                    "MAX_LLM_TOKENS_PER_RUN": 1000,
                                    "MAX_LLM_USD_PER_RUN": 1.5,
                                },
                                "stopped_by_cap": False,
                                "stop_reason": "",
                            }
                        ),
                        encoding="utf-8",
                    )
                if "make_packets.py" in cmd:
                    self._write_packet_index(Path("packets_test"))

            try:
                argv = [
                    "run_pipeline.py",
                    "--batch",
                    "test",
                    "--vertical",
                    "detailing",
                    "--skip-search",
                    "--skip-enrich",
                    "--skip-airtable",
                ]
                with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=fake_run), redirect_stdout(io.StringIO()):
                    run_pipeline.main()
                summary = json.loads(Path("output/run_summary_test.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(summary["counts"]["leads_in"], 0)
        self.assertEqual(summary["counts"]["enriched"], 2)
        self.assertEqual(summary["counts"]["scored"], 2)
        self.assertEqual(summary["counts"]["priority_ab"], 2)
        self.assertEqual(summary["counts"]["sendable"], 1)
        self.assertEqual(summary["counts"]["packets"], 1)
        self.assertEqual(summary["llm"]["calls"]["used"], 2)
        self.assertEqual(summary["llm"]["calls"]["cap"], 5)
        self.assertEqual(summary["llm"]["tokens"]["used"], 150)
        self.assertEqual(summary["llm"]["tokens"]["cap"], 1000)
        self.assertEqual(summary["llm"]["usd"]["used"], 0.00105)
        self.assertEqual(summary["llm"]["usd"]["cap"], 1.5)
        self.assertEqual(summary["artifacts"]["scored"], "output/scored_test.csv")
        self.assertEqual(summary["artifacts"]["summary"], "output/run_summary_test.json")

    def test_airtable_push_can_be_skipped_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            old_cwd = Path.cwd()
            os.chdir(root)
            commands: list[list[str]] = []

            def fake_run(cmd: list[str]) -> None:
                commands.append(cmd)
                if "score_leads.py" in cmd:
                    self._write_scored(Path("output/scored_test.csv"))
                if "make_packets.py" in cmd:
                    self._write_packet_index(Path("packets_test"))

            try:
                argv = [
                    "run_pipeline.py",
                    "--batch",
                    "test",
                    "--vertical",
                    "detailing",
                    "--skip-search",
                    "--skip-enrich",
                    "--skip-airtable",
                ]
                with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=fake_run), redirect_stdout(io.StringIO()):
                    run_pipeline.main()
                summary = json.loads(Path("output/run_summary_test.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertFalse(any("upload_airtable.py" in cmd for cmd in commands))
        self.assertEqual(summary["airtable"]["status"], "skipped")
        self.assertEqual(summary["counts"]["airtable_rows"], 0)

    def test_cap_hit_partial_scoring_still_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl", count=3)
            old_cwd = Path.cwd()
            os.chdir(root)

            def fake_run(cmd: list[str]) -> None:
                if "score_leads.py" in cmd:
                    self._write_scored(Path("output/scored_test.csv"))
                    Path("output/llm_usage_test.json").write_text(
                        json.dumps(
                            {
                                "calls": 1,
                                "input_tokens": 75,
                                "output_tokens": 25,
                                "total_tokens": 100,
                                "estimated_usd": 0.0006,
                                "caps": {
                                    "MAX_LLM_CALLS_PER_RUN": 1,
                                    "MAX_LLM_TOKENS_PER_RUN": 0,
                                    "MAX_LLM_USD_PER_RUN": 0.0,
                                },
                                "stopped_by_cap": True,
                                "stop_reason": "MAX_LLM_CALLS_PER_RUN",
                            }
                        ),
                        encoding="utf-8",
                    )
                if "make_packets.py" in cmd:
                    self._write_packet_index(Path("packets_test"))

            try:
                stdout = io.StringIO()
                argv = [
                    "run_pipeline.py",
                    "--batch",
                    "test",
                    "--vertical",
                    "detailing",
                    "--skip-search",
                    "--skip-enrich",
                    "--skip-airtable",
                ]
                with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=fake_run), redirect_stdout(stdout):
                    code = run_pipeline.main()
                summary = json.loads(Path("output/run_summary_test.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0)
        self.assertTrue(summary["llm"]["stopped_by_cap"])
        self.assertEqual(summary["llm"]["stop_reason"], "MAX_LLM_CALLS_PER_RUN")
        self.assertEqual(summary["counts"]["scored"], 2)
        self.assertIn("Stopped by cap: MAX_LLM_CALLS_PER_RUN", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
