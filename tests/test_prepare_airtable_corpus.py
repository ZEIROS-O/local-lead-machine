from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lead_machine.compliance import DNC_SUPPRESSED_REASON, suppression_identifier
from refresh_verdicts import VERDICT_REFRESH_FIELDS
from tests.test_verdict import SENDABLE_ROW


class PrepareAirtableCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        # This CLI now fails closed on an unconfigured suppression ledger — it
        # rebuilds `Verdict` and feeds upload_airtable, so running it with no
        # ledger is how an opted-out lead is pushed back to the base as `send`.
        # These cases are about paths, counts and reports, so give them a real
        # but empty ledger and let the compliance behaviour be tested directly
        # in PrepareAirtableCorpusSuppressionTests.
        ledger_dir = tempfile.TemporaryDirectory()
        self.addCleanup(ledger_dir.cleanup)
        self.ledger = Path(ledger_dir.name) / "suppression.jsonl"
        self.ledger.write_text("", encoding="utf-8")
        patcher = patch.dict(os.environ, {"SUPPRESSION_SOURCE": str(self.ledger)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_csv(
        self, directory: Path, name: str, rows: list[dict[str, str]], mtime_ns: int
    ) -> Path:
        path = directory / name
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def _row(self, **changes: str) -> dict[str, str]:
        return {**SENDABLE_ROW, "Vertical": "Hardscaping", **changes}

    def _write_malformed_csv(self, path: Path, kind: str) -> None:
        row = self._row()
        fieldnames = list(row)
        values = [row[field] for field in fieldnames]
        if kind == "blank header":
            fieldnames.append("")
            values.append("")
        elif kind == "duplicate header":
            fieldnames.append("Business Name")
            values.append(row["Business Name"])
        elif kind == "unexpected extra cells":
            values.append("unexpected")
        elif kind == "missing cells":
            values.pop()
        else:
            raise AssertionError(f"unknown malformed CSV kind: {kind}")
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(fieldnames)
            writer.writerow(values)

    def test_newer_place_key_row_replaces_older_row(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            older = self._write_csv(
                directory, "older.csv", [self._row(**{"Place Key": "place-1", "Business Name": "Older"})], 1
            )
            newer = self._write_csv(
                directory, "newer.csv", [self._row(**{"Place Key": "place-1", "Business Name": "Newer"})], 2
            )

            _, rows, report = prepare_corpus([newer, older], {})

        self.assertEqual([row["Business Name"] for row in rows], ["Newer"])
        self.assertEqual(report["overwritten_rows"], 1)

    def test_phone_only_key_uses_normalized_phone(self) -> None:
        from prepare_airtable_corpus import upsert_key

        self.assertEqual(
            upsert_key(self._row(**{"Place Key": "", "Phone": "(216) 555-0100"})),
            "phone:2165550100",
        )

    def test_place_keys_remain_distinct_when_phones_match(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_csv(
                Path(temporary),
                "source.csv",
                [
                    self._row(**{"Place Key": "first", "Phone": "216-555-0100"}),
                    self._row(**{"Place Key": "second", "Phone": "216-555-0100"}),
                ],
                1,
            )
            _, rows, _ = prepare_corpus([path], {})

        self.assertEqual([row["Place Key"] for row in rows], ["first", "second"])

    def test_no_place_key_rows_with_same_phone_keep_newest_row(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_csv(
                Path(temporary),
                "source.csv",
                [
                    self._row(**{"Place Key": "", "Phone": "216-555-0100", "Business Name": "Older"}),
                    self._row(**{"Place Key": "", "Phone": "(216) 555-0100", "Business Name": "Newer"}),
                ],
                1,
            )
            _, rows, report = prepare_corpus([path], {})

        self.assertEqual([row["Business Name"] for row in rows], ["Newer"])
        self.assertEqual(report["overwritten_rows"], 1)

    def test_missing_identifiers_is_reported_and_not_written(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_csv(
                Path(temporary), "source.csv", [self._row(**{"Place Key": "", "Phone": ""})], 1
            )
            _, rows, report = prepare_corpus([path], {})

        self.assertEqual(rows, [])
        self.assertEqual(report["skipped_no_upsert"], 1)

    def test_registered_stored_verticals_normalize_to_airtable_choices(self) -> None:
        from prepare_airtable_corpus import resolve_upload_vertical

        for stored, expected in (
            ("Hardscaping", "hardscaping"),
            ("Landscaping / Hardscaping", "hardscaping"),
            ("Masonry / Hardscaping", "hardscaping"),
            ("Roofing", "roofing"),
            ("Detailing", "detailing"),
        ):
            with self.subTest(stored=stored):
                self.assertEqual(
                    resolve_upload_vertical(self._row(Vertical=stored), {}, "place:vertical-test"), expected
                )

    def test_service_vertical_resolves_from_raw_evidence_google_context(self) -> None:
        from prepare_airtable_corpus import resolve_upload_vertical

        row = self._row(
            Vertical="service",
            **{
                "Raw Evidence": json.dumps(
                    {"google": {"category": "Auto detailing service", "source_query": "detailing Cleveland OH"}}
                )
            },
        )

        self.assertEqual(resolve_upload_vertical(row, {}, "place:service"), "detailing")

    def test_unresolved_service_requires_exact_key_override(self) -> None:
        from prepare_airtable_corpus import resolve_upload_vertical

        row = self._row(
            Vertical="service",
            **{"Raw Evidence": json.dumps({"google": {"category": "Novel service", "source_query": "Novel service"}})},
        )

        with self.assertRaisesRegex(ValueError, "cannot resolve"):
            resolve_upload_vertical(row, {}, "place:unknown")
        self.assertEqual(
            resolve_upload_vertical(row, {"place:unknown": "Hardscaping"}, "place:unknown"), "hardscaping"
        )

    def test_unsupported_stored_vertical_is_rejected(self) -> None:
        from prepare_airtable_corpus import resolve_upload_vertical

        with self.assertRaisesRegex(ValueError, "unsupported"):
            resolve_upload_vertical(self._row(Vertical="Novel service"), {}, "place:unknown")

    def test_override_does_not_rescue_unsupported_stored_vertical(self) -> None:
        from prepare_airtable_corpus import resolve_upload_vertical

        with self.assertRaisesRegex(ValueError, "unsupported"):
            resolve_upload_vertical(
                self._row(Vertical="Novel service"),
                {"place:unknown": "Hardscaping"},
                "place:unknown",
            )

    def test_override_does_not_bypass_malformed_service_evidence(self) -> None:
        from prepare_airtable_corpus import resolve_upload_vertical

        with self.assertRaisesRegex(ValueError, "malformed Raw Evidence"):
            resolve_upload_vertical(
                self._row(Vertical="service", **{"Raw Evidence": "not json"}),
                {"place:unknown": "Hardscaping"},
                "place:unknown",
            )

    def test_override_on_resolved_selected_row_is_unused(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_csv(Path(temporary), "source.csv", [self._row()], 1)
            with self.assertRaisesRegex(ValueError, "unused vertical overrides"):
                prepare_corpus([path], {"place:place-send-1": "Roofing"})

    def test_preparation_only_changes_vertical_and_machine_owned_verdict_fields(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        operational = {
            "Status": "Follow-Up",
            "Last Contacted": "2026-08-10",
            "Next Follow-Up": "2026-08-14",
            "Assigned Setter": "Lou",
            "Outreach Channel": "Phone",
        }
        source_row = self._row(
            Vertical="Masonry / Hardscaping",
            Verdict="reject",
            **operational,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_csv(Path(temporary), "source.csv", [source_row], 1)
            _, rows, report = prepare_corpus([path], {})

        self.assertEqual(len(rows), 1)
        output = rows[0]
        allowed_changes = {"Vertical", *VERDICT_REFRESH_FIELDS}
        changed = {key for key in set(source_row) | set(output) if source_row.get(key, "") != output.get(key, "")}
        self.assertTrue(changed <= allowed_changes)
        self.assertEqual(output["Vertical"], "hardscaping")
        self.assertEqual(output["Verdict"], "send")
        for field, value in operational.items():
            self.assertEqual(output[field], value)
        self.assertEqual(report["verdict_counts"], {"send": 1})
        self.assertTrue(all(field in allowed_changes or source_row.get(field, "") == output.get(field, "") for field in set(source_row) | set(output)))

    def test_source_violation_counts_measure_the_input_not_the_refreshed_output(
        self,
    ) -> None:
        """The counters have to be able to reach a non-zero value.

        They were previously computed on the row `refresh_row` returned, which
        that function has already validated and raised on — so both were
        structurally always zero and the report field asserted nothing. Pinning
        a stale row against an already-refreshed one is what keeps the signal
        honest: the stale corpus must count, the clean corpus must not.
        """
        from prepare_airtable_corpus import prepare_corpus
        from refresh_verdicts import refresh_row

        stale = self._row(Verdict="reject")
        refreshed = {
            key: str(value)
            for key, value in refresh_row(self._row(Vertical="hardscaping")).items()
        }

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stale_report = prepare_corpus(
                [self._write_csv(directory, "stale.csv", [stale], 1)], {}
            )[2]
            refreshed_report = prepare_corpus(
                [self._write_csv(directory, "fresh.csv", [refreshed], 2)], {}
            )[2]

        # A stored `reject` on a row the current gate sends breaks the send
        # biconditional on both `Sendable` and `Verdict`, and strands the tier.
        self.assertEqual(stale_report["source_biconditional_violations"], 2)
        self.assertEqual(stale_report["source_verdict_tier_violations"], 1)

        self.assertEqual(refreshed_report["source_biconditional_violations"], 0)
        self.assertEqual(refreshed_report["source_verdict_tier_violations"], 0)

        # Either way the corpus itself lands on the current authority.
        self.assertEqual(stale_report["verdict_counts"], {"send": 1})
        self.assertEqual(refreshed_report["verdict_counts"], {"send": 1})

    def test_preparation_rejects_unused_or_invalid_vertical_overrides(self) -> None:
        from prepare_airtable_corpus import prepare_corpus

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_csv(Path(temporary), "source.csv", [self._row()], 1)
            with self.assertRaisesRegex(ValueError, "unused"):
                prepare_corpus([path], {"place:unused": "Hardscaping"})
            with self.assertRaisesRegex(ValueError, "supported Vertical"):
                prepare_corpus([path], {"place:place-send-1": "Novel service"})

    def test_expected_count_mismatch_refuses_without_replacing_output(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_csv(directory, "source.csv", [self._row()], 1)
            output = directory / "corpus.csv"
            output.write_text("existing output\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--source",
                        str(source),
                        "--output",
                        str(output),
                        "--expect-output-rows",
                        "2",
                    ]
                )
            output_contents = output.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 2)
        self.assertEqual(output_contents, "existing output\n")
        self.assertIn("expect-output-rows", stderr.getvalue())

    def test_malformed_source_refuses_without_replacing_existing_output(self) -> None:
        from prepare_airtable_corpus import main

        for kind in (
            "blank header",
            "duplicate header",
            "unexpected extra cells",
            "missing cells",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                source = directory / "source.csv"
                self._write_malformed_csv(source, kind)
                output = directory / "corpus.csv"
                output.write_bytes(b"existing output\n")
                before = output.read_bytes()
                stderr = io.StringIO()

                with redirect_stderr(stderr):
                    exit_code = main(
                        ["--source", str(source), "--output", str(output)]
                    )

                self.assertEqual(exit_code, 2)
                self.assertEqual(output.read_bytes(), before)
                self.assertIn(kind, stderr.getvalue())

    def test_report_cannot_replace_a_source_or_existing_output(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_csv(directory, "source.csv", [self._row()], 1)
            source_before = source.read_bytes()
            output = directory / "corpus.csv"
            output.write_bytes(b"existing output\n")
            output_before = output.read_bytes()
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--source",
                        str(source),
                        "--output",
                        str(output),
                        "--report",
                        str(source),
                    ]
                )

            source_after = source.read_bytes()
            output_after = output.read_bytes()

        self.assertEqual(exit_code, 2)
        self.assertEqual(source_after, source_before)
        self.assertEqual(output_after, output_before)
        self.assertIn("path collision", stderr.getvalue())

    def test_report_cannot_alias_output_and_preserves_existing_bytes(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_csv(directory, "source.csv", [self._row()], 1)
            output = directory / "corpus.csv"
            output.write_bytes(b"existing output\n")
            before = output.read_bytes()
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--source",
                        str(source),
                        "--output",
                        str(output),
                        "--report",
                        str(output),
                    ]
                )

            after = output.read_bytes()

        self.assertEqual(exit_code, 2)
        self.assertEqual(after, before)
        self.assertIn("path collision", stderr.getvalue())

    def test_support_file_cannot_alias_output_and_preserves_existing_bytes(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_csv(directory, "source.csv", [self._row()], 1)
            overrides_and_output = directory / "vertical-overrides.json"
            overrides_and_output.write_text("{}\n", encoding="utf-8")
            before = overrides_and_output.read_bytes()
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--source",
                        str(source),
                        "--vertical-overrides",
                        str(overrides_and_output),
                        "--output",
                        str(overrides_and_output),
                    ]
                )

            after = overrides_and_output.read_bytes()

        self.assertEqual(exit_code, 2)
        self.assertEqual(after, before)
        self.assertIn("path collision", stderr.getvalue())

    def test_cli_report_redacts_sensitive_source_values_and_flattens_sources(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = self._write_csv(directory, "first.csv", [self._row()], 1)
            second = self._write_csv(directory, "second.csv", [self._row(**{"Place Key": "other"})], 2)
            third = self._write_csv(directory, "third.csv", [self._row(**{"Place Key": "third"})], 3)
            output = directory / "corpus.csv"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(["--source", str(first), str(second), "--source", str(third), "--output", str(output)]),
                    0,
                )

        report = json.loads(stdout.getvalue())
        rendered = json.dumps(report)
        self.assertEqual(report["source_file_count"], 3)
        self.assertNotIn("Lorain Garage Door Repair Co", rendered)
        self.assertNotIn("4405550142", rendered)
        self.assertNotIn("place:place-send-1", rendered)

    def test_cli_excludes_the_newest_selected_key_and_reports_only_its_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            older = self._write_csv(
                directory,
                "older.csv",
                [self._row(**{"Place Key": "excluded", "Business Name": "Older SBR"})],
                1,
            )
            newer = self._write_csv(
                directory,
                "newer.csv",
                [self._row(**{"Place Key": "excluded", "Business Name": "Newest SBR"})],
                2,
            )
            exclusions = directory / "excluded-upsert-keys.json"
            exclusions.write_text(json.dumps(["place:excluded"]), encoding="utf-8")
            output = directory / "corpus.csv"
            report_path = directory / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "prepare_airtable_corpus.py",
                    "--source",
                    str(older),
                    str(newer),
                    "--excluded-upsert-keys",
                    str(exclusions),
                    "--output",
                    str(output),
                    "--report",
                    str(report_path),
                    "--expect-excluded-upsert",
                    "1",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )

            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            if output.exists():
                with output.open(newline="", encoding="utf-8") as handle:
                    output_rows = list(csv.DictReader(handle))
            else:
                output_rows = []

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output_rows, [])
        self.assertEqual(report["source_row_count"], 2)
        self.assertEqual(report["overwritten_rows"], 1)
        self.assertEqual(report["excluded_upsert_count"], 1)
        self.assertEqual(
            report["excluded_upsert_hashes"],
            ["86a773cfab673d6ad307b8f87c927227a8441fcd2f80ec216d231eb694c52503"],
        )
        self.assertNotIn("place:excluded", json.dumps(report))

    def test_cli_refuses_an_exclusion_that_matches_no_selected_upsert_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_csv(directory, "source.csv", [self._row(**{"Place Key": "kept"})], 1)
            exclusions = directory / "excluded-upsert-keys.json"
            exclusions.write_text(json.dumps(["place:unused"]), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "prepare_airtable_corpus.py",
                    "--source",
                    str(source),
                    "--excluded-upsert-keys",
                    str(exclusions),
                    "--output",
                    str(directory / "corpus.csv"),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("unused excluded upsert keys: 1", completed.stderr)


class PrepareAirtableCorpusSuppressionTests(unittest.TestCase):
    """The corpus builder is the last stage before `upload_airtable.py`.

    It re-derives `Verdict` from stored CSV rows and never re-enriches, so
    without the live ledger a lead that opted out after it was enriched is
    rebuilt as `send` and pushed back into the base. That is the 2026-08-11
    condition, and this is the gate that closes it.
    """

    def _row(self, **changes: str) -> dict[str, str]:
        return {**SENDABLE_ROW, "Vertical": "Hardscaping", **changes}

    def _write_source(self, directory: Path, rows: list[dict[str, str]]) -> Path:
        path = directory / "scored.csv"
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_it_refuses_to_build_with_no_ledger_configured(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_source(directory, [self._row()])
            output = directory / "corpus.csv"
            stderr = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
                self.assertEqual(main(["--source", str(source), "--output", str(output)]), 1)
            self.assertFalse(output.exists())
            self.assertIn("[ERROR]", stderr.getvalue())
            self.assertIn("suppression source", stderr.getvalue())

    def test_an_opted_out_lead_is_not_rebuilt_to_send(self) -> None:
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            row = self._row()
            source = self._write_source(directory, [row])
            output = directory / "corpus.csv"
            ledger = directory / "suppression.jsonl"
            ledger.write_text(
                json.dumps({"event": "opt_out", "phone": row["Phone"]}) + "\n", encoding="utf-8"
            )
            stdout = io.StringIO()
            with patch.dict(os.environ, {"SUPPRESSION_SOURCE": str(ledger)}), redirect_stdout(stdout):
                self.assertEqual(main(["--source", str(source), "--output", str(output)]), 0)

            report = json.loads(stdout.getvalue())
            self.assertEqual(report["suppressed_rows"], 1)
            self.assertEqual(report["verdict_counts"].get("send", 0), 0)

            with output.open(newline="", encoding="utf-8") as handle:
                written = list(csv.DictReader(handle))
            self.assertEqual(len(written), 1)
            self.assertNotEqual(written[0]["Verdict"], "send")
            self.assertIn(DNC_SUPPRESSED_REASON, written[0]["Skip Reason"])

    def test_the_same_lead_is_sendable_when_the_ledger_is_empty(self) -> None:
        """The control. Without it the test above passes for any reason at all."""
        from prepare_airtable_corpus import main

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._write_source(directory, [self._row()])
            output = directory / "corpus.csv"
            ledger = directory / "suppression.jsonl"
            ledger.write_text("", encoding="utf-8")
            stdout = io.StringIO()
            with patch.dict(os.environ, {"SUPPRESSION_SOURCE": str(ledger)}), redirect_stdout(stdout):
                self.assertEqual(main(["--source", str(source), "--output", str(output)]), 0)

            report = json.loads(stdout.getvalue())
            self.assertEqual(report["suppressed_rows"], 0)
            self.assertEqual(report["verdict_counts"].get("send", 0), 1)


if __name__ == "__main__":
    unittest.main()
