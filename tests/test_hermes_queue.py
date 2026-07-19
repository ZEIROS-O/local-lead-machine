from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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
    (root / "outcomes.jsonl").write_text("", encoding="utf-8")
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
        "Reviews": 40,
        "Rating": 4.8,
        "Lead Priority": priority,
        "Score Total": 90 - i,
        "AI Confidence": "high",
        "Money Signal": "Yes",
        "Buyer Intent Signals": "live review demand",
        "Send Trigger": "Quote path dead end",
        "Best Service Angle": "Website Rebuild",
        "Offer Fit": "Website Rebuild",
        "Specific Observation": "obs",
        "Site Status": "Broken",
        "Opener": "stored opener",
        "Safe Outreach Claims": "claim A; claim B",
        "Evidence Refs": '["ev-google-rating-1", "ev-site-status-2"]',
        "Outreach Channel": "Phone",
        "Consent Posture": "",
        "Sendable": "Yes",
        "Packet Eligible": "Yes",
        "Commercial Fit State": "commercially-sendable",
        "Contact Eligibility": "contact-now",
        "Status": "New",
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

    def test_no_existing_batch_retries_fresh_once_at_capped_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            calls: list[tuple[tuple, dict]] = []

            def fake_run(vertical, market, size, **kwargs):
                calls.append(((vertical, market, size), kwargs))
                if kwargs.get("use_existing"):
                    raise CommandError("No usable existing batch found for epoxy_cleveland_medium_*.")
                return completed_run()

            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(hq, "cmd_run", fake_run):
                hq.cmd_queue_topup(execute=True, root=root)
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0][1].get("use_existing"))
            self.assertEqual(calls[0][0][2], "medium")
            self.assertTrue(calls[1][1].get("fresh"))
            self.assertEqual(calls[1][0][2], "small")  # fresh always capped, not the vertical's size
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "completed")
            self.assertEqual(outcome["size"], "small")

    def test_fresh_size_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_fresh_size": "medium"})
            calls: list[tuple] = []

            def fake_run(vertical, market, size, **kwargs):
                calls.append((size, kwargs))
                if kwargs.get("use_existing"):
                    raise CommandError("No usable existing batch found.")
                return completed_run()

            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(hq, "cmd_run", fake_run):
                hq.cmd_queue_topup(execute=True, root=root)
            self.assertEqual(calls[1][0], "medium")

    def test_fresh_disabled_records_no_existing_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_fresh_enabled": False})
            calls: list[dict] = []

            def fake_run(vertical, market, size, **kwargs):
                calls.append(kwargs)
                raise CommandError("No usable existing batch found for epoxy_cleveland_medium_*.")

            with patch.object(hq, "fetch_pooled_records", return_value=[]), patch.object(hq, "cmd_run", fake_run):
                res = hq.cmd_queue_topup(execute=True, root=root)
            self.assertEqual(len(calls), 1)  # no fresh retry
            outcome = json.loads(hq.topup_file_path(root, hq._today()).read_text(encoding="utf-8"))
            self.assertEqual(outcome["outcome"], "no-existing-batch")
            self.assertEqual(outcome["spend_usd"], 0.0)
            self.assertIn("no-existing-batch", res.message)

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
            (root / "outcomes.jsonl").write_text("", encoding="utf-8")
            records = [make_record(i, priority="C") for i in range(1, 8)]
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=3)
            data = read_latest_queue_run(root)
            self.assertEqual(data["schema_version"], hq.QUEUE_SCHEMA_VERSION)
            self.assertEqual(data["pooled_sendable"], 7)
            self.assertEqual(len(data["items"]), 3)  # target 3, no A/B overflow
            first = data["items"][0]
            self.assertEqual(first["n"], 1)
            self.assertEqual(first["business"], "Biz 1")
            self.assertEqual(first["stored_opener"], "stored opener")
            self.assertEqual(first["safe_outreach_claims"], "claim A; claim B")
            self.assertEqual(first["reviews"], 40)
            self.assertEqual(first["rating"], 4.8)
            self.assertEqual(first["channel"], "Phone")
            self.assertEqual(first["suppression_status"], "clear")
            self.assertIn("owner/operator", first["person_read"])
            self.assertIsNone(first["sent_at"])

    def test_opted_out_record_is_excluded_from_immutable_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            record = make_record(1)
            (root / "outcomes.jsonl").write_text(
                json.dumps({"event": "opt_out", "record_id": record["record_id"]}) + "\n",
                encoding="utf-8",
            )
            with patch.object(hq, "fetch_pooled_records", return_value=[record]):
                hq.cmd_morning_queue(execute=True, root=root, target=1)
            data = read_latest_queue_run(root)
            self.assertEqual(data["items"], [])
            self.assertEqual(data["fetched_count"], 1)
            self.assertEqual(data["suppressed_excluded_count"], 1)
            self.assertEqual(data["queued_count"], 0)

    def test_suppression_exclusion_happens_before_target_refill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(i, priority="C") for i in range(1, 5)]
            (root / "outcomes.jsonl").write_text(
                json.dumps({"event": "opt-out", "phone": "+1 (216) 555-0000"}) + "\n",
                encoding="utf-8",
            )
            records[1]["Phone"] = "216.555.0001"
            records[2]["Phone"] = "216.555.0002"
            records[3]["Phone"] = "216.555.0003"
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=3)
            data = read_latest_queue_run(root)
            self.assertEqual(data["fetched_count"], 4)
            self.assertEqual(data["suppressed_excluded_count"], 1)
            self.assertEqual(data["queued_count"], 3)
            self.assertEqual([item["business"] for item in data["items"]], ["Biz 2", "Biz 3", "Biz 4"])

    def test_sms_without_documented_consent_is_excluded_before_target_refill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(i, priority="C") for i in range(1, 5)]
            records[0]["Outreach Channel"] = "SMS"
            records[0]["Consent Posture"] = ""
            records[1]["Outreach Channel"] = "SMS"
            records[1]["Consent Posture"] = "express written consent"
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=3)
            data = read_latest_queue_run(root)
            self.assertEqual(data["sms_consent_excluded_count"], 1)
            self.assertEqual(data["queued_count"], 3)
            self.assertEqual([item["business"] for item in data["items"]], ["Biz 2", "Biz 3", "Biz 4"])

    def test_unsafe_status_is_excluded_before_queue_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(1, priority="C"), make_record(2, priority="C")]
            records[0]["Status"] = "Manual Review"
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=1)
            data = read_latest_queue_run(root)
            self.assertEqual(data["status_excluded_count"], 1)
            self.assertEqual([item["business"] for item in data["items"]], ["Biz 2"])

    def test_missing_suppression_source_writes_degraded_zero_item_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "outcomes.jsonl").unlink()
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                result = hq.cmd_morning_queue(execute=True, root=root, target=1)
            data = read_latest_queue_run(root)
            self.assertEqual(data["items"], [])
            self.assertEqual(data["fetched_count"], 1)
            self.assertIn("missing or unreadable", data["error"])
            self.assertIn("DEGRADED", result.message)

    def test_malformed_suppression_source_writes_degraded_zero_item_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "outcomes.jsonl").write_text('{"event": "opt_out"\n', encoding="utf-8")
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                hq.cmd_morning_queue(execute=True, root=root, target=1)
            data = read_latest_queue_run(root)
            self.assertEqual(data["items"], [])
            self.assertIn("malformed JSON", data["error"])

    def test_ab_leads_extend_past_target_capped_at_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            records = [make_record(i, priority="A") for i in range(1, 13)]
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_morning_queue(execute=True, root=root, target=3)
            data = read_latest_queue_run(root)
            self.assertEqual(len(data["items"]), 8)  # 12 A-leads capped by queue_overflow_cap=8

    def test_preserves_prior_run_and_merges_topup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            today = hq._today()
            hq.queue_file_path(root, today).write_text('{"manual_rehearsal": true}', encoding="utf-8")
            hq.topup_file_path(root, today).write_text(
                json.dumps(
                    {
                        "date": today,
                        "generated_at": "2026-07-09T06:30:00-04:00",
                        "target": 5,
                        "budget_usd": 2.0,
                        "ran": True,
                        "outcome": "completed",
                        "vertical": "epoxy",
                        "market": "cleveland",
                        "size": "small",
                        "detail": "Live sync complete",
                        "spend_usd": 0.3,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                res = hq.cmd_morning_queue(execute=True, root=root)
            data = read_latest_queue_run(root, today)
            self.assertEqual(json.loads(hq.queue_file_path(root, today).read_text(encoding="utf-8"))["runs"][-1]["queue_run_id"], data["queue_run_id"])
            self.assertEqual(data["topup"]["outcome"], "completed")
            self.assertIn("outcome: completed", res.message)

    def test_missing_topup_report_emits_exact_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_enabled": False})
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                res = hq.cmd_morning_queue(execute=True, root=root)
            data = read_latest_queue_run(root)
            self.assertEqual(data["topup_report"]["status"], "missing")
            self.assertFalse(data["topup_report"]["produced"])
            self.assertEqual(data["topup_report"]["message"], hq.TOPUP_REPORT_MISSING)
            self.assertIn(hq.TOPUP_REPORT_MISSING, res.message)

    def test_malformed_topup_report_emits_exact_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_enabled": False})
            hq.topup_file_path(root, hq._today()).write_text("{bad json", encoding="utf-8")
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                hq.cmd_morning_queue(execute=True, root=root)
            data = read_latest_queue_run(root)
            self.assertEqual(data["topup_report"]["message"], hq.TOPUP_REPORT_MISSING)

    def test_disabled_topup_report_is_present_and_zero_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_enabled": False})
            hq.cmd_queue_topup(execute=True, root=root)
            with patch.object(hq, "fetch_pooled_records", return_value=[make_record(1)]):
                res = hq.cmd_morning_queue(execute=True, root=root)
            data = read_latest_queue_run(root)
            self.assertEqual(data["topup_report"]["status"], "present")
            self.assertTrue(data["topup_report"]["produced"])
            self.assertIn("topup disabled in config", data["topup_report"]["message"])
            self.assertIn("spend: $0.00", res.message)

    def test_v1b_target_met_report_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, config={"topup_enabled": True})
            records = [make_record(i) for i in range(6)]
            with patch.object(hq, "fetch_pooled_records", return_value=records):
                hq.cmd_queue_topup(execute=True, root=root)
                hq.cmd_morning_queue(execute=True, root=root)
            data = read_latest_queue_run(root)
            self.assertEqual(data["topup_report"]["status"], "present")
            self.assertIn("reason: target met", data["topup_report"]["message"])

    def test_degraded_write_on_airtable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with patch.object(hq, "fetch_pooled_records", side_effect=CommandError("Airtable list failed 503")):
                res = hq.cmd_morning_queue(execute=True, root=root)
            data = read_latest_queue_run(root)
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


def read_latest_queue_run(root: Path, date: str | None = None) -> dict:
    date = date or hq._today()
    index = json.loads(hq.queue_file_path(root, date).read_text(encoding="utf-8"))
    run_id = index["latest_queue_run_id"]
    return json.loads(hq.queue_run_path(root, date, run_id).read_text(encoding="utf-8"))


def write_queue_file(root: Path, *, items: list[dict], date: str | None = None, queue_run_id: str = "mq-test-run") -> Path:
    date = date or hq._today()
    payload = {
        "schema_version": hq.QUEUE_SCHEMA_VERSION,
        "queue_run_id": queue_run_id,
        "created_at": "2026-07-09T08:00:00-04:00",
        "date": date,
        "generated_at": "2026-07-09T08:00:00-04:00",
        "target": 5,
        "pooled_sendable": len(items),
        "per_vertical": {},
        "topup": None,
        "error": None,
        "items": items,
    }
    payload["queue_sha256"] = hq._queue_content_hash(payload)
    path = hq.queue_run_path(root, date, queue_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    hq._write_queue_index(root, date, payload, hq.queue_file_path(root, date))
    return path


def queue_identity(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "queue_run_id": data["queue_run_id"],
        "queue_sha256": data["queue_sha256"],
        "artifact_sha256": "a" * 64,
    }


def dry_run_approval(root: Path, path: Path, tokens: list[str], *, channel: str = "sms") -> str:
    res = hq.cmd_mark_sent(tokens, channel=channel, dry_run=True, execute=True, root=root, **queue_identity(path))
    return next(line.split(": ", 1)[1] for line in res.message.splitlines() if line.startswith("approval_id:"))


def queue_item(n: int, *, sent_at: str | None = None) -> dict:
    return {
        "n": n,
        "record_id": f"recABCDEFGHIJKLM{n}",
        "business": f"Biz {n}",
        "vertical": "water_restoration",
        "consent_posture": "express written consent",
        "sent_at": sent_at,
        "sent_channel": "SMS" if sent_at else None,
    }


def ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"result": "applied", "record_id": "rec", "status": "Sent"}),
        stderr="",
    )


