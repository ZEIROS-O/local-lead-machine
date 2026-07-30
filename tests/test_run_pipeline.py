from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import places_search
import run_pipeline
from lead_machine.review_velocity import ReviewSnapshotStore


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


class ReviewSignalStageTest(unittest.TestCase):
    """`collect_review_signals.py` is paid, and it attaches at both ends of scoring.

    `submit` selects with `select_deep_enrichment`, which ranks on Scoring V3
    Disposition/Rank — columns that exist only in the scored CSV. `merge` writes
    `review_items`/`review_collection` onto the enriched record, which is what
    `scoring_v3` reads. So merge belongs before score_leads and submit after it,
    and getting that backwards buys DataForSEO evidence no scored row ever sees.
    These tests pin the ordering and the default-off-ness rather than the wording
    of the commands.
    """

    def _write_enriched(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"google": {"business_name": "Lead A", "place_id": "place-1"}, "evidence": {}}) + "\n",
            encoding="utf-8",
        )

    def _write_scored(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Business Name", "Lead Priority", "Sendable", "Packet Eligible"])
            writer.writeheader()
            writer.writerow(
                {"Business Name": "Lead A", "Lead Priority": "A", "Sendable": "Yes", "Packet Eligible": "Yes"}
            )

    def _write_receipt(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "submitted_count": 1,
                    "estimated_usd": 0.20,
                    "cost_usd": 0.17,
                    "tasks": [{"id": "task-1"}],
                }
            ),
            encoding="utf-8",
        )

    def _run(
        self,
        root: Path,
        extra: list[str],
        *,
        review_state: str = "complete",
    ) -> tuple[int, list[list[str]], str]:
        old_cwd = Path.cwd()
        os.chdir(root)
        commands: list[list[str]] = []

        def fake_run(cmd: list[str]) -> None:
            commands.append(cmd)
            if cmd[1:3] == ["collect_review_signals.py", "collect"]:
                output = Path(cmd[cmd.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                signal = {
                    "task_id": "task-1",
                    "state": review_state,
                    "place_id": "place-1",
                    "tag": "place-1",
                    "items": [{"review_id": "review-1"}] if review_state == "complete" else [],
                    "cost_usd": 0.17 if review_state == "complete" else 0.0,
                    "source": "dataforseo_google_reviews_newest",
                }
                output.write_text(json.dumps(signal) + "\n", encoding="utf-8")
                summary_output = Path(cmd[cmd.index("--summary-output") + 1])
                summary_output.write_text(
                    json.dumps(
                        {
                            "status": review_state,
                            "total": 1,
                            "complete": 1 if review_state == "complete" else 0,
                            "pending": 0 if review_state == "complete" else 1,
                            "cost_usd": signal["cost_usd"],
                        }
                    ),
                    encoding="utf-8",
                )
            if cmd[1:3] == ["collect_review_signals.py", "merge"]:
                summary_output = Path(cmd[cmd.index("--summary-output") + 1])
                summary_output.write_text(
                    json.dumps(
                        {
                            "records": 1,
                            "complete_signals": 1,
                            "attached": 1,
                        }
                    ),
                    encoding="utf-8",
                )
            if "score_leads.py" in cmd:
                self._write_scored(Path("output/scored_test.csv"))

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
                "--skip-packets",
                "--skip-airtable",
                *extra,
            ]
            with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=fake_run), redirect_stdout(stdout):
                code = run_pipeline.main()
        finally:
            os.chdir(old_cwd)
        return code, commands, stdout.getvalue()

    @staticmethod
    def _script_order(commands: list[list[str]]) -> list[str]:
        order: list[str] = []
        for cmd in commands:
            script = next((part for part in cmd if part.endswith(".py")), "")
            subcommand = (
                cmd[2]
                if script in {"collect_review_signals.py", "collect_external_signals.py"}
                else ""
            )
            order.append(f"{script} {subcommand}".strip())
        return order

    def test_review_collection_costs_nothing_unless_it_is_asked_for(self) -> None:
        """The whole reason these are opt-in instead of --skip-*: every batch
        command already in use has to keep billing exactly what it bills today."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            self._write_receipt(root / "output/review_tasks_test.json")
            code, commands, _ = self._run(root, [])

        self.assertEqual(code, 0)
        self.assertNotIn("collect_review_signals.py", [part for cmd in commands for part in cmd])

    def test_merge_lands_before_scoring_reads_the_enriched_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            enriched = root / "output/enriched_test.jsonl"
            self._write_enriched(enriched)
            self._write_receipt(root / "output/review_tasks_test.json")
            code, commands, _ = self._run(root, ["--merge-review-signals"])

        order = self._script_order(commands)
        self.assertEqual(code, 0)
        self.assertEqual(
            order[:3],
            [
                "collect_review_signals.py collect",
                "collect_review_signals.py merge",
                "score_leads.py",
            ],
        )
        merge_cmd = next(cmd for cmd in commands if cmd[1:3] == ["collect_review_signals.py", "merge"])
        score_cmd = next(cmd for cmd in commands if "score_leads.py" in cmd)
        # The merge has to write back over the file scoring reads, or the evidence
        # is stranded in a side artifact nothing opens.
        self.assertEqual(merge_cmd[merge_cmd.index("--output") + 1], "output/enriched_test.jsonl")
        self.assertEqual(score_cmd[score_cmd.index("--input") + 1], "output/enriched_test.jsonl")

    def test_pending_review_collection_refuses_to_score_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            self._write_receipt(root / "output/review_tasks_test.json")
            with self.assertRaises(SystemExit) as raised:
                self._run(
                    root,
                    ["--merge-review-signals"],
                    review_state="pending",
                )
            summary = json.loads(
                (root / "output/run_summary_test.json").read_text(encoding="utf-8")
            )

        self.assertIn("review tasks are still pending", str(raised.exception).lower())
        self.assertEqual(summary["review_signals"]["status"], "pending")
        self.assertEqual(summary["review_signals"]["pending"], 1)
        self.assertEqual(summary["review_signals"]["estimated_usd"], 0.20)

    def test_submit_lands_after_scoring_and_reads_the_scored_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            code, commands, _ = self._run(root, ["--submit-review-signals"])

        order = self._script_order(commands)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["score_leads.py", "collect_review_signals.py submit"])
        submit_cmd = commands[-1]
        self.assertEqual(submit_cmd[submit_cmd.index("--input") + 1], "output/scored_test.csv")
        self.assertEqual(submit_cmd[submit_cmd.index("--output") + 1], "output/review_tasks_test.json")

    def test_merge_refuses_rather_than_collecting_against_no_receipt(self) -> None:
        """`collect` with no receipt is not a harmless no-op — it silently returns
        an empty signal file, and the merge then reports success having attached
        nothing, which reads as "the evidence just wasn't there"."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            with self.assertRaises(SystemExit) as raised:
                self._run(root, ["--merge-review-signals"])

        self.assertIn("Review task receipt not found", str(raised.exception))

    def test_submit_holds_while_manual_scores_are_still_outstanding(self) -> None:
        """In manual-llm mode the scored CSV does not exist yet, so a submit here
        would either crash or bill against a stale CSV from an earlier batch."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            code, commands, stdout = self._run(
                root, ["--mode", "manual-llm", "--submit-review-signals"]
            )

        self.assertEqual(code, 0)
        self.assertNotIn("submit", [part for cmd in commands for part in cmd])
        self.assertIn("[SKIP] collect_review_signals.py submit", stdout)

    def test_summary_records_which_halves_of_the_loop_ran(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            self._write_receipt(root / "output/review_tasks_test.json")
            old_cwd = Path.cwd()
            try:
                self._run(root, ["--merge-review-signals", "--submit-review-signals"])
                os.chdir(root)
                summary = json.loads(Path("output/run_summary_test.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(summary["review_signals"]["status"], "merged+submitted")
        self.assertEqual(summary["review_signals"]["receipt"], "output/review_tasks_test.json")
        self.assertEqual(summary["review_signals"]["signals"], "output/review_signals_test.jsonl")
        self.assertEqual(summary["review_signals"]["complete"], 1)
        self.assertEqual(summary["review_signals"]["pending"], 0)
        self.assertEqual(summary["review_signals"]["cost_usd"], 0.17)
        self.assertEqual(summary["review_signals"]["estimated_usd"], 0.20)

    def test_external_signal_artifacts_merge_before_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_enriched(root / "output/enriched_test.jsonl")
            signal = root / "signals/pagespeed.json"
            signal.parent.mkdir(parents=True)
            signal.write_text(
                json.dumps(
                    {
                        "url": "https://lead.example",
                        "observation": {
                            "signal_id": "pagespeed:mobile:https://lead.example",
                            "kind": "website_performance",
                            "state": "present",
                            "cost_usd": 0.03,
                        },
                    }
                ),
                encoding="utf-8",
            )
            code, commands, _ = self._run(
                root,
                ["--external-signal", "signals/pagespeed.json"],
            )

        order = self._script_order(commands)
        self.assertEqual(code, 0)
        self.assertEqual(order[:2], ["collect_external_signals.py merge", "score_leads.py"])
        merge = commands[0]
        self.assertEqual(merge[merge.index("--signal") + 1], "signals/pagespeed.json")


class Stage1SuppressionGuardTest(unittest.TestCase):
    """Stage 1 is the pipeline's first spend, so it is where the refusal has to land.

    `make_search_batch.main` already fails closed, but `run_pipeline` does not
    call it — it shells out to `places_search.py`, which billed Google before
    anything had asked whether a suppression source existed. These live here
    rather than with the other places_search tests because the property under
    test is about the stage-1 command run_pipeline actually executes, and the
    first test pins that linkage so the rest cannot quietly stop covering it.
    """

    SUPPRESSED_PHONE_DIGITS = "2165550100"

    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"GOOGLE_PLACES_API_KEY": "test-key"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # main() calls load_dotenv(), which reads the developer's real .env back
        # in and re-supplies SUPPRESSION_SOURCE — the fail-closed branch would
        # then never run and this file would pass only on machines with no .env.
        # Stubbed the same way tests/test_dnc_enrichment.py does it.
        patcher = patch.object(places_search, "load_dotenv", lambda: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _searches_csv(self, root: Path) -> Path:
        path = root / "data/searches_test.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("query,max_results\nplumber Cleveland OH,2\n", encoding="utf-8")
        return path

    def _places_argv(self, root: Path) -> list[str]:
        return [
            "places_search.py",
            "--searches",
            str(self._searches_csv(root)),
            "--output",
            str(root / "output/raw_places_test.csv"),
            "--sleep",
            "0",
            "--review-snapshot-db",
            str(root / "run_state/review_velocity.sqlite3"),
        ]

    def test_stage_one_command_still_targets_places_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._searches_csv(root)
            old_cwd = Path.cwd()
            os.chdir(root)
            commands: list[list[str]] = []

            try:
                argv = [
                    "run_pipeline.py",
                    "--batch",
                    "test",
                    "--vertical",
                    "detailing",
                    "--skip-enrich",
                    "--skip-score",
                    "--skip-packets",
                    "--skip-airtable",
                ]
                with patch("sys.argv", argv), patch("run_pipeline.run", side_effect=commands.append), redirect_stdout(
                    io.StringIO()
                ):
                    code = run_pipeline.main()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0)
        search_cmd = commands[0]
        self.assertIn("places_search.py", search_cmd)
        self.assertNotIn("make_search_batch.py", [part for cmd in commands for part in cmd])

    def test_stage_one_refuses_before_any_paid_places_call_without_a_suppression_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = self._places_argv(root)
            with patch("sys.argv", argv), patch(
                "places_search.request_json", return_value={"places": []}
            ) as request_json:
                stderr = io.StringIO()
                with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                    code = places_search.main()

            self.assertEqual(code, 1)
            request_json.assert_not_called()
            self.assertIn("No suppression source configured", stderr.getvalue())
            self.assertFalse((root / "output/raw_places_test.csv").exists())

    def test_stage_one_searches_once_the_missing_index_is_explicitly_bypassed(self) -> None:
        """The guard has to be a gate, not a permanent stop: the documented
        ALLOW_MISSING_SUPPRESSION_INDEX escape hatch still has to let a run through."""
        os.environ["ALLOW_MISSING_SUPPRESSION_INDEX"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = self._places_argv(root)
            with patch("sys.argv", argv), patch(
                "places_search.request_json", return_value={"places": []}
            ) as request_json:
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    code = places_search.main()

            self.assertEqual(code, 0)
            request_json.assert_called_once()

    def test_stage_one_keeps_a_suppressed_lead_out_of_the_raw_csv(self) -> None:
        """A loaded index that nothing consults is ceremony. The raw CSV is what
        every later stage reads, so the opt-out has to bite before it is written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "outcomes.jsonl"
            source.write_text(
                json.dumps({"event": "opt_out", "phone": self.SUPPRESSED_PHONE_DIGITS}) + "\n",
                encoding="utf-8",
            )
            os.environ["SUPPRESSION_SOURCE"] = str(source)

            responses = [
                {"places": [{"id": "place-suppressed"}, {"id": "place-clean"}]},
                {
                    "id": "place-suppressed",
                    "displayName": {"text": "Opted Out Plumbing"},
                    "formattedAddress": "1 Main St, Cleveland, OH 44101, USA",
                    "nationalPhoneNumber": "(216) 555-0100",
                },
                {
                    "id": "place-clean",
                    "displayName": {"text": "Reachable Plumbing"},
                    "formattedAddress": "2 Main St, Cleveland, OH 44101, USA",
                    "nationalPhoneNumber": "(216) 555-0199",
                },
            ]

            argv = self._places_argv(root)
            with patch("sys.argv", argv), patch("places_search.request_json", side_effect=responses):
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    code = places_search.main()

            with (root / "output/raw_places_test.csv").open(newline="", encoding="utf-8") as f:
                names = [row["business_name"] for row in csv.DictReader(f)]

        self.assertEqual(code, 0)
        self.assertEqual(names, ["Reachable Plumbing"])

    def _suppressed_and_clean_responses(self) -> list[dict]:
        """Two places, the first opted out, both carrying a review count.

        `userRatingCount` matters: without it `lead.reviews` is empty and the
        review-velocity write is skipped for every lead, so a test asserting the
        suppressed one is not banked would pass on a bug.
        """
        return [
            {"places": [{"id": "place-suppressed"}, {"id": "place-clean"}]},
            {
                "id": "place-suppressed",
                "displayName": {"text": "Opted Out Plumbing"},
                "formattedAddress": "1 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0100",
                "rating": 4.6,
                "userRatingCount": 41,
            },
            {
                "id": "place-clean",
                "displayName": {"text": "Reachable Plumbing"},
                "formattedAddress": "2 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0199",
                "rating": 4.7,
                "userRatingCount": 52,
            },
        ]

    def _write_suppression_source(self, root: Path) -> None:
        source = root / "outcomes.jsonl"
        source.write_text(
            json.dumps({"event": "opt_out", "phone": self.SUPPRESSED_PHONE_DIGITS}) + "\n",
            encoding="utf-8",
        )
        os.environ["SUPPRESSION_SOURCE"] = str(source)

    def test_stage_one_banks_no_review_snapshot_for_a_suppressed_lead(self) -> None:
        """The drop has to land before the write, not after it.

        The suppression check used to run below `record_snapshot`, so an opted-out
        business was persisted into run_state/review_velocity.sqlite3 — longitudinal
        data on a company that asked us to stop — and only then dropped from the CSV.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_suppression_source(root)

            argv = self._places_argv(root)
            with patch("sys.argv", argv), patch(
                "places_search.request_json", side_effect=self._suppressed_and_clean_responses()
            ):
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    code = places_search.main()

            # Read the real store back rather than a mock: the defect was a row
            # on disk, so the assertion has to be about what is on disk.
            store = ReviewSnapshotStore(root / "run_state/review_velocity.sqlite3")
            suppressed_history = store.history("place-suppressed")
            clean_history = store.history("place-clean")

        self.assertEqual(code, 0)
        self.assertEqual(suppressed_history, [])
        self.assertEqual(len(clean_history), 1)

    def test_stage_one_still_counts_the_billed_details_call_for_a_suppressed_lead(self) -> None:
        """Dropping the lead does not un-bill the call it took to recognise it.

        Nothing in a Text Search page can be matched against the suppression index
        — the field mask returns no phone, and a Place ID is deliberately not a
        suppression identifier — so Details is unavoidable here. Since it is paid
        for, it has to keep counting against MAX_DETAILS_CALLS_PER_RUN; excusing
        suppressed leads from the counter would let a run bill past its own cap.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_suppression_source(root)
            os.environ["MAX_DETAILS_CALLS_PER_RUN"] = "1"

            argv = self._places_argv(root)
            with patch("sys.argv", argv), patch(
                "places_search.request_json", side_effect=self._suppressed_and_clean_responses()
            ) as request_json:
                stdout = io.StringIO()
                with redirect_stderr(io.StringIO()), redirect_stdout(stdout):
                    code = places_search.main()

            with (root / "output/raw_places_test.csv").open(newline="", encoding="utf-8") as f:
                names = [row["business_name"] for row in csv.DictReader(f)]

        self.assertEqual(code, 0)
        # One search + one Details. The cap stopped the loop before the second
        # place, so the suppressed lead consumed the budget it actually spent.
        self.assertEqual(request_json.call_count, 2)
        self.assertIn("[STOP] Hit MAX_DETAILS_CALLS_PER_RUN", stdout.getvalue())
        self.assertIn("[DONE] Details calls: 1", stdout.getvalue())
        self.assertEqual(names, [])


if __name__ == "__main__":
    unittest.main()
