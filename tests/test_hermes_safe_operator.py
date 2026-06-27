from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lead_machine import hermes_commands as hc
from lead_machine.hermes_batch import BatchPaths, resolve_batch_paths
from lead_machine.hermes_executor import StreamResult
from lead_machine.hermes_state import current_sync_hash


def make_repo_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("run_agent_pipeline.py", "score_leads.py"):
        (root / name).write_text("# fixture\n", encoding="utf-8")
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / "search_configs").mkdir(parents=True, exist_ok=True)
    (root / "vertical_profiles").mkdir(parents=True, exist_ok=True)
    (root / "market_profiles").mkdir(parents=True, exist_ok=True)
    (root / "vertical_profiles" / "detailing.json").write_text("{}", encoding="utf-8")
    (root / "market_profiles" / "cleveland.json").write_text("{}", encoding="utf-8")
    (root / "search_configs" / "detailing_cleveland_medium_001.json").write_text("{}", encoding="utf-8")


class HermesCommandPlanningTest(unittest.TestCase):
    def test_run_builds_search_only_command(self) -> None:
        cmd = hc.build_run_command("detailing", "cleveland", "medium")
        self.assertIn("--mode", cmd)
        self.assertIn("search-only", cmd)
        self.assertNotIn("score-confirmed", cmd)
        self.assertNotIn("--airtable", cmd)
        self.assertNotIn("--yes", cmd)

    def test_confirm_refuses_and_runs_nothing(self) -> None:
        with patch.object(hc, "run_streaming") as run:
            with self.assertRaises(hc.CommandError) as caught:
                hc.cmd_confirm("detailing_cleveland_medium_001", execute=True)
        run.assert_not_called()
        msg = str(caught.exception)
        self.assertIn("/confirm is deprecated", msg)
        self.assertIn("/score", msg)
        self.assertIn("/packets", msg)
        self.assertIn("/sync-dry", msg)
        self.assertIn("/sync-live", msg)

    def test_score_dry_uses_cost_only_flags(self) -> None:
        paths = BatchPaths.for_batch("detailing_cleveland_medium_001")
        cmd, env = hc.build_score_dry_command(paths, limit=25, budget=2.0, batch_size=3, max_tokens=4096)
        self.assertIn("--dry-run-llm-cost", cmd)
        self.assertNotIn("--confirm-llm-spend", cmd)
        self.assertNotIn("--fresh-scores-dir", cmd)
        self.assertEqual(env["MAX_LLM_USD_PER_RUN"], "2.00")
        self.assertIn("--llm-batch-size", cmd)
        self.assertIn("3", cmd)
        self.assertIn("--llm-max-tokens", cmd)
        self.assertIn("4096", cmd)

    def test_score_requires_limit(self) -> None:
        with self.assertRaises(hc.CommandError):
            hc.cmd_score("detailing_cleveland_medium_001", limit=None)

    def test_score_includes_paid_safety_flags_and_env(self) -> None:
        paths = BatchPaths.for_batch("detailing_cleveland_medium_001")
        cmd, env = hc.build_score_command(
            paths, limit=15, budget=2.0, batch_size=3, max_tokens=4096
        )
        self.assertIn("--resume-scored", cmd)
        self.assertIn("--confirm-llm-spend", cmd)
        self.assertIn("--fresh-scores-dir", cmd)
        self.assertIn("--llm-batch-size", cmd)
        self.assertIn("3", cmd)
        self.assertIn("--llm-max-tokens", cmd)
        self.assertIn("4096", cmd)
        self.assertEqual(env["MAX_LLM_USD_PER_RUN"], "2.00")
        self.assertIn("MAX_LLM_CALLS_PER_RUN", env)
        self.assertIn("MAX_LLM_TOKENS_PER_RUN", env)

    def test_no_command_except_sync_live_builds_live_flag(self) -> None:
        paths = BatchPaths.for_batch("detailing_cleveland_medium_001")
        commands = [
            hc.build_run_command("detailing", "cleveland", "medium"),
            hc.build_score_dry_command(paths, limit=25, budget=2, batch_size=3, max_tokens=4096)[0],
            hc.build_score_command(paths, limit=15, budget=2, batch_size=3, max_tokens=4096)[0],
            hc.build_refresh_command(paths),
            hc.build_packets_command(paths),
            hc.build_sync_command(paths, include_manual=False, live=False),
        ]
        for cmd in commands:
            self.assertNotIn("--live", cmd)
        self.assertIn("--live", hc.build_sync_command(paths, include_manual=False, live=True))

    def test_sync_dry_builds_dry_run_and_manual_flag(self) -> None:
        paths = BatchPaths.for_batch("detailing_cleveland_medium_001")
        sendable = hc.build_sync_command(paths, include_manual=False, live=False)
        manual = hc.build_sync_command(paths, include_manual=True, live=False)
        self.assertIn("--dry-run", sendable)
        self.assertIn("--only-sendable", sendable)
        self.assertNotIn("--include-manual-review", sendable)
        self.assertIn("--dry-run", manual)
        self.assertIn("--include-manual-review", manual)


