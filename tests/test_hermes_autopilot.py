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
from lead_machine.hermes_batch import BatchPaths
from lead_machine.hermes_executor import StreamResult
from lead_machine.hermes_state import load_run_state, update_run_state, utc_now


def make_repo_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in (
        "run_agent_pipeline.py",
        "score_leads.py",
        "refresh_packet_tiers.py",
        "make_packets.py",
        "upload_airtable.py",
    ):
        (root / name).write_text("# fixture\n", encoding="utf-8")
    for directory in (
        "output",
        "reports",
        "search_configs",
        "vertical_profiles",
        "market_profiles",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "vertical_profiles" / "epoxy.json").write_text("{}", encoding="utf-8")
    (root / "market_profiles" / "cleveland.json").write_text("{}", encoding="utf-8")
    (root / "search_configs" / "epoxy_cleveland_medium_001.json").write_text("{}", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_cli(root: Path, argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = hc.main(["--repo-root", str(root), *argv])
    return rc, buf.getvalue()


class HermesAutopilotTest(unittest.TestCase):
    def _seed_scoring_approval(self, root: Path, *, include_manual: bool = False) -> BatchPaths:
        paths = BatchPaths.for_batch("epoxy_cleveland_medium_001", root=root)
        paths.enriched.parent.mkdir(parents=True, exist_ok=True)
        paths.enriched.write_text("{}\n{}\n", encoding="utf-8")
        hc.create_pending_approval(
            paths,
            stage="score",
            original_command="/run epoxy cleveland medium --auto --score-budget 5",
            next_command="/score-rest epoxy_cleveland_medium_001 --budget 5.00",
            include_manual=include_manual,
            score_budget=5.0,
            projected_score_cost=0.87,
        )
        return paths

    def _seed_live_approval(self, root: Path, *, include_manual: bool = False) -> BatchPaths:
        paths = BatchPaths.for_batch("epoxy_cleveland_medium_001", root=root)
        paths.enriched.parent.mkdir(parents=True, exist_ok=True)
        paths.enriched.write_text("{}\n{}\n", encoding="utf-8")
        write_csv(
            paths.scored_csv,
            [
                {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
            ],
        )
        write_csv(
            paths.tiered_csv,
            [
                {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
            ],
        )
        paths.packets_dir.mkdir(parents=True, exist_ok=True)
        paths.packet_index.write_text(
            "Business Name,Packet Tier\nA,Sendable Packet\nB,Manual Review Packet\n",
            encoding="utf-8",
        )
        hc.record_dry_run(paths, include_manual=include_manual, post=1, patch=1, skip=0, warnings=[], root=root)
        hc.create_pending_approval(
            paths,
            stage="live",
            original_command="/run epoxy cleveland medium --auto --score-budget 5",
            next_command="/sync-live epoxy_cleveland_medium_001",
            include_manual=include_manual,
            score_budget=5.0,
            dry_run_hash=load_run_state(paths)["dry_run_input_hash"],
        )
        return paths

    def test_run_auto_preview_starts_plan_without_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            with patch.object(hc, "run_streaming") as run:
                rc, out = run_cli(
                    root,
                    ["run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--include-manual"],
                )
        self.assertEqual(rc, 0)
        self.assertIn("preview /run --auto", out)
        self.assertIn("/run epoxy cleveland medium --auto --score-budget 5.00 --include-manual --execute", out)
        run.assert_not_called()

    def test_telegram_run_slash_auto_path_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            rc, out = run_cli(
                root,
                ["run", "/run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--include-manual"],
            )
        self.assertEqual(rc, 0)
        self.assertIn("preview /run --auto", out)

    def test_autopilot_runs_search_status_score_dry_then_pending_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = BatchPaths.for_batch("epoxy_cleveland_medium_001", root=root)

            def fake_run(cmd: list[str], **_: object) -> StreamResult:
                if "run_agent_pipeline.py" in cmd:
                    paths.enriched.write_text("{}\n{}\n{}\n", encoding="utf-8")
                    return StreamResult(0, False, ["[PIPELINE] batch=epoxy_cleveland_medium_001"])
                if "score_leads.py" in cmd and "--dry-run-llm-cost" in cmd:
                    return StreamResult(0, False, ["projected_usd=0.87", "selected=25"])
                raise AssertionError(cmd)

            with patch.object(hc, "run_streaming", side_effect=fake_run) as run:
                rc, out = run_cli(
                    root,
                    ["run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--execute"],
                )

            self.assertEqual(rc, 0)
            self.assertEqual(run.call_count, 2)
            self.assertIn("Scoring dry-run complete for epoxy_cleveland_medium_001", out)
            self.assertIn("Continue paid scoring? Reply yes/no.", out)
            state = load_run_state(paths)
            self.assertEqual(state["pending_approval"]["stage"], "score")
            self.assertEqual(state["pending_approval"]["projected_score_cost"], 0.87)

    def test_yes_to_score_runs_score_rest_packets_sync_dry_and_pending_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_scoring_approval(root, include_manual=True)

            def fake_run(cmd: list[str], **_: object) -> StreamResult:
                if "score_leads.py" in cmd and "--confirm-llm-spend" in cmd:
                    write_csv(
                        paths.scored_csv,
                        [
                            {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                            {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
                        ],
                    )
                    paths.usage_json.write_text('{"estimated_usd": 0.42}\n', encoding="utf-8")
                    return StreamResult(0, False, ["scored 2"])
                if "refresh_packet_tiers.py" in cmd:
                    write_csv(
                        paths.tiered_csv,
                        [
                            {"Business Name": "A", "Packet Tier": "Sendable Packet"},
                            {"Business Name": "B", "Packet Tier": "Manual Review Packet"},
                        ],
                    )
                    return StreamResult(0, False, ["refreshed"])
                if "make_packets.py" in cmd:
                    paths.packets_dir.mkdir(parents=True, exist_ok=True)
                    paths.packet_index.write_text(
                        "Business Name,Packet Tier\nA,Sendable Packet\nB,Manual Review Packet\n",
                        encoding="utf-8",
                    )
                    return StreamResult(0, False, ["packets"])
                if "upload_airtable.py" in cmd and "--dry-run" in cmd:
                    self.assertIn("--include-manual-review", cmd)
                    return StreamResult(
                        0,
                        False,
                        ['[{"method":"POST"},{"method":"PATCH"}]'],
                    )
                raise AssertionError(cmd)

            with patch.object(hc, "run_streaming", side_effect=fake_run) as run:
                rc, out = run_cli(root, ["yes"])

            self.assertEqual(rc, 0)
            self.assertEqual(run.call_count, 4)
            self.assertIn("Airtable dry-run complete for epoxy_cleveland_medium_001", out)
            self.assertIn("Live sync now? Reply yes/no.", out)
            state = load_run_state(paths)
            self.assertEqual(state["pending_approval"]["stage"], "live")
            self.assertTrue(state["pending_approval"]["include_manual"])

    def test_no_cancels_pending_approval_and_prints_resume_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_scoring_approval(root)
            rc, out = run_cli(root, ["no"])
        self.assertEqual(rc, 0)
        self.assertIn("Autopilot stopped.", out)
        self.assertIn("Resume with:", out)
        self.assertIsNone(load_run_state(paths).get("pending_approval"))

    def test_expired_approval_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_scoring_approval(root)
            state = load_run_state(paths)
            state["pending_approval"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            update_run_state(paths, pending_approval=state["pending_approval"])
            rc, out = run_cli(root, ["yes"])
        self.assertEqual(rc, 2)
        self.assertIn("Approval expired", out)

    def test_wrong_stage_approval_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_scoring_approval(root)
            state = load_run_state(paths)
            state["pending_approval"]["stage"] = "unknown"
            update_run_state(paths, pending_approval=state["pending_approval"])
            rc, out = run_cli(root, ["yes"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown approval stage", out)

    def test_score_dry_over_budget_stops_before_paid_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = BatchPaths.for_batch("epoxy_cleveland_medium_001", root=root)

            def fake_run(cmd: list[str], **_: object) -> StreamResult:
                if "run_agent_pipeline.py" in cmd:
                    paths.enriched.write_text("{}\n", encoding="utf-8")
                    return StreamResult(0, False, ["[PIPELINE] batch=epoxy_cleveland_medium_001"])
                if "score_leads.py" in cmd and "--dry-run-llm-cost" in cmd:
                    return StreamResult(0, False, ["projected_usd=7.25"])
                raise AssertionError(cmd)

            with patch.object(hc, "run_streaming", side_effect=fake_run) as run:
                rc, out = run_cli(
                    root,
                    ["run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--execute"],
                )

            self.assertEqual(rc, 0)
            self.assertEqual(run.call_count, 2)
            self.assertIn("Projected cost $7.25 exceeds budget $5.00", out)
            self.assertIsNone(load_run_state(paths).get("pending_approval"))

    def test_billing_error_stops_autopilot_and_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_scoring_approval(root)
            with patch.object(
                hc,
                "run_streaming",
                return_value=StreamResult(1, False, ["credit balance too low"]),
            ):
                rc, out = run_cli(root, ["yes"])
            self.assertEqual(rc, 0)
            self.assertIn("Anthropic credit/billing error. Autopilot stopped.", out)
            self.assertIn("Top up credits, then resume with:", out)
            self.assertIn("credit/billing", load_run_state(paths)["last_error"])

    def test_clean_sync_dry_creates_pending_live_and_warnings_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_scoring_approval(root)
            write_csv(paths.scored_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])

            def fake_clean(cmd: list[str], **_: object) -> StreamResult:
                if "score_leads.py" in cmd:
                    paths.usage_json.write_text('{"estimated_usd": 0.01}\n', encoding="utf-8")
                    return StreamResult(0, False, ["no new scored rows"])
                if "refresh_packet_tiers.py" in cmd:
                    write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
                    return StreamResult(0, False, ["refreshed"])
                if "make_packets.py" in cmd:
                    paths.packets_dir.mkdir(parents=True, exist_ok=True)
                    paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
                    return StreamResult(0, False, ["packets"])
                if "upload_airtable.py" in cmd:
                    return StreamResult(0, False, ['[{"method":"POST"}]'])
                raise AssertionError(cmd)

            with patch.object(hc, "run_streaming", side_effect=fake_clean):
                rc, out = run_cli(root, ["yes"])
            self.assertEqual(rc, 0)
            self.assertIn("Live sync now? Reply yes/no.", out)
            self.assertEqual(load_run_state(paths)["pending_approval"]["stage"], "live")

            hc.create_pending_approval(
                paths,
                stage="score",
                original_command="/run epoxy cleveland medium --auto",
                next_command="/score-rest epoxy_cleveland_medium_001",
                include_manual=False,
                score_budget=5.0,
            )

            def fake_warn(cmd: list[str], **_: object) -> StreamResult:
                if "score_leads.py" in cmd:
                    return StreamResult(0, False, ["no new scored rows"])
                if "refresh_packet_tiers.py" in cmd:
                    return StreamResult(0, False, ["refreshed"])
                if "make_packets.py" in cmd:
                    return StreamResult(0, False, ["packets"])
                if "upload_airtable.py" in cmd:
                    return StreamResult(0, False, ["[WARN] ambiguous", '[{"method":"SKIP","warning":"ambiguous"}]'])
                raise AssertionError(cmd)

            with patch.object(hc, "run_streaming", side_effect=fake_warn):
                rc, out = run_cli(root, ["yes"])
            self.assertEqual(rc, 0)
            self.assertIn("Live sync refused", out)
            self.assertIsNone(load_run_state(paths).get("pending_approval"))

    def test_yes_to_live_runs_sync_live_only_when_hash_matches_and_preserves_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_live_approval(root, include_manual=True)

            with patch.object(
                hc,
                "run_streaming",
                return_value=StreamResult(0, False, ['[{"method":"POST"},{"method":"PATCH"}]']),
            ) as run:
                rc, out = run_cli(root, ["yes"])
            self.assertEqual(rc, 0)
            self.assertIn("Live sync complete", out)
            self.assertIn("--live", run.call_args.args[0])
            self.assertIn("--include-manual-review", run.call_args.args[0])

            paths = self._seed_live_approval(root, include_manual=True)
            paths.packet_index.write_text("Business Name,Packet Tier\nchanged,Sendable Packet\n", encoding="utf-8")
            with patch.object(hc, "run_streaming") as run2:
                rc, out = run_cli(root, ["yes"])
            self.assertEqual(rc, 2)
            self.assertIn("hash no longer matches", out)
            run2.assert_not_called()

    def test_no_to_live_cancels_and_prints_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = self._seed_live_approval(root)
            rc, out = run_cli(root, ["no"])
        self.assertEqual(rc, 0)
        self.assertIn("Autopilot stopped.", out)
        self.assertIn("/sync-live epoxy_cleveland_medium_001", out)
        self.assertIsNone(load_run_state(paths).get("pending_approval"))

    def test_yes_score_yes_live_and_yes_all_cli_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo_root(root)
            paths = BatchPaths.for_batch("epoxy_cleveland_medium_001", root=root)

            def fake_run(cmd: list[str], **_: object) -> StreamResult:
                if "run_agent_pipeline.py" in cmd:
                    paths.enriched.write_text("{}\n", encoding="utf-8")
                    return StreamResult(0, False, ["[PIPELINE] batch=epoxy_cleveland_medium_001"])
                if "score_leads.py" in cmd and "--dry-run-llm-cost" in cmd:
                    return StreamResult(0, False, ["projected_usd=0.50"])
                if "score_leads.py" in cmd and "--confirm-llm-spend" in cmd:
                    write_csv(paths.scored_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
                    paths.usage_json.write_text('{"estimated_usd": 0.50}\n', encoding="utf-8")
                    return StreamResult(0, False, ["scored"])
                if "refresh_packet_tiers.py" in cmd:
                    write_csv(paths.tiered_csv, [{"Business Name": "A", "Packet Tier": "Sendable Packet"}])
                    return StreamResult(0, False, ["refreshed"])
                if "make_packets.py" in cmd:
                    paths.packets_dir.mkdir(parents=True, exist_ok=True)
                    paths.packet_index.write_text("Business Name,Packet Tier\nA,Sendable Packet\n", encoding="utf-8")
                    return StreamResult(0, False, ["packets"])
                if "upload_airtable.py" in cmd:
                    return StreamResult(0, False, ['[{"method":"POST"}]'])
                raise AssertionError(cmd)

            with patch.object(hc, "run_streaming", side_effect=fake_run) as run:
                rc, out = run_cli(
                    root,
                    ["run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--yes-score", "--execute"],
                )
            self.assertEqual(rc, 0)
            self.assertIn("Live sync now? Reply yes/no.", out)
            self.assertNotIn("--live", [arg for call in run.call_args_list for arg in call.args[0]])

            with patch.object(hc, "run_streaming", side_effect=fake_run) as run:
                rc, out = run_cli(
                    root,
                    ["run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--yes-all", "--execute"],
                )
        self.assertEqual(rc, 0)
        self.assertIn("Live sync complete", out)
        self.assertTrue(any("--live" in call.args[0] for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