class MarkSentTest(unittest.TestCase):
    def test_concurrent_mark_sent_attempts_serialize_to_one_external_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            approval_id = dry_run_approval(root, path, ["1"])

            def slow_apply(*_args, **_kwargs):
                time.sleep(0.05)
                return ok_proc()

            def execute_once():
                return hq.cmd_mark_sent(
                    ["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path)
                )

            with patch.object(hq, "_run_capture", side_effect=slow_apply) as run:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: execute_once(), range(2)))
            self.assertEqual(run.call_count, 1)
            self.assertTrue(any("[SENT]" in result.message for result in results))
            self.assertTrue(any("[ALREADY-SENT]" in result.message for result in results))
            self.assertEqual(len((root / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_concurrent_queue_runs_share_one_event_and_external_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first_path = write_queue_file(root, items=[queue_item(1)], queue_run_id="mq-first")
            second_path = write_queue_file(root, items=[queue_item(1)], queue_run_id="mq-second")
            first_approval = dry_run_approval(root, first_path, ["1"])
            second_approval = dry_run_approval(root, second_path, ["1"])

            def slow_apply(*_args, **_kwargs):
                time.sleep(0.05)
                return ok_proc()

            calls = [
                (first_path, first_approval),
                (second_path, second_approval),
            ]

            def execute(call):
                path, approval_id = call
                return hq.cmd_mark_sent(
                    ["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path)
                )

            with patch.object(hq, "_run_capture", side_effect=slow_apply) as run:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(execute, calls))
            self.assertEqual(run.call_count, 1)
            self.assertTrue(any("[SENT]" in result.message for result in results))
            self.assertTrue(any("[ALREADY-SENT]" in result.message for result in results))
            outcomes = (root / "outcomes.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(json.loads(outcomes[0])["event_id"].startswith("sent-event-"))

    def test_same_event_with_different_artifact_is_refused_before_external_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first_path = write_queue_file(root, items=[queue_item(1)], queue_run_id="mq-first")
            first_approval = dry_run_approval(root, first_path, ["1"])
            with patch.object(hq, "_run_capture", return_value=ok_proc()):
                hq.cmd_mark_sent(
                    ["1"], approval_id=first_approval, execute=True, root=root, **queue_identity(first_path)
                )

            second_path = write_queue_file(root, items=[queue_item(1)], queue_run_id="mq-second")
            second_identity = queue_identity(second_path)
            second_identity["artifact_sha256"] = "b" * 64
            dry = hq.cmd_mark_sent(["1"], dry_run=True, execute=True, root=root, **second_identity)
            second_approval = next(
                line.split(": ", 1)[1] for line in dry.message.splitlines() if line.startswith("approval_id:")
            )
            with patch.object(hq, "_run_capture") as run, self.assertRaisesRegex(
                CommandError, "conflicting outcome artifact"
            ):
                hq.cmd_mark_sent(
                    ["1"], approval_id=second_approval, execute=True, root=root, **second_identity
                )
            run.assert_not_called()

    def test_v1_approval_without_send_date_is_bound_to_immutable_queue_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)], date="2026-07-10")
            approval_id = dry_run_approval(root, path, ["1"])
            approval_path = hq.queue_approval_path(root, approval_id)
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval["schema_version"] = 1
            approval.pop("send_date")
            approval["approval_sha256"] = hq._canonical_hash(
                {key: value for key, value in approval.items() if key != "approval_sha256"}
            )
            approval_path.write_text(json.dumps(approval), encoding="utf-8")

            with patch.object(hq, "_run_capture", return_value=ok_proc()) as run:
                hq.cmd_mark_sent(
                    ["1"], date="2026-07-10", approval_id=approval_id, execute=True, root=root,
                    **queue_identity(path),
                )
            self.assertEqual(run.call_count, 1)

            with self.assertRaisesRegex(CommandError, "send_date"):
                hq.cmd_mark_sent(
                    ["1"], date="2026-07-11", approval_id=approval_id, execute=True, root=root,
                    **queue_identity(path),
                )

    def test_v1_writeback_exact_retry_remains_idempotent_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            queue_date = hq._today()
            path = write_queue_file(root, items=[queue_item(1)], date=queue_date)
            approval_id = dry_run_approval(root, path, ["1"])
            state_path = hq.queue_writeback_path(root, "mq-test-run")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "queue_run_id": "mq-test-run",
                        "sent": {
                            queue_item(1)["record_id"]: {
                                "sent_at": f"{queue_date}T09:00:00-04:00",
                                "sent_channel": "SMS",
                                "approval_id": "legacy",
                                "artifact_sha256": "a" * 64,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            before = state_path.read_bytes()
            with patch.object(hq, "_run_capture") as run:
                result = hq.cmd_mark_sent(
                    ["1"], date=queue_date, approval_id=approval_id, execute=True, root=root,
                    **queue_identity(path),
                )
            run.assert_not_called()
            self.assertIn("[ALREADY-SENT]", result.message)
            self.assertEqual(state_path.read_bytes(), before)

    def test_journal_failure_before_airtable_aborts_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            approval_id = dry_run_approval(root, path, ["1"])
            with patch.object(hq, "_atomic_write_json", side_effect=OSError("disk full")), patch.object(
                hq, "_run_capture"
            ) as run, self.assertRaises(OSError):
                hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            run.assert_not_called()

    def test_retry_after_airtable_confirmation_journal_failure_converges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            approval_id = dry_run_approval(root, path, ["1"])
            real_write = hq._atomic_write_json
            writes = 0

            def fail_second(write_path, payload):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("post-patch disk failure")
                return real_write(write_path, payload)

            with patch.object(hq, "_atomic_write_json", side_effect=fail_second), patch.object(
                hq, "_run_capture", return_value=ok_proc()
            ) as run, self.assertRaises(OSError):
                hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            self.assertEqual(run.call_count, 1)
            pending = json.loads(hq.queue_writeback_path(root, "mq-test-run").read_text(encoding="utf-8"))
            self.assertEqual(pending["operations"][queue_item(1)["record_id"]]["phase"], "pending")

            already = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps({"result": "already_applied"}), stderr=""
            )
            with patch.object(hq, "_run_capture", return_value=already) as retry:
                hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            self.assertEqual(retry.call_count, 1)
            completed = json.loads(hq.queue_writeback_path(root, "mq-test-run").read_text(encoding="utf-8"))
            self.assertEqual(completed["operations"][queue_item(1)["record_id"]]["phase"], "complete")
            self.assertEqual(len((root / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_retry_after_outcome_logging_failure_deduplicates_without_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            approval_id = dry_run_approval(root, path, ["1"])
            real_write = hq._atomic_write_json
            writes = 0

            def fail_third(write_path, payload):
                nonlocal writes
                writes += 1
                if writes == 3:
                    raise OSError("completion disk failure")
                return real_write(write_path, payload)

            with patch.object(hq, "_atomic_write_json", side_effect=fail_third), patch.object(
                hq, "_run_capture", return_value=ok_proc()
            ), self.assertRaises(OSError):
                hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            state = json.loads(hq.queue_writeback_path(root, "mq-test-run").read_text(encoding="utf-8"))
            self.assertEqual(state["operations"][queue_item(1)["record_id"]]["phase"], "airtable_applied")
            self.assertEqual(len((root / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()), 1)

            with patch.object(hq, "_run_capture") as run:
                hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            run.assert_not_called()
            self.assertEqual(len((root / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_number_mapping_marks_and_updates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1), queue_item(2), queue_item(3)])
            immutable_before = path.read_bytes()
            approval_id = dry_run_approval(root, path, ["1", "3"])
            with patch.object(hq, "_run_capture", return_value=ok_proc()) as run:
                res = hq.cmd_mark_sent(["1", "3"], channel="sms", approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            self.assertEqual(run.call_count, 2)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(data["items"][0]["sent_at"])
            self.assertEqual(path.read_bytes(), immutable_before)
            state = json.loads(hq.queue_writeback_path(root, data["queue_run_id"]).read_text(encoding="utf-8"))
            self.assertEqual(set(state["sent"]), {data["items"][0]["record_id"], data["items"][2]["record_id"]})
            self.assertEqual(state["sent"][data["items"][0]["record_id"]]["sent_channel"], "SMS")
            self.assertIn("[SENT] 1. Biz 1", res.message)
            outcomes = (root / "outcomes.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(outcomes), 2)

    def test_delayed_send_uses_actual_date_not_queue_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)], date="2026-07-09")
            identity = queue_identity(path)
            dry = hq.cmd_mark_sent(
                ["1"], date="2026-07-10", dry_run=True, execute=True, root=root, **identity
            )
            approval_id = next(
                line.split(": ", 1)[1] for line in dry.message.splitlines() if line.startswith("approval_id:")
            )
            with patch.object(hq, "_run_capture", return_value=ok_proc()) as run:
                hq.cmd_mark_sent(
                    ["1"], date="2026-07-10", approval_id=approval_id, execute=True, root=root, **identity
                )
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--date") + 1], "2026-07-10")
            self.assertEqual(command[command.index("--suppression-source") + 1], str(root / "outcomes.jsonl"))
            outcome = json.loads((root / "outcomes.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(outcome["date"], "2026-07-10")

    def test_sms_mark_sent_refuses_missing_consent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            item = queue_item(1)
            item["consent_posture"] = ""
            path = write_queue_file(root, items=[item])
            with patch.object(hq, "_run_capture") as run, self.assertRaisesRegex(
                CommandError, "documented consent is missing or unacceptable"
            ):
                hq.cmd_mark_sent(["1"], dry_run=True, execute=True, root=root, **queue_identity(path))
            run.assert_not_called()

    def test_mark_sent_refuses_unsafe_status_bound_into_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            item = queue_item(1)
            item["status"] = "Manual Review"
            path = write_queue_file(root, items=[item])
            with patch.object(hq, "_run_capture") as run, self.assertRaisesRegex(
                CommandError, "New or Mockup Needed"
            ):
                hq.cmd_mark_sent(["1"], dry_run=True, execute=True, root=root, **queue_identity(path))
            run.assert_not_called()

    def test_unknown_number_refused_with_valid_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1), queue_item(2)])
            with self.assertRaises(CommandError) as ctx:
                hq.cmd_mark_sent(["7"], execute=True, root=root, **queue_identity(path))
            self.assertIn("1..2", str(ctx.exception))

    def test_bare_number_against_latest_queue_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_queue_file(root, items=[queue_item(1)])
            with self.assertRaisesRegex(CommandError, "exact --queue-run-id"):
                hq.cmd_mark_sent(["1"], artifact_sha256="a" * 64, execute=True, root=root)

    def test_davis_roofing_digest_cannot_mark_sbr_concrete_after_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            davis = queue_item(1)
            davis["business"] = "Davis Roofing"
            davis["record_id"] = "recDAVISROOFING01"
            davis_path = write_queue_file(root, items=[davis], queue_run_id="mq-davis")
            approval_id = dry_run_approval(root, davis_path, ["1"])

            sbr = queue_item(1)
            sbr["business"] = "SBR Concrete"
            sbr["record_id"] = "recSBRCONCRETE001"
            sbr_path = write_queue_file(root, items=[sbr], queue_run_id="mq-sbr")

            with patch.object(hq, "_run_capture") as run, self.assertRaisesRegex(CommandError, "approval mismatch"):
                hq.cmd_mark_sent(
                    ["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(sbr_path)
                )
            run.assert_not_called()
            self.assertFalse(hq.queue_writeback_path(root, "mq-sbr").exists())

    def test_changed_channel_or_artifact_hash_invalidates_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            approval_id = dry_run_approval(root, path, ["1"], channel="sms")
            identity = queue_identity(path)
            identity["artifact_sha256"] = "b" * 64
            with self.assertRaisesRegex(CommandError, "artifact_sha256"):
                hq.cmd_mark_sent(["1"], channel="sms", approval_id=approval_id, execute=True, root=root, **identity)

    def test_already_sent_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            immutable_before = path.read_bytes()
            approval_id = dry_run_approval(root, path, ["1"])
            with patch.object(hq, "_run_capture", return_value=ok_proc()):
                hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            queue_run_id = json.loads(path.read_text(encoding="utf-8"))["queue_run_id"]
            writeback_path = hq.queue_writeback_path(root, queue_run_id)
            writeback_before = writeback_path.read_bytes()
            outcomes_before = (root / "outcomes.jsonl").read_bytes()
            with patch.object(hq, "_run_capture") as repeated:
                res = hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            repeated.assert_not_called()
            self.assertIn("[ALREADY-SENT]", res.message)
            self.assertEqual(path.read_bytes(), immutable_before)
            self.assertEqual(writeback_path.read_bytes(), writeback_before)
            self.assertEqual((root / "outcomes.jsonl").read_bytes(), outcomes_before)

    def test_record_id_token_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            item = queue_item(2)
            path = write_queue_file(root, items=[queue_item(1), item])
            approval_id = dry_run_approval(root, path, [item["record_id"]], channel="ig-dm")
            with patch.object(hq, "_run_capture", return_value=ok_proc()):
                res = hq.cmd_mark_sent([item["record_id"]], channel="ig-dm", approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            self.assertIn("Instagram DM", res.message)
            self.assertIn("[SENT] 2. Biz 2", res.message)

    def test_record_id_writeback_does_not_require_number_or_latest_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            record_id = "recDIRECTRECORD01"
            dry = hq.cmd_mark_sent(
                [record_id],
                channel="email",
                artifact_sha256="c" * 64,
                dry_run=True,
                execute=True,
                root=root,
            )
            approval_id = next(
                line.split(": ", 1)[1] for line in dry.message.splitlines() if line.startswith("approval_id:")
            )
            with patch.object(hq, "_run_capture", return_value=ok_proc()) as run:
                live = hq.cmd_mark_sent(
                    [record_id],
                    channel="email",
                    artifact_sha256="c" * 64,
                    approval_id=approval_id,
                    execute=True,
                    root=root,
                )
            self.assertIn(f"[SENT]", live.message)
            self.assertIn(record_id, run.call_args.args[0])
            with patch.object(hq, "_run_capture") as repeated:
                again = hq.cmd_mark_sent(
                    [record_id], channel="email", artifact_sha256="c" * 64,
                    approval_id=approval_id, execute=True, root=root,
                )
            repeated.assert_not_called()
            self.assertIn("[ALREADY-SENT]", again.message)

    def test_non_schema_manual_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            run_path = hq.queue_run_path(root, hq._today(), "mq-manual")
            run_path.parent.mkdir(parents=True, exist_ok=True)
            run_path.write_text(json.dumps({"date": hq._today(), "manual_rehearsal": True, "items": [queue_item(1)]}), encoding="utf-8")
            with self.assertRaises(CommandError) as ctx:
                hq.cmd_mark_sent(["1"], queue_run_id="mq-manual", queue_sha256="b" * 64, artifact_sha256="a" * 64, execute=True, root=root)
            self.assertIn("schema", str(ctx.exception))

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = write_queue_file(root, items=[queue_item(1)])
            before = path.read_text(encoding="utf-8")
            with patch.object(hq, "_run_capture") as run:
                res = hq.cmd_mark_sent(["1"], dry_run=True, execute=True, root=root, **queue_identity(path))
            run.assert_not_called()
            self.assertIn("[DRY-RUN]", res.message)
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual((root / "outcomes.jsonl").read_text(encoding="utf-8"), "")

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
            approval_id = dry_run_approval(root, path, ["1"])
            failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="[ERROR] Airtable PATCH failed 503")
            with patch.object(hq, "_run_capture", return_value=failed):
                res = hq.cmd_mark_sent(["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path))
            self.assertIn("[FAIL]", res.message)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(data["items"][0]["sent_at"])
            self.assertEqual((root / "outcomes.jsonl").read_text(encoding="utf-8"), "")

    def test_dry_run_refuses_opt_out_recorded_after_queue_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            item = queue_item(1)
            item["phone"] = "(216) 555-1234"
            path = write_queue_file(root, items=[item])
            (root / "outcomes.jsonl").write_text(
                json.dumps({"event": "opt_out", "phone": "+1 216-555-1234"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(hq, "_run_capture") as run, self.assertRaisesRegex(CommandError, "suppressed or opted out"):
                hq.cmd_mark_sent(["1"], dry_run=True, execute=True, root=root, **queue_identity(path))
            run.assert_not_called()

    def test_live_mark_sent_refuses_opt_out_recorded_after_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            item = queue_item(1)
            item["email"] = "Owner@Example.COM"
            path = write_queue_file(root, items=[item])
            approval_id = dry_run_approval(root, path, ["1"])
            (root / "outcomes.jsonl").write_text(
                json.dumps({"event": "unsubscribed", "email": "owner@example.com"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(hq, "_run_capture") as run, self.assertRaisesRegex(CommandError, "suppressed or opted out"):
                hq.cmd_mark_sent(
                    ["1"], approval_id=approval_id, execute=True, root=root, **queue_identity(path)
                )
            run.assert_not_called()


class ParserDispatchTest(unittest.TestCase):
    def test_new_verbs_parse_and_route(self) -> None:
        from lead_machine import hermes_commands as hc

        parser = hc.build_parser()
        args = parser.parse_args(
            ["mark-sent", "1", "3", "--channel", "sms", "--date", "2026-07-10", "--execute"]
        )
        self.assertEqual(args.items, ["1", "3"])
        self.assertEqual(args.date, "2026-07-10")
        parser.parse_args(["queue-status", "--execute"])
        parser.parse_args(["queue-topup", "--budget", "0.30", "--execute"])
        parser.parse_args(["morning-queue", "--target", "5", "--execute"])
        for verb in ("queue-status", "queue-topup", "morning-queue", "mark-sent"):
            self.assertIn(verb, hc.TELEGRAM_HERMES_COMMANDS)
        route = hc.route_telegram_message("/queue-status")
        self.assertIsNotNone(route)
        self.assertIn("--execute", route.argv)

    def test_refusal_prints_refuse_under_python_dash_m(self) -> None:
        # Regression: under `python -m lead_machine.hermes_commands` the module
        # runs as __main__ while hermes_queue raises the canonical module's
        # CommandError; without the dispatch re-raise this escaped as a
        # traceback instead of [REFUSE].
        import sys as _sys

        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = subprocess.run(
                [_sys.executable, "-m", "lead_machine.hermes_commands", "mark-sent", "1", "--execute"],
                cwd=Path(__file__).resolve().parent.parent,
                env={**__import__("os").environ, "LOCAL_LEAD_MACHINE_ROOT": str(root)},
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("[REFUSE]", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_help_mentions_queue_verbs(self) -> None:
        from lead_machine import hermes_commands as hc

        msg = hc.cmd_help().message
        for verb in ("/queue-status", "/queue-topup", "/morning-queue", "/mark-sent"):
            self.assertIn(verb, msg)


if __name__ == "__main__":
    unittest.main()
