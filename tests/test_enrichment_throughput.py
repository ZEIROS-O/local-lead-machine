"""Enrichment ran one lead at a time and wrote the file once, at the end.

Both loops were sequential with a `time.sleep(0.2)` between leads, against a
45s per-lead timeout, on work that is pure network I/O. And `write_jsonl` ran
once after every lead finished, so a Ctrl-C or a failure in that final write
discarded the whole run's scraping — the expensive part.

These tests cover the properties that make concurrency safe rather than the
speedup itself: same records regardless of worker count, nothing lost on
interrupt, nothing dropped silently, and one outcome for a failed lead rather
than two depending on the entry point.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import enrich_leads
from enrich_leads import (
    DEFAULT_BROWSER_ENRICH_WORKERS,
    DEFAULT_ENRICH_WORKERS,
    JsonlAppender,
    build_enrichment_error_record,
    enrich_rows_concurrently,
    resolve_enrich_workers,
)


def _rows(count: int) -> list[dict]:
    return [
        {"business_name": f"Lead {index}", "place_id": f"place-{index}", "website_url": ""}
        for index in range(count)
    ]


class WorkerResolutionTest(unittest.TestCase):
    def test_pure_network_work_gets_the_full_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_enrich_workers(browser_work=False), DEFAULT_ENRICH_WORKERS)

    def test_browser_work_defaults_lower(self) -> None:
        # Each rendered check or screenshot launches its own Chromium, so the
        # default that is right for HTTP fetches is a memory problem here.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_enrich_workers(browser_work=True), DEFAULT_BROWSER_ENRICH_WORKERS
            )
        self.assertLess(DEFAULT_BROWSER_ENRICH_WORKERS, DEFAULT_ENRICH_WORKERS)

    def test_an_explicit_setting_is_always_honoured(self) -> None:
        with patch.dict(os.environ, {"ENRICH_WORKERS": "12"}, clear=True):
            self.assertEqual(resolve_enrich_workers(browser_work=True), 12)

    def test_a_nonsense_setting_does_not_produce_zero_workers(self) -> None:
        with patch.dict(os.environ, {"ENRICH_WORKERS": "0"}, clear=True):
            self.assertGreaterEqual(resolve_enrich_workers(browser_work=False), 1)


class ConcurrentEnrichmentTest(unittest.TestCase):
    def test_every_lead_is_enriched_exactly_once(self) -> None:
        rows = _rows(20)
        seen: list[str] = []
        lock = threading.Lock()

        def enrich(row):
            with lock:
                seen.append(row["place_id"])
            return {"google": row}

        completed: list[dict] = []
        enrich_rows_concurrently(
            rows,
            workers=6,
            enrich=enrich,
            on_complete=lambda index, row, record, error, elapsed: completed.append(record),
        )

        self.assertEqual(sorted(seen), sorted(row["place_id"] for row in rows))
        self.assertEqual(len(completed), len(rows))

    def test_results_match_a_serial_run(self) -> None:
        # Records may be reordered; each record's content must not change.
        rows = _rows(15)

        def enrich(row):
            return {"google": row, "website": {"site_status": "No Website"}}

        def run(workers):
            out = []
            enrich_rows_concurrently(
                rows,
                workers=workers,
                enrich=enrich,
                on_complete=lambda index, row, record, error, elapsed: out.append(record),
            )
            return sorted(json.dumps(item, sort_keys=True) for item in out)

        self.assertEqual(run(1), run(6))

    def test_one_failing_lead_does_not_abort_the_others(self) -> None:
        rows = _rows(10)

        def enrich(row):
            if row["place_id"] == "place-4":
                raise RuntimeError("scrape exploded")
            return {"google": row}

        errors: list[str] = []
        records: list[dict] = []

        def on_complete(index, row, record, error, elapsed):
            if record is None:
                errors.append(error)
            else:
                records.append(record)

        enrich_rows_concurrently(rows, workers=4, enrich=enrich, on_complete=on_complete)

        self.assertEqual(len(records), 9)
        self.assertEqual(len(errors), 1)
        self.assertIn("scrape exploded", errors[0])

    def test_on_complete_runs_on_the_calling_thread(self) -> None:
        # Callers persist and log from on_complete, so it must not need its own
        # locking.
        threads = set()

        def enrich(row):
            return {"google": row}

        enrich_rows_concurrently(
            _rows(8),
            workers=4,
            enrich=enrich,
            on_complete=lambda *_: threads.add(threading.current_thread().name),
        )

        self.assertEqual(threads, {threading.current_thread().name})


class IncrementalPersistenceTest(unittest.TestCase):
    def test_records_are_readable_before_the_run_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "enriched.jsonl"
            with JsonlAppender(path) as appender:
                appender.append({"google": {"business_name": "First"}})
                mid_run = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                appender.append({"google": {"business_name": "Second"}})

            final = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(mid_run), 1)
        self.assertEqual(len(final), 2)
        self.assertEqual(appender.count, 2)

    def test_concurrent_appends_do_not_interleave_within_a_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "enriched.jsonl"
            with JsonlAppender(path) as appender:
                def write(index):
                    appender.append({"google": {"business_name": f"Lead {index}", "pad": "x" * 4000}})

                threads = [threading.Thread(target=write, args=(index,)) for index in range(24)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(lines), 24)
        for line in lines:
            json.loads(line)  # raises if two writers interleaved


class FailedLeadRecordTest(unittest.TestCase):
    def test_a_failed_lead_survives_with_an_unknown_site_status(self) -> None:
        record = build_enrichment_error_record(
            {"business_name": "Patio Pros", "place_id": "place-1"},
            error="connection reset",
            elapsed=1.5,
        )

        self.assertEqual(record["google"]["business_name"], "Patio Pros")
        self.assertEqual(record["website"]["site_status"], "Unknown")
        self.assertIn("enrichment_exception", record["website"]["site_issue_flags"])
        self.assertEqual(record["enrichment"]["error"], "connection reset")
        # Scoring reads both of these, so a failed lead must still carry them.
        self.assertIn("evidence", record)
        self.assertIn("buyer_intent", record)


class TruncationTest(unittest.TestCase):
    def test_hitting_the_cap_is_announced_rather_than_silent(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "leads.csv"
            source.write_text(
                "business_name,website_url,phone,place_id\n"
                + "".join(f"Lead {i},,216555010{i},place-{i}\n" for i in range(5)),
                encoding="utf-8",
            )
            argv = [
                "enrich_leads.py",
                "--input", str(source),
                "--output", str(root / "out.jsonl"),
                "--screenshots-dir", str(root / "shots"),
                "--no-screenshots",
            ]
            env = {
                "MAX_WEBSITES_PER_RUN": "2",
                "ALLOW_MISSING_SUPPRESSION_INDEX": "1",
                "REVIEW_SNAPSHOT_DB": str(root / "velocity.sqlite3"),
                "ENRICH_WORKERS": "2",
            }
            with patch("sys.argv", argv), patch.dict(os.environ, env, clear=True):
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    code = enrich_leads.main()

            written = [
                line
                for line in (root / "out.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(code, 0)
        self.assertEqual(len(written), 2)
        self.assertIn("[STOP] Hit MAX_WEBSITES_PER_RUN=2 (dropped 3 leads)", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
