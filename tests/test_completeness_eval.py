from __future__ import annotations

import ast
import csv
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_scored(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["Place Key", "Business Name", "City", "Packet Tier"]
    if any("Verdict" in row for row in rows):
        fieldnames.append("Verdict")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def base_record(
    record_id: str,
    place_key: str,
    *,
    name: str = "Acme Services",
    verdict: str = "reject",
) -> dict[str, object]:
    # The base side speaks verdicts; the scored CSVs above still speak tiers.
    return {
        "id": record_id,
        "fields": {
            "Place Key": place_key,
            "Business Name": name,
            "Verdict": verdict,
        },
    }


class CompletenessVocabularyTests(unittest.TestCase):
    """The base side and the scored-CSV side no longer name the same column.

    `Verdict` is what the base stores after the verdict cutover, and step 4
    deletes `Packet Tier` outright — a base read still naming it 422s. The
    scored CSVs are unchanged and still carry `Packet Tier`, so the corpus
    conflict check has to keep reading that one.
    """

    def module(self):
        return importlib.import_module("evals.completeness")

    # The base-column assertion that lived here is now discovered for every eval
    # by tests/test_eval_verdict_field_contract.py — it was present in two of the
    # three base-reading evals and absent from the one that broke.

    def test_corpus_conflicts_still_read_the_scored_csv_column(self) -> None:
        module = self.module()
        conflicts = module._corpus_conflicts(
            {
                "place-1": [
                    {"_source": "a.csv:2", "Packet Tier": "Sendable Packet"},
                    {"_source": "b.csv:2", "Packet Tier": "No Packet"},
                ]
            },
            module.SCORED_VERDICT_FIELD,
        )
        self.assertEqual(len(conflicts), 1)
        self.assertIn("Packet Tier", conflicts[0]["fields"])


class CompletenessEvalTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("evals.completeness")
        except ModuleNotFoundError:
            self.fail("evals.completeness must exist")

    def evaluate(
        self,
        tmp: str,
        rows_by_file: dict[str, list[dict[str, str]]],
        base_records: list[dict[str, object]],
    ) -> dict[str, object]:
        module = self.module()
        paths = []
        for filename, rows in rows_by_file.items():
            path = Path(tmp) / filename
            write_scored(path, rows)
            paths.append(path)
        return module.evaluate_completeness(paths, base_records)

    def test_complete_one_to_one_mirror_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_a.csv": [
                        {
                            "Place Key": "place-1",
                            "Business Name": "Acme Services",
                            "City": "Cleveland",
                            "Packet Tier": "No Packet",
                        }
                    ]
                },
                [base_record("rec1", "place-1")],
            )

        self.assertTrue(report["ok"])
        self.assertEqual(
            report["counts"],
            {
                "scored_files": 1,
                "scored_rows": 1,
                "distinct_scored_leads": 1,
                "base_rows": 1,
                "findings": {
                    "base_orphans": 0,
                    "blank_base_keys": 0,
                    "blank_base_verdicts": 0,
                    "blank_scored_keys": 0,
                    "corpus_conflicts": 0,
                    "duplicate_base_keys": 0,
                    "scored_orphans": 0,
                },
                # Counted apart from `findings` because it never fails the run.
                "reported": {"pruned_rejects": 0},
            },
        )

    def test_reports_scored_and_base_orphans_with_repair_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_a.csv": [
                        {
                            "Place Key": "scored-only",
                            "Business Name": "Scored Only LLC",
                            "City": "Akron",
                            "Packet Tier": "Watchlist",
                        }
                    ]
                },
                [base_record("rec-base", "base-only", name="Base Only LLC")],
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"]["scored_orphans"],
            [
                {
                    "place_key": "scored-only",
                    "business_name": "Scored Only LLC",
                    "sources": ["scored_a.csv:2"],
                    # Blank because this fixture carries no `Verdict` column. A
                    # lead with no recorded verdict is not provably a pruned
                    # reject, so it stays an orphan — the check fails toward
                    # reporting.
                    "verdicts": [],
                }
            ],
        )
        self.assertEqual(
            report["findings"]["base_orphans"],
            [
                {
                    "place_key": "base-only",
                    "business_name": "Base Only LLC",
                    "record_id": "rec-base",
                }
            ],
        )

    def test_reports_blank_and_duplicate_base_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_a.csv": [
                        {
                            "Place Key": "place-1",
                            "Business Name": "Acme Services",
                            "City": "Cleveland",
                            "Packet Tier": "No Packet",
                        }
                    ]
                },
                [
                    base_record("rec-blank", "", name="Blank Key LLC"),
                    base_record("rec-a", "place-1"),
                    base_record("rec-b", "place-1", name="Acme Services Duplicate"),
                ],
            )

        self.assertEqual(
            report["findings"]["blank_base_keys"],
            [{"business_name": "Blank Key LLC", "record_id": "rec-blank"}],
        )
        self.assertEqual(
            report["findings"]["duplicate_base_keys"],
            [
                {
                    "place_key": "place-1",
                    "records": [
                        {"business_name": "Acme Services", "record_id": "rec-a"},
                        {
                            "business_name": "Acme Services Duplicate",
                            "record_id": "rec-b",
                        },
                    ],
                }
            ],
        )

    def test_reports_blank_verdict_for_every_base_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_a.csv": [
                        {
                            "Place Key": "place-1",
                            "Business Name": "Acme Services",
                            "City": "Cleveland",
                            "Packet Tier": "",
                        }
                    ]
                },
                [base_record("rec1", "place-1", verdict=" ")],
            )

        self.assertEqual(
            report["findings"]["blank_base_verdicts"],
            [
                {
                    "place_key": "place-1",
                    "business_name": "Acme Services",
                    "record_id": "rec1",
                }
            ],
        )

    def test_reports_blank_scored_key_with_source_row_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_blank.csv": [
                        {
                            "Place Key": " ",
                            "Business Name": "Missing Key LLC",
                            "City": "Columbus",
                            "Packet Tier": "No Packet",
                        }
                    ]
                },
                [],
            )

        self.assertEqual(
            report["findings"]["blank_scored_keys"],
            [
                {
                    "business_name": "Missing Key LLC",
                    "city": "Columbus",
                    "source": "scored_blank.csv:2",
                }
            ],
        )

    def test_identical_snapshots_collapse_to_one_scored_lead(self) -> None:
        row = {
            "Place Key": "place-1",
            "Business Name": "Acme Services",
            "City": "Cleveland",
            "Packet Tier": "No Packet",
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_old.csv": [row], "scored_new.csv": [row]},
                [base_record("rec1", "place-1")],
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["distinct_scored_leads"], 1)
        self.assertEqual(report["counts"]["scored_rows"], 2)
        self.assertEqual(report["findings"]["duplicate_base_keys"], [])

    def test_disagreeing_snapshots_report_each_conflicting_field_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_a.csv": [
                        {
                            "Place Key": "place-1",
                            "Business Name": "Acme Services",
                            "City": "Cleveland",
                            "Packet Tier": "Watchlist",
                        }
                    ],
                    "scored_b.csv": [
                        {
                            "Place Key": "place-1",
                            "Business Name": "Acme Service Co",
                            "City": "Cleveland",
                            "Packet Tier": "No Packet",
                        }
                    ],
                },
                [base_record("rec1", "place-1")],
            )

        self.assertEqual(
            report["findings"]["corpus_conflicts"],
            [
                {
                    "place_key": "place-1",
                    "fields": {
                        "Business Name": [
                            {"source": "scored_a.csv:2", "value": "Acme Services"},
                            {"source": "scored_b.csv:2", "value": "Acme Service Co"},
                        ],
                        "Packet Tier": [
                            {"source": "scored_a.csv:2", "value": "Watchlist"},
                            {"source": "scored_b.csv:2", "value": "No Packet"},
                        ],
                    },
                }
            ],
        )

    def test_fetch_paginates_with_get_and_requests_only_eval_fields(self) -> None:
        module = self.module()
        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "records": [base_record("rec1", "place-1")],
            "offset": "next-page",
        }
        second = MagicMock()
        second.raise_for_status.return_value = None
        second.json.return_value = {"records": [base_record("rec2", "place-2")]}

        with patch(
            "evals.completeness.requests.get", side_effect=[first, second]
        ) as get:
            records = module.fetch_base_records(
                "appBase", "Prospects", "pat", verdict_field="Packet Tier"
            )

        self.assertEqual([record["id"] for record in records], ["rec1", "rec2"])
        self.assertEqual(get.call_count, 2)
        first_params = get.call_args_list[0].kwargs["params"]
        self.assertEqual(
            [value for key, value in first_params if key == "fields[]"],
            ["Place Key", "Business Name", "Packet Tier"],
        )
        self.assertNotIn(("offset", "next-page"), first_params)
        self.assertIn(
            ("offset", "next-page"), get.call_args_list[1].kwargs["params"]
        )

    def test_source_contains_no_airtable_write_http_methods(self) -> None:
        self.module()
        path = REPO_ROOT / "evals" / "completeness.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called_request_methods = {
            node.func.attr.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
        }
        self.assertEqual(called_request_methods, {"get"})
        self.assertNotIn("typecast", path.read_text(encoding="utf-8").casefold())

    def test_run_can_use_loaded_records_without_network(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            write_scored(
                Path(tmp) / "scored_fixture.csv",
                [
                    {
                        "Place Key": "place-1",
                        "Business Name": "Acme Services",
                        "City": "Cleveland",
                        "Packet Tier": "No Packet",
                    }
                ],
            )
            with patch("evals.completeness.requests.get") as get:
                code, report = module.run_eval(
                    output_dir=Path(tmp),
                    pattern="scored_*.csv",
                    base_records=[base_record("rec1", "place-1")],
                )

        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        get.assert_not_called()

    def test_cli_fixture_records_emit_stable_json_and_nonzero_on_findings(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_scored(
                root / "scored_fixture.csv",
                [
                    {
                        "Place Key": "place-1",
                        "Business Name": "Acme Services",
                        "City": "Cleveland",
                        "Packet Tier": "No Packet",
                    }
                ],
            )
            records_path = root / "records.json"
            records_path.write_text("[]\n", encoding="utf-8")
            with patch("builtins.print") as output:
                code = module.main(
                    [
                        "--output-dir",
                        str(root),
                        # An explicit glob overrides evals/active_corpora.json,
                        # which names the repo's real corpora and would not
                        # resolve against a fixture directory.
                        "--glob",
                        "scored_*.csv",
                        "--base-records-json",
                        str(records_path),
                    ]
                )

        self.assertEqual(code, 2)
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual(rendered["counts"]["findings"]["scored_orphans"], 1)

    def test_cli_redacts_request_exception_details(self) -> None:
        module = self.module()
        unsafe = (
            "GET https://api.airtable.com/v0/appSecret/Prospects failed "
            "Authorization: Bearer patSecret"
        )
        with patch(
            "evals.completeness.run_eval",
            side_effect=requests.ConnectionError(unsafe),
        ), patch("builtins.print") as output:
            code = module.main([])

        self.assertEqual(code, 2)
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual(
            rendered,
            {
                "ok": False,
                "error": "Airtable GET failed (ConnectionError)",
            },
        )
        self.assertNotIn("appSecret", output.call_args.args[0])
        self.assertNotIn("patSecret", output.call_args.args[0])


class PrunedRejectTests(unittest.TestCase):
    """Reject-verdict leads are pruned from the CRM on purpose.

    `scored_orphans = scored_keys - base_keys` therefore counted every pruned
    reject as a finding — 351 of the 462 it reported on 2026-08-12 — so the check
    could only go green by uploading leads the operator deliberately removes.
    """

    def module(self):
        return importlib.import_module("evals.completeness")

    def evaluate(self, tmp: str, rows: list[dict[str, str]]) -> dict:
        path = Path(tmp) / "scored_a.csv"
        write_scored(path, rows)
        return self.module().evaluate_completeness([path], [])

    def test_a_pruned_reject_is_reported_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [
                    {
                        "Place Key": "pruned",
                        "Business Name": "Pruned Reject LLC",
                        "City": "Akron",
                        "Packet Tier": "No Packet",
                        "Verdict": "reject",
                    }
                ],
            )

        self.assertEqual(report["findings"]["scored_orphans"], [])
        self.assertEqual(report["counts"]["reported"]["pruned_rejects"], 1)
        self.assertEqual(report["counts"]["findings"]["scored_orphans"], 0)
        self.assertTrue(report["ok"])

    def test_an_actionable_lead_missing_from_the_base_still_fails(self) -> None:
        """The invariant the narrowing is meant to preserve."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [
                    {
                        "Place Key": "actionable",
                        "Business Name": "Sendable LLC",
                        "City": "Akron",
                        "Packet Tier": "Sendable Packet",
                        "Verdict": "send",
                    }
                ],
            )

        self.assertEqual(report["counts"]["findings"]["scored_orphans"], 1)
        self.assertEqual(report["counts"]["reported"]["pruned_rejects"], 0)
        self.assertFalse(report["ok"])

    def test_a_lead_rejected_in_one_pass_and_sendable_in_another_is_an_orphan(self) -> None:
        """Fails toward reporting: any non-reject scoring keeps it a finding."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                [
                    {
                        "Place Key": "mixed",
                        "Business Name": "Mixed LLC",
                        "City": "Akron",
                        "Packet Tier": "No Packet",
                        "Verdict": "reject",
                    },
                    {
                        "Place Key": "mixed",
                        "Business Name": "Mixed LLC",
                        "City": "Akron",
                        "Packet Tier": "Sendable Packet",
                        "Verdict": "send",
                    },
                ],
            )

        self.assertEqual(report["counts"]["findings"]["scored_orphans"], 1)
        self.assertEqual(report["counts"]["reported"]["pruned_rejects"], 0)


if __name__ == "__main__":
    unittest.main()
