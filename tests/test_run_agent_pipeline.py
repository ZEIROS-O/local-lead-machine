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

    def _write_scored(self, batch: str, suffix: str = "", *, rows: bool = True) -> Path:
        path = self.root / "output" / f"scored_{batch}{suffix}.csv"
        fieldnames = ["Business Name", "Sendable", "Packet Eligible", "Packet Tier"]
        data = (
            [
                {"Business Name": "A", "Sendable": "Yes", "Packet Eligible": "Yes", "Packet Tier": rap.PACKET_TIER_SENDABLE},
                {"Business Name": "B", "Sendable": "No", "Packet Eligible": "Yes", "Packet Tier": rap.PACKET_TIER_MANUAL},
                {"Business Name": "C", "Sendable": "No", "Packet Eligible": "", "Packet Tier": rap.PACKET_TIER_WATCHLIST},
            ]
            if rows
            else []
        )
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        return path

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
                self._write_scored("detailing_cleveland_medium_001", "_api")
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
        self.assertNotIn("--only-sendable", airtable_cmd)
        self.assertNotIn("--include-manual-review", airtable_cmd)
        self.assertIn("--dry-run", airtable_cmd)

    def test_airtable_synced_count_includes_every_scored_row(self) -> None:
        self._write_config()
        self._write_scored("detailing_cleveland_medium_001")
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            rap.main(self._argv("packets-only", airtable="dry-run"))
        summary = json.loads(
            (self.root / "output" / "agent_run_summary_detailing_cleveland_medium_001.json").read_text()
        )
        self.assertEqual(summary["counts"]["airtable_synced"], 3)
        self.assertEqual(summary["airtable"]["scope"], "all_scored_rows")

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

    def test_packets_only_prefers_api_csv_over_empty_plain(self) -> None:
        self._write_config()
        batch = "detailing_cleveland_medium_001"
        # Empty legacy plain CSV plus a populated API CSV.
        self._write_scored(batch, "", rows=False)
        self._write_scored(batch, "_api")
        api_csv = f"output/scored_{batch}_api.csv"
        plain_csv = f"output/scored_{batch}.csv"
        tiered_csv = f"output/scored_{batch}_api_tiered.csv"
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("packets-only"))
        self.assertEqual(code, 0)
        refresh = next(cmd for cmd in commands if "refresh_packet_tiers.py" in cmd)
        self.assertIn(api_csv, refresh)
        self.assertNotIn(plain_csv, refresh)
        # Tiers refresh into the *_api_tiered.csv; packets build from it.
        self.assertIn(tiered_csv, refresh)
        packets = next(cmd for cmd in commands if "make_packets.py" in cmd)
        self.assertIn(tiered_csv, packets)

    def test_score_dry_run_resumes_from_api_csv_and_reports_existing_rows(self) -> None:
        self._write_config()
        batch = "detailing_cleveland_medium_001"
        self._write_enriched(batch)
        self._write_scored(batch, "_api")  # 3 existing scored rows
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("score-dry-run"))
        self.assertEqual(code, 0)
        dry = next(cmd for cmd in commands if "--dry-run-llm-cost" in cmd)
        # The dry-run resumes from / projects against the API CSV, not the plain CSV.
        out_idx = dry.index("--output") + 1
        self.assertEqual(dry[out_idx], f"output/scored_{batch}_api.csv")
        summary = json.loads(
            (self.root / "output" / f"agent_run_summary_{batch}.json").read_text()
        )
        self.assertEqual(summary["counts"]["scored"], 3)

    def test_packets_only_refuses_empty_scored_unless_allowed(self) -> None:
        self._write_config()
        batch = "detailing_cleveland_medium_001"
        self._write_scored(batch, "_api", rows=False)  # 0 rows
        # Refuses without the flag.
        commands: list[list[str]] = []
        out = io.StringIO()
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(out):
            code = rap.main(self._argv("packets-only"))
        self.assertEqual(code, 2)
        self.assertIn("[REFUSE]", out.getvalue())
        self.assertFalse(any("make_packets.py" in cmd for cmd in commands))
        # Proceeds with --allow-empty-scored.
        commands2: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands2)), redirect_stdout(io.StringIO()):
            code2 = rap.main(self._argv("packets-only", allow_empty_scored=None))
        self.assertEqual(code2, 0)
        self.assertTrue(any("make_packets.py" in cmd for cmd in commands2))

    def test_explicit_scored_csv_overrides_default_resolution(self) -> None:
        self._write_config()
        batch = "detailing_cleveland_medium_001"
        self._write_scored(batch, "_api")  # would be picked by default
        custom = self.root / "output" / "custom_scored.csv"
        self._write_scored(batch, "")  # placeholder
        (self.root / "output" / f"scored_{batch}.csv").replace(custom)
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("packets-only", scored_csv=str(custom)))
        self.assertEqual(code, 0)
        refresh = next(cmd for cmd in commands if "refresh_packet_tiers.py" in cmd)
        self.assertIn(str(custom), refresh)
        self.assertNotIn(str(self.root / "output" / f"scored_{batch}_api.csv"), refresh)

    def test_wrapper_passes_llm_token_and_batch_size_to_score_leads(self) -> None:
        self._write_config()
        batch = "detailing_cleveland_medium_001"
        self._write_enriched(batch)
        commands: list[list[str]] = []
        with patch.object(rap, "run", side_effect=self._fake_run(commands)), redirect_stdout(io.StringIO()):
            code = rap.main(self._argv("score-dry-run"))
        self.assertEqual(code, 0)
        score = next(cmd for cmd in commands if "score_leads.py" in cmd)
        self.assertIn("--llm-max-tokens", score)
        self.assertEqual(score[score.index("--llm-max-tokens") + 1], "8192")
        self.assertIn("--llm-batch-size", score)
        self.assertEqual(score[score.index("--llm-batch-size") + 1], "5")

    def _write_profiles(self, vertical: str = "epoxy", market: str = "cleveland") -> None:
        (self.root / "vertical_profiles").mkdir(parents=True, exist_ok=True)
        (self.root / "market_profiles").mkdir(parents=True, exist_ok=True)
        (self.root / "vertical_profiles" / f"{vertical}.json").write_text(
            json.dumps({
                "vertical": vertical,
                "display_name": vertical.title(),
                "search_terms": ["alpha", "beta"],
                "boost_terms": ["alpha", "beta"],
                "exclude_terms": ["noise"],
            }),
            encoding="utf-8",
        )
        (self.root / "market_profiles" / f"{market}.json").write_text(
            json.dumps({
                "market": market,
                "display_name": market.title(),
                "state": "OH",
                "geos": ["A OH", "B OH", "C OH"],
            }),
            encoding="utf-8",
        )

    def test_resolve_batch_auto_generates_config_when_profiles_exist(self) -> None:
        # No search config, but vertical + market profiles are present.
        self._write_profiles(vertical="epoxy", market="cleveland")
        batch, config_path = rap.resolve_batch(
            vertical="epoxy", market="cleveland", size="medium",
            explicit_batch="", mode="full", overwrite=False,
        )
        self.assertEqual(batch, "epoxy_cleveland_medium_001")
        self.assertTrue(config_path.exists())
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["vertical"], "epoxy")
        self.assertIn("alpha Cleveland OH", cfg["queries"])
        self.assertIn("A OH", cfg["geo_slices"])

    def test_resolve_batch_refuses_when_vertical_profile_missing(self) -> None:
        # Only the market profile exists.
        self._write_profiles(vertical="epoxy", market="cleveland")
        (self.root / "vertical_profiles" / "epoxy.json").unlink()
        with self.assertRaises(rap.PipelineRefusal) as ctx:
            rap.resolve_batch(
                vertical="epoxy", market="cleveland", size="medium",
                explicit_batch="", mode="full", overwrite=False,
            )
        self.assertIn("[REFUSE] Missing vertical profile", str(ctx.exception))

    def test_resolve_batch_uses_existing_config_without_profiles(self) -> None:
        # Existing config present, NO profiles -> must not attempt generation.
        self._write_config("epoxy_cleveland_medium_001")
        batch, config_path = rap.resolve_batch(
            vertical="epoxy", market="cleveland", size="medium",
            explicit_batch="", mode="packets-only", overwrite=False,
        )
        self.assertEqual(batch, "epoxy_cleveland_medium_001")
        self.assertTrue(config_path.exists())

    def test_overwrite_reuses_existing_batch(self) -> None:
        self._write_config()
        self._write_enriched("detailing_cleveland_medium_001")
        batch, _ = rap.resolve_batch(
            vertical="detailing", market="cleveland", size="medium",
            explicit_batch="", mode="full", overwrite=True,
        )
        self.assertEqual(batch, "detailing_cleveland_medium_001")

    def test_captured_child_failure_surfaces_stdout_and_stderr(self) -> None:
        fake_result = types.SimpleNamespace(
            returncode=1,
            stdout="child progress line\n",
            stderr="Traceback: boom in child\n",
        )
        with patch("run_agent_pipeline.subprocess.run", return_value=fake_result):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(rap.PipelineRefusal) as ctx:
                    rap.run(["python", "child.py"], capture=True)
        message = str(ctx.exception)
        self.assertIn("Command failed (1):", message)
        self.assertIn("[CHILD STDOUT]", message)
        self.assertIn("child progress line", message)
        self.assertIn("[CHILD STDERR]", message)
        self.assertIn("Traceback: boom in child", message)

    def test_captured_child_success_returns_result(self) -> None:
        fake_result = types.SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        with patch("run_agent_pipeline.subprocess.run", return_value=fake_result):
            with redirect_stdout(io.StringIO()):
                result = rap.run(["python", "child.py"], capture=True)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
