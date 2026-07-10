from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lead_machine import hermes_queue as hq
from lead_machine.hermes_commands import CommandError, CommandResult


def make_root(tmp: str, *, config: dict | None = None) -> Path:
    root = Path(tmp)
    (root / "run_state").mkdir(parents=True, exist_ok=True)
    payload = {
        "target_queue_size": 5,
        "morning_budget_usd": 2.00,
        "queue_overflow_cap": 8,
        "topup_enabled": True,
        "topup_verticals": [
            {"vertical": "water_restoration", "market": "cleveland", "size": "medium"},
            {"vertical": "epoxy", "market": "cleveland", "size": "medium"},
        ],
        "outcomes_jsonl": str(root / "outcomes.jsonl"),
    }
    if config:
        payload.update(config)
    (root / "morning_queue.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def make_record(i: int, *, vertical: str = "water_restoration", priority: str = "A") -> dict:
    return {
        "record_id": f"rec{i:014d}"[:17],
        "Business Name": f"Biz {i}",
        "Vertical": vertical,
        "City": "Cleveland",
        "Phone": "(216) 555-0000",
        "Email": "",
        "Website URL": f"https://biz{i}.example",
        "Lead Priority": priority,
        "Score Total": 90 - i,
        "Best Service Angle": "Website Rebuild",
        "Offer Fit": "Website Rebuild",
        "Specific Observation": "obs",
        "Site Status": "Broken",
        "Opener": "stored opener",
        "Safe Outreach Claims": "claim A; claim B",
        "Mockup URL": "",
        "Video URL": "",
    }


def completed_run(message: str = "Live sync complete. ...\nFinal summary: batch water_restoration_cleveland_medium_002; actual LLM $0.42; ...") -> CommandResult:
    return CommandResult(message)


class QueueStatusTest(unittest.TestCase):
    def test_preview_does_not_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with patch.object(hq, "fetch_pooled_records") as fetch:
                res = hq.cmd_queue_status(root=root)
            fetch.assert_not_called()
            self.assertIn("preview /queue-status", res.message)

    def test_execute_reports_counts_and_deficit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(1), make_record(2, vertical="epoxy")]
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                res = hq.cmd_queue_status(execute=True, root=root)
            payload = json.loads(res.message)
            self.assertEqual(payload["pooled_sendable"], 2)
            self.assertEqual(payload["target"], 5)
            self.assertEqual(payload["deficit"], 3)
            self.assertEqual(payload["per_vertical"], {"water_restoration": 1, "epoxy": 1})


class QueueTopupTest(unittest.TestCase):
    def test_target_met_writes_ran_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(i) for i in range(6)]
            with patch.object(hq, "fetch_pooled_records", return_value=records), patch.object(hq, "cmd_run") as run:
                res = hq.cmd_queue_topup(execute=True, root=root)
            run.assert_not_called()
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertFalse(outcome["ran"])
            self.assertEqual(outcome["reason"], "target met")
            self.assertIn("not needed", res.message)

    def test_disabled_writes_reason_without_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_enabled": False})
            with patch.object(hq, "fetch_pooled_records") as fetch, patch.object(hq, "cmd_run") as run:
                hq.cmd_queue_topup(execute=True, root=root)
            fetch.assert_not_called()
            run.assert_not_called()
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertFalse(outcome["ran"])
            self.assertIn("disabled", outcome["reason"])

    def test_deficit_picks_lowest_sendable_vertical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(1), make_record(2)]  # water 2, epoxy 0
            with patch.object(hq, "fetch_pooled_records", return_value=records), patch.object(
                hq, "cmd_run", return_value=completed_run()
            ) as run:
                hq.cmd_queue_topup(execute=True, root=root)
            args, kwargs = run.call_args
            self.assertEqual(args, ("epoxy", "cleveland", "medium"))
            self.assertTrue(kwargs["use_existing"])
            self.assertTrue(kwargs["auto"])
            self.assertTrue(kwargs["yes_score"])
            self.assertTrue(kwargs["yes_live"])
            self.assertFalse(kwargs["include_manual"])
            self.assertTrue(kwargs["execute"])
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "completed")
            self.assertEqual(outcome["vertical"], "epoxy")
            self.assertAlmostEqual(outcome["spend_usd"], 0.42)
            self.assertEqual(outcome["batch"], "water_restoration_cleveland_medium_002")

    def test_over_budget_stop_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            stopped = CommandResult(
                "Projected cost $3.10 exceeds budget $2.00. Autopilot stopped before paid scoring. Resume with: /score-dry x"
            )
            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(
                hq, "cmd_run", return_value=stopped
            ):
                res = hq.cmd_queue_topup(execute=True, root=root)
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "stopped-over-budget")
            self.assertIn("stopped-over-budget", res.message)

    def test_no_existing_batch_retries_fresh_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            calls: list[dict] = []

            def fake_run(vertical, market, size, **kwargs):
                calls.append(kwargs)
                if kwargs.get("use_existing"):
                    raise CommandError("No usable existing batch found for epoxy_cleveland_medium_*.")
                return completed_run()

            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(hq, "cmd_run", fake_run):
                hq.cmd_queue_topup(execute=True, root=root)
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0].get("use_existing"))
            self.assertTrue(calls[1].get("fresh"))
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "completed")

    def test_command_error_recorded_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(
                hq, "cmd_run", side_effect=CommandError("lockfile held")
            ):
                res = hq.cmd_queue_topup(execute=True, root=root)
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "failed")
            self.assertIn("lockfile held", outcome["detail"])
            self.assertIn("failed", res.message)

    def test_dry_run_not_clean_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            not_clean = CommandResult("Scoring complete. Packets built. Live sync refused: dry-run is not clean. ...")
            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(
                hq, "cmd_run", return_value=not_clean
            ):
                hq.cmd_queue_topup(execute=True, root=root)
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "dry-run-not-clean")