class HermesBatchStateTest(unittest.TestCase):
    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_batch_resolution_paths(self) -> None:
        paths = resolve_batch_paths("detailing_cleveland_medium_001")
        self.assertEqual(paths.vertical, "detailing")
        self.assertEqual(paths.market, "cleveland")
        self.assertEqual(paths.size, "medium")
        self.assertEqual(paths.scored_csv, Path("output/scored_detailing_cleveland_medium_001_api.csv"))
        self.assertEqual(paths.tiered_csv, Path("output/scored_detailing_cleveland_medium_001_api_tiered.csv"))
        self.assertEqual(paths.packets_dir, Path("packets_detailing_cleveland_medium_001"))

    def test_default_repo_root_prefers_home_local_lead_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = home / "local-lead-machine"
            make_repo_root(expected)
            resolved = hc.resolve_repo_root(env={}, cwd=home / "elsewhere", home=home)
            self.assertEqual(resolved, expected.resolve())

    def test_repo_root_env_and_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env_root = base / "env-root"
            cli_root = base / "cli-root"
            make_repo_root(env_root)
            make_repo_root(cli_root)
            self.assertEqual(
                hc.resolve_repo_root(env={"LOCAL_LEAD_MACHINE_ROOT": str(env_root)}, cwd=base, home=base),
                env_root.resolve(),
            )
            self.assertEqual(
                hc.resolve_repo_root(cli_root, env={"LOCAL_LEAD_MACHINE_ROOT": str(env_root)}, cwd=base, home=base),
                cli_root.resolve(),
            )

    def test_repo_root_falls_back_to_pipeline_cwd_when_default_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "hermes"
            cwd.mkdir()
            make_repo_root(cwd)
            self.assertEqual(hc.resolve_repo_root(env={}, cwd=cwd, home=Path(tmp)), cwd.resolve())

    def test_python_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("# python\n", encoding="utf-8")
            cli_python = root / "cli-python"
            cli_python.write_text("# python\n", encoding="utf-8")
            env_python = root / "env-python"
            env_python.write_text("# python\n", encoding="utf-8")
            self.assertEqual(hc.resolve_python(cli_python, repo_root=root, env={}), cli_python.resolve())
            self.assertEqual(
                hc.resolve_python(None, repo_root=root, env={"LOCAL_LEAD_MACHINE_PYTHON": str(env_python)}),
                env_python.resolve(),
            )
            self.assertEqual(hc.resolve_python(None, repo_root=root, env={}), venv_python.resolve())

    def test_batch_resolution_uses_specified_repo_root_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = resolve_batch_paths("detailing_cleveland_medium_001", root=root)
            self.assertEqual(paths.config_path, root / "search_configs" / "detailing_cleveland_medium_001.json")
            self.assertEqual(paths.enriched, root / "output" / "enriched_detailing_cleveland_medium_001.jsonl")
            self.assertEqual(paths.scored_csv, root / "output" / "scored_detailing_cleveland_medium_001_api.csv")
            self.assertEqual(paths.tiered_csv, root / "output" / "scored_detailing_cleveland_medium_001_api_tiered.csv")
            self.assertEqual(paths.packet_index, root / "packets_detailing_cleveland_medium_001" / "packet_index.csv")
            self.assertEqual(paths.report_path, root / "reports" / "search_detailing_cleveland_medium_001.md")

    def test_status_counts_artifacts_from_repo_root_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pipeline"
            cwd = Path(tmp) / "hermes"
            cwd.mkdir()
            make_repo_root(root)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.enriched.write_text("{}\n{}\n", encoding="utf-8")
            self._write_csv(paths.scored_csv, [
                {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
            ])
            self._write_csv(paths.tiered_csv, [
                {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
            ])
            paths.packets_dir.mkdir()
            paths.packet_index.write_text(
                "Business Name,Packet Tier\nA,Sendable Packet\nB,Manual Review Packet\n",
                encoding="utf-8",
            )
            with patch.object(hc.Path, "cwd", return_value=cwd):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = hc.main(["--repo-root", str(root), "status", paths.batch])
            self.assertEqual(rc, 0)
            self.assertIn("enriched 2", buf.getvalue())
            self.assertIn("packets 2", buf.getvalue())

    def test_packets_and_sync_dry_check_inputs_under_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pipeline"
            cwd = Path(tmp) / "hermes"
            cwd.mkdir()
            make_repo_root(root)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            self._write_csv(paths.scored_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            paths.packets_dir.mkdir()
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            with patch.object(hc.Path, "cwd", return_value=cwd):
                self.assertIn("preview /packets", hc.cmd_packets(paths.batch, root=root, python_path=Path("/py")).message)
                self.assertIn("preview /sync-dry", hc.cmd_sync_dry(paths.batch, root=root, python_path=Path("/py")).message)

    def test_sync_dry_keeps_large_airtable_plan_tail_for_counting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            paths.packets_dir.mkdir()
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            plan = [
                {"method": "PATCH", "record_id": "rec1"},
                {"method": "POST"},
                {"method": "SKIP", "warning": "ambiguous"},
            ]
            lines = ["[DRY-RUN] Records: 3", json.dumps(plan)]
            with patch.object(hc, "run_streaming", return_value=StreamResult(0, False, lines)) as run:
                msg = hc.cmd_sync_dry(paths.batch, execute=True, root=root, python_path=Path("/py")).message
            self.assertIn("POST 1 PATCH 1 SKIP 1", msg)
            self.assertEqual(run.call_args.kwargs["tail_limit"], 5000)

    def test_run_state_is_written_under_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.packets_dir.mkdir()
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            hc.record_dry_run(paths, include_manual=False, post=1, patch=0, skip=0, warnings=[])
            self.assertTrue((root / "run_state" / "detailing_cleveland_medium_001.json").exists())

    def test_sync_live_refuses_without_fresh_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.packets_dir.mkdir(parents=True)
            paths.packet_index.write_text("Business Name,Packet Tier\n", encoding="utf-8")
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            with self.assertRaises(hc.CommandError):
                hc.cmd_sync_live(paths.batch, root=root)

    def test_sync_live_refuses_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.packets_dir.mkdir(parents=True)
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            hc.record_dry_run(paths, include_manual=False, post=1, patch=0, skip=0, warnings=[], root=root)
            paths.packet_index.write_text("Business Name,Packet Tier\nB,Sendable Packet\n", encoding="utf-8")
            with self.assertRaises(hc.CommandError):
                hc.cmd_sync_live(paths.batch, root=root)

    def test_sync_live_refuses_stale_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.packets_dir.mkdir(parents=True)
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            hc.record_dry_run(paths, include_manual=False, post=1, patch=0, skip=0, warnings=[], root=root)
            stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            data = json.loads(paths.state_json.read_text(encoding="utf-8"))
            data["last_airtable_dry_run_timestamp"] = stale
            paths.state_json.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(hc.CommandError):
                hc.cmd_sync_live(paths.batch, root=root)

    def test_sync_live_refuses_warnings_and_include_manual_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.packets_dir.mkdir(parents=True)
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            hc.record_dry_run(paths, include_manual=False, post=1, patch=0, skip=0, warnings=["ambiguous"], root=root)
            with self.assertRaises(hc.CommandError):
                hc.cmd_sync_live(paths.batch, root=root)
            hc.record_dry_run(paths, include_manual=False, post=1, patch=0, skip=0, warnings=[], root=root)
            with self.assertRaises(hc.CommandError):
                hc.cmd_sync_live(paths.batch, include_manual=True, root=root)

    def test_status_summarizes_fixture_files_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.enriched.parent.mkdir(parents=True, exist_ok=True)
            paths.enriched.write_text("{}\n{}\n", encoding="utf-8")
            self._write_csv(paths.scored_csv, [
                {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
            ])
            self._write_csv(paths.tiered_csv, [
                {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
            ])
            paths.packets_dir.mkdir(parents=True)
            paths.packet_index.write_text(
                "Business Name,Packet Tier\nA,Sendable Packet\nB,Manual Review Packet\n",
                encoding="utf-8",
            )
            hc.record_dry_run(paths, include_manual=True, post=1, patch=1, skip=0, warnings=[], root=root)
            msg = hc.cmd_status(paths.batch, root=root).message
            self.assertIn("enriched 2", msg)
            self.assertIn("scored 2", msg)
            self.assertIn("unscored 0", msg)
            self.assertIn("packets 2", msg)
            self.assertIn("sendable 1", msg)
            self.assertIn("manual 1", msg)
            self.assertIn("dry-run POST 1 PATCH 1 SKIP 0", msg)

    def test_current_sync_hash_changes_with_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.packets_dir.mkdir(parents=True)
            paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
            self._write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
            first = current_sync_hash(paths, include_manual=False)
            paths.packet_index.write_text("Business Name,Packet Tier\nB,Sendable Packet\n", encoding="utf-8")
            second = current_sync_hash(paths, include_manual=False)
            self.assertNotEqual(first, second)


class HermesExecutorTest(unittest.TestCase):
    def test_executor_streams_lines_and_preserves_return_code(self) -> None:
        from lead_machine.hermes_executor import run_streaming

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = run_streaming(
                ["python", "-c", "print('one'); print('two')"],
                timeout_seconds=5,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("[RUN] python -c", buf.getvalue())
        self.assertIn("one", buf.getvalue())
        self.assertIn("two", buf.getvalue())

    def test_executor_prints_repo_root_and_runs_with_cwd(self) -> None:
        from lead_machine.hermes_executor import run_streaming

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = run_streaming(
                    ["python", "-c", "import os; print(os.getcwd())"],
                    timeout_seconds=5,
                    cwd=root,
                )
            self.assertEqual(result.returncode, 0)
            self.assertIn(f"repo_root: {root}", buf.getvalue())
            self.assertIn(str(root), "\n".join(result.tail_lines))

    def test_previews_display_repo_root_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = BatchPaths.for_batch("detailing_cleveland_medium_001", root=root)
            paths.enriched.write_text("{}\n", encoding="utf-8")
            msg = hc.cmd_score_dry(paths.batch, root=root, python_path=Path("/repo/.venv/bin/python")).message
            self.assertIn(f"repo_root: {root}", msg)
            self.assertIn("cd ", msg)
            self.assertIn("/repo/.venv/bin/python", msg)

    def test_executor_handles_nonzero_exit(self) -> None:
        from lead_machine.hermes_executor import run_streaming

        result = run_streaming(["python", "-c", "print('bad'); raise SystemExit(7)"], timeout_seconds=5)
        self.assertEqual(result.returncode, 7)
        self.assertIn("bad", "\n".join(result.tail_lines))

    def test_executor_handles_timeout(self) -> None:
        from lead_machine.hermes_executor import run_streaming

        result = run_streaming(
            ["python", "-c", "import time; print('start', flush=True); time.sleep(5)"],
            timeout_seconds=0.2,
            resume_hint="/score demo --limit 3",
        )
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("start", "\n".join(result.tail_lines))


if __name__ == "__main__":
    unittest.main()
