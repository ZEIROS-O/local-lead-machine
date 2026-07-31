from __future__ import annotations

import csv
import importlib
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

CURRENT_SCHEMA_ROW = {
    "Score Schema Version": "2",
    "Commercial Fit State": "commercially-sendable",
    "Contact Eligibility": "contact-now",
}


def write_scored(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def scored_row(place_key: str, **overrides: str) -> dict[str, str]:
    row = {
        "Place Key": place_key,
        "Business Name": "Acme Services",
        **CURRENT_SCHEMA_ROW,
    }
    row.update(overrides)
    return row


def legacy_row(place_key: str, **overrides: str) -> dict[str, str]:
    """A pre-v2 scored row: no `Score Schema Version` column at all."""
    row = {"Place Key": place_key, "Business Name": "Acme Services"}
    row.update(overrides)
    return row


def base_record(
    record_id: str,
    place_key: str,
    *,
    name: str = "Acme Services",
    verdict: str = "No Packet",
) -> dict[str, object]:
    return {
        "id": record_id,
        "fields": {
            "Place Key": place_key,
            "Business Name": name,
            "Packet Tier": verdict,
        },
    }


class ReproducibilityEvalTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("evals.reproducibility")
        except ModuleNotFoundError:
            self.fail("evals.reproducibility must exist")

    def evaluate(
        self,
        tmp: str,
        rows_by_file: dict[str, list[dict[str, str]]],
        base_records: list[dict[str, object]],
        **kwargs: object,
    ) -> dict[str, object]:
        module = self.module()
        paths = []
        for filename, rows in rows_by_file.items():
            path = Path(tmp) / filename
            write_scored(path, rows)
            paths.append(path)
        return module.evaluate_reproducibility(paths, base_records, **kwargs)

    def fixed_verdict(self, verdict: str):
        return lambda row: verdict

    # --- the verdict recompute is the real authority, never a copy ----------

    def test_recompute_delegates_to_the_sendability_authority(self) -> None:
        module = self.module()
        from lead_machine import sendability

        calls: list[str] = []
        real_sendability = sendability.evaluate_sendability
        real_tier = sendability.evaluate_packet_tier

        def spy_sendability(row):
            calls.append("evaluate_sendability")
            return real_sendability(row)

        def spy_tier(row, result):
            calls.append("evaluate_packet_tier")
            return real_tier(row, result)

        sendability.evaluate_sendability = spy_sendability
        sendability.evaluate_packet_tier = spy_tier
        try:
            verdict = module.recompute_verdict(scored_row("place-1"))
        finally:
            sendability.evaluate_sendability = real_sendability
            sendability.evaluate_packet_tier = real_tier

        self.assertEqual(calls, ["evaluate_sendability", "evaluate_packet_tier"])
        self.assertEqual(verdict, sendability.PACKET_TIER_NONE)

    def test_module_does_not_reimplement_the_verdict(self) -> None:
        """No packet-tier literal may be spelled here; they are imported."""
        from lead_machine import sendability

        source = (REPO_ROOT / "evals" / "reproducibility.py").read_text(
            encoding="utf-8"
        )
        for tier in sorted(sendability.PACKET_TIER_CHOICES):
            self.assertNotIn(
                f'"{tier}"',
                source,
                f"{tier!r} must be imported from sendability, not spelled here",
            )

    # --- the join and its categories ---------------------------------------

    def test_reproduced_verdict_matching_the_base_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row("place-1")]},
                [base_record("rec1", "place-1", verdict="Watchlist")],
                recompute=self.fixed_verdict("Watchlist"),
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["findings"]["verdict_drift"], 0)
        self.assertEqual(report["counts"]["findings"]["stale_evidence"], 0)
        self.assertEqual(report["counts"]["matched"], 1)

    def test_current_schema_disagreement_is_verdict_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row("place-1")]},
                [base_record("rec1", "place-1", verdict="Sendable Packet")],
                recompute=self.fixed_verdict("No Packet"),
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"]["verdict_drift"],
            [
                {
                    "place_key": "place-1",
                    "business_name": "Acme Services",
                    "record_id": "rec1",
                    "base_verdict": "Sendable Packet",
                    "recomputed_verdict": "No Packet",
                    "schema_version": 2,
                    "source": "scored_a.csv:2",
                }
            ],
        )
        self.assertEqual(report["findings"]["stale_evidence"], [])

    def test_pre_schema_disagreement_is_stale_evidence_not_drift(self) -> None:
        """The two failures have different remedies, so they never share a bucket.

        A row scored before the commercial-fit columns existed cannot be
        re-derived at all; calling that a changed decision would bury a real one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [legacy_row("place-1")]},
                [base_record("rec1", "place-1", verdict="Sendable Packet")],
                recompute=self.fixed_verdict("No Packet"),
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["findings"]["verdict_drift"], [])
        self.assertEqual(
            report["findings"]["stale_evidence"],
            [
                {
                    "place_key": "place-1",
                    "business_name": "Acme Services",
                    "record_id": "rec1",
                    "base_verdict": "Sendable Packet",
                    "recomputed_verdict": "No Packet",
                    "schema_version": 0,
                    "source": "scored_a.csv:2",
                }
            ],
        )

    def test_base_row_without_evidence_is_reported_but_not_failed(self) -> None:
        """Orphans belong to the completeness eval; double-owning them would
        make two gates red for one defect."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row("place-1")]},
                [
                    base_record("rec1", "place-1"),
                    base_record("rec2", "no-evidence", name="Ghost LLC"),
                ],
                recompute=self.fixed_verdict("No Packet"),
            )

        self.assertTrue(report["ok"])
        self.assertEqual(
            report["findings"]["unverifiable"],
            [
                {
                    "place_key": "no-evidence",
                    "business_name": "Ghost LLC",
                    "record_id": "rec2",
                }
            ],
        )

    def test_current_schema_evidence_wins_over_a_legacy_duplicate(self) -> None:
        """When one lead was scored twice, only the reproducible row can judge it."""
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {
                    "scored_a.csv": [legacy_row("place-1")],
                    "scored_b.csv": [scored_row("place-1")],
                },
                [base_record("rec1", "place-1", verdict="Sendable Packet")],
                recompute=self.fixed_verdict("Sendable Packet"),
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["verdicts"]["place-1"]["schema_version"], 2)
        self.assertEqual(report["verdicts"]["place-1"]["source"], "scored_b.csv:2")

    def test_blank_scored_keys_cannot_join_and_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row(" ", **{"Business Name": "No Key LLC"})]},
                [],
                recompute=self.fixed_verdict("No Packet"),
            )

        self.assertEqual(report["counts"]["blank_scored_keys"], 1)
        self.assertEqual(report["verdicts"], {})

    # --- the snapshot diff: the gate on the step 7 collapse -----------------

    def test_snapshot_diff_is_empty_when_no_decision_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row("place-1")]},
                [],
                recompute=self.fixed_verdict("Watchlist"),
                baseline={"place-1": {"verdict": "Watchlist"}},
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["findings"]["snapshot_drift"], [])

    def test_snapshot_diff_fails_on_any_changed_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row("place-1")]},
                [],
                recompute=self.fixed_verdict("No Packet"),
                baseline={"place-1": {"verdict": "Watchlist"}},
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"]["snapshot_drift"],
            [
                {
                    "place_key": "place-1",
                    "baseline_verdict": "Watchlist",
                    "recomputed_verdict": "No Packet",
                }
            ],
        )

    def test_snapshot_diff_fails_when_a_lead_leaves_or_joins_the_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.evaluate(
                tmp,
                {"scored_a.csv": [scored_row("place-new")]},
                [],
                recompute=self.fixed_verdict("No Packet"),
                baseline={"place-gone": {"verdict": "No Packet"}},
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"]["snapshot_drift"],
            [
                {
                    "place_key": "place-gone",
                    "baseline_verdict": "No Packet",
                    "recomputed_verdict": "",
                },
                {
                    "place_key": "place-new",
                    "baseline_verdict": "",
                    "recomputed_verdict": "No Packet",
                },
            ],
        )

    # --- CLI ----------------------------------------------------------------

    def test_main_exits_non_zero_and_writes_a_snapshot(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            scored = Path(tmp) / "scored_a.csv"
            write_scored(scored, [scored_row("place-1")])
            records = Path(tmp) / "base.json"
            records.write_text(
                json.dumps([base_record("rec1", "place-1", verdict="Sendable Packet")]),
                encoding="utf-8",
            )
            snapshot = Path(tmp) / "snapshot.json"
            code = module.main(
                [
                    "--scored-file",
                    str(scored),
                    "--base-records-json",
                    str(records),
                    "--snapshot-out",
                    str(snapshot),
                ]
            )

            self.assertEqual(code, 2)
            written = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(written["place-1"]["verdict"], "No Packet")

    def test_main_reads_a_baseline_snapshot_it_wrote(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            scored = Path(tmp) / "scored_a.csv"
            write_scored(scored, [scored_row("place-1")])
            snapshot = Path(tmp) / "snapshot.json"
            first = module.main(
                [
                    "--scored-file",
                    str(scored),
                    "--base-records-json",
                    str(Path(tmp) / "empty.json"),
                    "--snapshot-out",
                    str(snapshot),
                ]
            )
            self.assertEqual(first, 2)  # the empty fixture does not exist yet

            (Path(tmp) / "empty.json").write_text("[]", encoding="utf-8")
            second = module.main(
                [
                    "--scored-file",
                    str(scored),
                    "--base-records-json",
                    str(Path(tmp) / "empty.json"),
                    "--snapshot-out",
                    str(snapshot),
                ]
            )
            self.assertEqual(second, 0)

            third = module.main(
                [
                    "--scored-file",
                    str(scored),
                    "--base-records-json",
                    str(Path(tmp) / "empty.json"),
                    "--baseline",
                    str(snapshot),
                ]
            )
            self.assertEqual(third, 0)


if __name__ == "__main__":
    unittest.main()