class MorningQueueTest(unittest.TestCase):
    def test_writes_schema_file_with_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(i, priority="C") for i in range(1, 8)]
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=3)
            data = json.loads(hq.queue_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], hq.QUEUE_SCHEMA_VERSION)
            self.assertEqual(data["pooled_sendable"], 7)
            self.assertEqual(len(data["items"]), 3)  # target 3, no A/B overflow
            first = data["items"][0]
            self.assertEqual(first["n"], 1)
            self.assertEqual(first["business"], "Biz 1")
            self.assertEqual(first["stored_opener"], "stored opener")
            self.assertEqual(first["safe_outreach_claims"], "claim A; claim B")
            self.assertIsNone(first["sent_at"])

    def test_ab_leads_extend_past_target_capped_at_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(i, priority="A") for i in range(1, 13)]
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=3)
            data = json.loads(hq.queue_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(len(data["items"]), 8)  # 12 A-leads capped by queue_overflow_cap=8

    def test_overwrites_manual_file_and_merges_topup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            today = hq._today()
            hq.queue_file_path(root, today).write_text('{"manual_rehearsal": true}', encoding="utf-8")
            hq.topup_file_path(root, today).write_text(
                json.dumps({"ran": True, "outcome": "completed", "spend_usd": 0.3}), encoding="utf-8"
            )
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                res = hq.cmd_morning_queue(execute=True, root=root)
            data = json.loads(hq.queue_file_path(root, today).read_text(encoding="utf-8"))
            self.assertNotIn("manual_rehearsal", data)
            self.assertEqual(data["topup"]["outcome"], "completed")
            self.assertIn("top-up: completed", res.message)

    def test_degraded_write_on_airtable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with patch.object(hq, "fetch_pooled_records", side_effect=CommandError("Airtable list failed 503")):
                res = hq.cmd_morning_queue(execute=True, root=root)
            data = json.loads(hq.queue_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(data["items"], [])
            self.assertIn("503", data["error"])
            self.assertIn("DEGRADED", res.message)

    def test_preview_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with patch.object(hq, "fetch_pooled_records") as fetch:
                res = hq.cmd_morning_queue(root=root)
            fetch.assert_not_called()
            self.assertIn("preview /morning-queue", res.message)
            self.assertFalse(hq.queue_file_path(root, hq._today()).exists())


def write_queue_file(root: Path, *, items: list[dict], date: str | None = None) -> Path:
    date = date or hq._today()
    payload = {
        "schema_version": hq.QUEUE_SCHEMA_VERSION,
        "date": date,
        "generated_at": "2026-07-09T08:00:00-04:00",
        "target": 5,
        "pooled_sendable": len(items),
        "per_vertical": {},
        "topup": None,
        "error": None,
        "items": items,
    }
    path = hq.queue_file_path(root, date)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def queue_item(n: int, *, sent_at: str | None = None) -> dict:
    return {
        "n": n,
        "record_id": f"recABCDEFGHIJKLM{n}",
        "business": f"Biz {n}",
        "vertical": "water_restoration",
        "sent_at": sent_at,
        "sent_channel": "SMS" if sent_at else None,
    }


def ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="[SENT] ok", stderr="")


class MarkSentTest(unittest.TestCase):
    def test_number_mapping_marks_and_updates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1), queue_item(2), queue_item(3)])
            with patch.object(hq, "_run_capture", return_value=ok_proc()) as run:
                res = hq.cmd_mark_sent(["1", "3"], channel="sms", execute=True, root=root)
            self.assertEqual(run.call_count, 2)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(data["items"][0]["sent_at"])
            self.assertIsNone(data["items"][1]["sent_at"])
            self.assertIsNotNone(data["items"][2]["sent_at"])
            self.assertEqual(data["items"][0]["sent_channel"], "SMS")
            self.assertIn("[SENT] 1. Biz 1", res.message)
            outcomes = (root / "outcomes.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(outcomes), 2)

    def test_unknown_number_refused_with_valid_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_queue_file(root, items=[queue_item(1), queue_item(2)])
            with self.assertRaises(CommandError) as ctx:
                hq.cmd_mark_sent(["7"], execute=True, root=root)
            self.assertIn("1..2", str(ctx.exception))

    def test_already_sent_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_queue_file(root, items=[queue_item(1, sent_at="2026-07-09T09:00:00-04:00")])
            with patch.object(hq, "_run_capture") as run:
                res = hq.cmd_mark_sent(["1"], execute=True, root=root)
            run.assert_not_called()
            self.assertIn("[ALREADY-SENT]", res.message)

    def test_record_id_token_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            item = queue_item(2)
            write_queue_file(root, items=[queue_item(1), item])
            with patch.object(hq, "_run_capture", return_value=ok_proc()):
                res = hq.cmd_mark_sent([item["record_id"]], channel="ig-dm", execute=True, root=root)
            self.assertIn("Instagram DM", res.message)
            self.assertIn("[SENT] 2. Biz 2", res.message)

    def test_non_schema_manual_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            hq.queue_file_path(root, hq._today()).write_text(
                json.dumps({"date": hq._today(), "manual_rehearsal": True, "items": [queue_item(1)]}),
                encoding="utf-8",
            )
            with self.assertRaises(CommandError) as ctx:
                hq.cmd_mark_sent(["1"], execute=True, root=root)
            self.assertIn("schema", str(ctx.exception))

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            before = path.read_text(encoding="utf-8")
            with patch.object(hq, "_run_capture") as run:
                res = hq.cmd_mark_sent(["1"], dry_run=True, execute=True, root=root)
            run.assert_not_called()
            self.assertIn("[DRY-RUN]", res.message)
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertFalse((root / "outcomes.jsonl").exists())

    def test_outcomes_rows_deduped_by_record_channel_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_queue_file(root, items=[queue_item(1)])
            config = hq.load_queue_config(root)
            row = {"record_id": "recABCDEFGHIJKLM1", "channel": "SMS", "date": hq._today()}
            first = hq._append_outcome_row(config, dict(row))
            second = hq._append_outcome_row(config, dict(row))
            self.assertIn("appended", first)
            self.assertIn("already present", second)
            lines = (root / "outcomes.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_patch_failure_leaves_item_unsent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="[ERROR] Airtable PATCH failed 503")
            with patch.object(hq, "_run_capture", return_value=failed):
                res = hq.cmd_mark_sent(["1"], execute=True, root=root)
            self.assertIn("[FAIL]", res.message)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(data["items"][0]["sent_at"])
            self.assertFalse((root / "outcomes.jsonl").exists())


class ParserDispatchTest(unittest.TestCase):
    def test_new_verbs_parse_and_route(self) -> None:
        from lead_machine import hermes_commands as hc

        parser = hc.build_parser()
        args = parser.parse_args(["mark-sent", "1", "3", "--channel", "sms", "--execute"])
        self.assertEqual(args.items, ["1", "3"])
        parser.parse_args(["queue-status", "--execute"])
        parser.parse_args(["queue-topup", "--budget", "0.30", "--execute"])
        parser.parse_args(["morning-queue", "--target", "5", "--execute"])
        for verb in ("queue-status", "queue-topup", "morning-queue", "mark-sent"):
            self.assertIn(verb, hc.TELEGRAM_HERMES_COMMANDS)
        route = hc.route_telegram_message("/queue-status")
        self.assertIsNotNone(route)
        self.assertIn("--execute", route.argv)

    def test_help_mentions_queue_verbs(self) -> None:
        from lead_machine import hermes_commands as hc

        msg = hc.cmd_help().message
        for verb in ("/queue-status", "/queue-topup", "/morning-queue", "/mark-sent"):
            self.assertIn(verb, msg)


if __name__ == "__main__":
    unittest.main()
