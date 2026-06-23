from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import run_agent_pipeline as rap


def _flatten(commands: list[list[str]]) -> list[str]:
    return [part for cmd in commands for part in cmd]


class AgentPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_cwd = Path.cwd()
        os.chdir(self.root)
        (self.root / "search_configs").mkdir(parents=True, exist_ok=True)
        (self.root / "output").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    # ---- fixtures ------------------------------------------------------- #
    def _write_config(self, batch: str = "detailing_cleveland_medium_001") -> None:
        (self.root / "search_configs" / f"{batch}.json").write_text(
            json.dumps({"batch": batch, "vertical": "detailing", "queries": ["x"]}),
            encoding="utf-8",
        )

    def _write_enriched(self, batch: str, count: int = 3) -> None:
        path = self.root / "output" / f"enriched_{batch}.jsonl"
        rows = [{"google": {"business_name": f"L{i}", "place_id": f"p{i}"}, "evidence": {}} for i in range(count)]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def _write_scored(self, batch: str) -> None:
        path = self.root / "output" / f"scored_{batch}.csv"
        rows = [
            {"Business Name": "A", "Sendable": "Yes", "Packet Eligible": "Yes", "Packet Tier": rap.PACKET_TIER_SENDABLE},
            {"Business Name": "B", "Sendable": "No", "Packet Eligible": "Yes", "Packet Tier": rap.PACKET_TIER_MANUAL},
            {"Business Name": "C", "Sendable": "No", "Packet Eligible": "", "Packet Tier": rap.PACKET_TIER_WATCHLIST},
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def _fake_run(self, commands: list[list[str]], *, projected: float = 0.05):
        """Record commands; emit a dry-run cost line when capturing."""
        def runner(cmd: list[str], *, capture: bool = False, check: bool = True):
            commands.append(cmd)
            if "--dry-run-llm-cost" in cmd:
                stdout = f"[LLM COST] records_selected=3 batches=1 projected_usd={projected:.4f}\n"
                return types.SimpleNamespace(returncode=0, stdout=stdout)
            return types.SimpleNamespace(returncode=0, stdout="")
        return runner

    def _argv(self, mode: str, **extra: str) -> list[str]:
        argv = ["--vertical", "detailing", "--market", "cleveland", "--size", "medium", "--mode", mode]
        for key, value in extra.items():
            flag = "--" + key.replace("_", "-")
            if value is None:
                argv.append(flag)
            else:
                argv.extend([flag, value])
        return argv

    # ---- tests ---------------------------------------------------------- #
    def test_dry_run_mode_never_calls_paid_scoring(self) -> None:
        self._write_config()
        self._write_enriched("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("score-dry-run"))
        flat = _flatten(commands)
        self.assertEqual(code, 0)
        self.assertIn("--dry-run-llm-cost", flat)
        self.assertNotIn("--confirm-llm-spend", flat)
        self.assertFalse(any("make_packets.py" in cmd for cmd in commands))

    def test_full_mode_refuses_when_cost_exceeds_max(self) -> None:
        self._write_config()
        self._write_enriched("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        out = io.StringIO()
        with patch.object(rap, "run", side_effect=self._fake_run(commands, projected=5.0)), redirect_stdout(out):
            code = rap.main(self._argv("full", max_llm_usd="1.00", overwrite=None, yes=None))
        flat = _flatten(commands)
        self.assertEqual(code, 2)
        self.assertIn("[REFUSE]", out.getvalue())
        self.assertIn("--dry-run-llm-cost", flat)
        self.assertNotIn("--confirm-llm-spend", flat)
        self.assertFalse(any("make_packets.py" in cmd for cmd in commands))

    def test_full_mode_runs_paid_scoring_when_under_max_and_confirmed(self) -> None:
        self._write_config()
        self._write_enriched("detailing_cleveland_medium_001")

        def runner(cmd, *, capture: bool = False, check: bool = True):
            if "--dry-run-llm-cost" in cmd:
                return types.SimpleNamespace(returncode=0, stdout="projected_usd=0.7000\n")
            if "score_leads.py" in cmd and "--confirm-llm-spend" in cmd:
                self._write_scored("detailing_cleveland_medium_001")
            return types.SimpleNamespace(returncode=0, stdout="")

        recorded: list[list[str]] = []

        def recording(cmd, **kw):
            recorded.append(cmd)
            return runner(cmd, **kw)

        with patch.object(rap, "run", side_effect=recording), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("full", max_llm_usd="1.00", overwrite=None, yes=None))
        flat = _flatten(recorded)
        self.assertEqual(code, 0)
        self.assertIn("--confirm-llm-spend", flat)
        self.assertIn("--fresh-scores-dir", flat)
        self.assertTrue(any("make_packets.py" in cmd for cmd in recorded))

    def test_lockfile_prevents_concurrent_run(self) -> None:
        self._write_config()
        self._write_enriched("detailing_cleveland_medium_001")
        lock = self.root / "output" / ".agent_pipeline.lock"
        # A live lock held by this (alive) process.
        lock.write_text(f"{os.getpid()} now batch\n", encoding="utf-8")
        commands: list[list[str]] = []
        out = io.StringIO()
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(out):
            code = rap.main(self._argv("score-dry-run"))
        self.assertEqual(code, 2)
        self.assertIn("[REFUSE]", out.getvalue())
        self.assertEqual(commands, [])

    def test_packets_only_refreshes_tier_and_packets_from_existing_scored(self) -> None:
        self._write_config()
        self._write_scored("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("packets-only"))
        self.assertEqual(code, 0)
        self.assertTrue(any("refresh_packet_tiers.py" in cmd for cmd in commands))
        self.assertTrue(any("make_packets.py" in cmd for cmd in commands))
        self.assertFalse(any("score_leads.py" in cmd for cmd in commands))

    def test_airtable_not_called_unless_requested(self) -> None:
        self._write_config()
        self._write_scored("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            rap.main(self._argv("packets-only"))
        self.assertFalse(any("upload_airtable.py" in cmd for cmd in commands))

    def test_airtable_called_when_requested(self) -> None:
        self._write_config()
        self._write_scored("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            rap.main(self._argv("packets-only", airtable="dry-run"))
        airtable_cmd = next((cmd for cmd in commands if "upload_airtable.py" in cmd), None)
        self.assertIsNotNone(airtable_cmd)
        self.assertIn("--only-sendable", airtable_cmd)
        self.assertIn("--dry-run", airtable_cmd)

    def test_summary_counts_packet_tiers(self) -> None:
        self._write_config()
        self._write_scored("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            rap.main(self._argv("packets-only"))
        summary = json.loads(
            (self.root / "output" / "agent_run_summary_detailing_cleveland_medium_001.json").read_text()
        )
        self.assertEqual(summary["counts"]["sendable_packet"], 1)
        self.assertEqual(summary["counts"]["manual_review_packet"], 1)
        self.assertEqual(summary["counts"]["watchlist"], 1)

    def test_search_mode_forks_batch_when_outputs_exist(self) -> None:
        self._write_config()
        # Existing outputs for _001 should trigger a fork to _002.
        self._write_enriched("detailing_cleveland_medium_001")
        batch, config_path = rap.resolve_batch(
            vertical="detailing", market="cleveland", size="medium",
            explicit_batch="", mode="full", overwrite=False,
        )
        self.assertEqual(batch, "detailing_cleveland_medium_002")
        self.assertTrue(config_path.exists())

    def test_overwrite_reuses_existing_batch(self) -> None:
        self._write_config()
        self._write_enriched("detailing_cleveland_medium_001")
        batch, _ = rap.resolve_batch(
            vertical="detailing", market="cleveland", size="medium",
            explicit_batch="", mode="full", overwrite=True,
        )
        self.assertEqual(batch, "detailing_cleveland_medium_001")


if __name__ == "__main__":
    unittest.main()
