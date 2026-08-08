"""The scored CSV's two heavy JSON columns can live in a sidecar.

`Raw Evidence` and `Evidence Snapshot` are 51 of the scored CSV's 53 MB, which
makes the artifact the operator is supposed to review unopenable in a
spreadsheet. They can be written to a sidecar keyed by Place Key instead.

The contract these tests pin:

* the sidecar sits alongside the CSV under a name derived from it, and carries
  one record per lead keyed by Place Key;
* `read_scored_csv_rows` returns the same rows either way, so a reader that
  goes through it cannot tell which layout it was handed;
* the inline layout stays the default, because the out-of-lane readers
  (`evals/reproducibility.py`, `evals/gate_liveness.py`, `upload_airtable.py`,
  `make_packets.py`, `collect_review_signals.py`) still use bare
  `csv.DictReader` and would silently change verdicts on a slim CSV.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import score_leads
from lead_machine import sendability


def _raw_evidence(name: str) -> dict[str, object]:
    return {
        "business_name": name,
        "google": {"place_id": f"place-{name}", "rating": 4.8, "reviews": 90},
        "evidence": {"evidence_items": [{"id": "ev-1", "text": "x" * 400}]},
        "pages": [{"url": f"https://{name}.example/", "text": "y" * 2000}],
    }


def _snapshot(name: str) -> dict[str, object]:
    return {"visible_pain_score": 21, "evidence_items": [{"id": "ev-1", "source": name}]}


def _row(name: str, place_key: str) -> dict[str, object]:
    return {
        "Place Key": place_key,
        "Business Name": name,
        "Lead Priority": "A",
        "Evidence Refs": json.dumps(["ev-1", "ev-2"]),
        "Raw Evidence": json.dumps(_raw_evidence(name), ensure_ascii=False),
        "Evidence Snapshot": json.dumps(_snapshot(name), ensure_ascii=False),
        "Packet Eligible": "Yes",
    }


def _rows() -> list[dict[str, object]]:
    return [_row("alpha", "chij-alpha"), _row("beta", "chij-beta"), _row("gamma", "chij-gamma")]


class EvidenceSidecarTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_sidecar_path_is_derived_deterministically_from_the_csv(self) -> None:
        path = score_leads.evidence_sidecar_path(Path("output/scored_garage_doors.csv"))
        self.assertEqual(path, Path("output/scored_garage_doors.evidence.jsonl"))
        self.assertEqual(
            score_leads.evidence_sidecar_path(Path("output/scored_garage_doors.csv")),
            path,
        )

    def test_inline_is_the_default_layout(self) -> None:
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, _rows())

        with csv_path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        self.assertIn("Raw Evidence", header)
        self.assertIn("Evidence Snapshot", header)

    def test_inline_layout_writes_no_sidecar_and_clears_a_stale_one(self) -> None:
        # In the inline layout the sidecar would duplicate the CSV's own columns
        # byte for byte — another 52 MB on a real batch, for no reader. It must
        # not be written, and a sidecar left by an earlier sidecar-layout run
        # must not survive beside an inline CSV.
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, _rows(), inline_evidence=False)
        self.assertTrue(score_leads.evidence_sidecar_path(csv_path).exists())

        score_leads.write_scored_csv(csv_path, _rows(), inline_evidence=True)
        self.assertFalse(score_leads.evidence_sidecar_path(csv_path).exists())

    def test_sidecar_layout_round_trips_the_evidence_columns(self) -> None:
        before = _rows()
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, before, inline_evidence=False)

        with csv_path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        self.assertNotIn("Raw Evidence", header)
        self.assertNotIn("Evidence Snapshot", header)

        after = score_leads.read_scored_csv_rows(csv_path)
        self.assertEqual(len(after), len(before))
        for original, restored in zip(before, after):
            self.assertEqual(restored["Raw Evidence"], original["Raw Evidence"])
            self.assertEqual(restored["Evidence Snapshot"], original["Evidence Snapshot"])
            self.assertEqual(restored["Place Key"], original["Place Key"])

    def test_sidecar_keeps_the_main_csv_small(self) -> None:
        rows = _rows()
        inline_path = self.tmp / "inline.csv"
        slim_path = self.tmp / "slim.csv"
        score_leads.write_scored_csv(inline_path, rows)
        score_leads.write_scored_csv(slim_path, rows, inline_evidence=False)
        self.assertLess(slim_path.stat().st_size * 4, inline_path.stat().st_size)

    def test_sidecar_records_are_keyed_by_place_key(self) -> None:
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, _rows(), inline_evidence=False)

        entries = [
            json.loads(line)
            for line in score_leads.evidence_sidecar_path(csv_path)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(
            [entry["place_key"] for entry in entries],
            ["chij-alpha", "chij-beta", "chij-gamma"],
        )
        self.assertEqual(
            json.loads(entries[1]["Raw Evidence"])["business_name"],
            "beta",
        )

    def test_reader_passes_through_an_inline_csv_with_no_sidecar(self) -> None:
        # Every scored CSV already in output/ is this shape.
        rows = _rows()
        csv_path = self.tmp / "legacy.csv"
        score_leads.write_csv(csv_path, rows)
        self.assertFalse(score_leads.evidence_sidecar_path(csv_path).exists())

        after = score_leads.read_scored_csv_rows(csv_path)
        self.assertEqual(
            [row["Raw Evidence"] for row in after],
            [row["Raw Evidence"] for row in rows],
        )

    def test_inline_values_win_over_a_stale_sidecar(self) -> None:
        rows = _rows()
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, rows)
        score_leads.evidence_sidecar_path(csv_path).write_text(
            json.dumps(
                {
                    "row_index": 0,
                    "place_key": "chij-alpha",
                    "Raw Evidence": json.dumps({"business_name": "STALE"}),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        after = score_leads.read_scored_csv_rows(csv_path)
        self.assertEqual(json.loads(after[0]["Raw Evidence"])["business_name"], "alpha")

    def test_rows_without_a_place_key_still_round_trip(self) -> None:
        rows = _rows()
        rows[1]["Place Key"] = ""
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, rows, inline_evidence=False)

        after = score_leads.read_scored_csv_rows(csv_path)
        self.assertEqual(
            [row["Raw Evidence"] for row in after],
            [row["Raw Evidence"] for row in rows],
        )

    def test_duplicate_place_keys_do_not_cross_contaminate(self) -> None:
        rows = _rows()
        rows[2]["Place Key"] = rows[0]["Place Key"]
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, rows, inline_evidence=False)

        after = score_leads.read_scored_csv_rows(csv_path)
        self.assertEqual(
            [json.loads(row["Raw Evidence"])["business_name"] for row in after],
            ["alpha", "beta", "gamma"],
        )

    def test_resume_rewrite_does_not_lose_evidence(self) -> None:
        # The api-llm resume path reads the existing scored CSV and writes those
        # rows back out. A slim CSV must survive that trip intact.
        rows = _rows()
        first = self.tmp / "scored.csv"
        score_leads.write_scored_csv(first, rows, inline_evidence=False)

        resumed = score_leads.read_scored_csv_rows(first)
        second = self.tmp / "scored_again.csv"
        score_leads.write_scored_csv(second, resumed, inline_evidence=False)

        after = score_leads.read_scored_csv_rows(second)
        self.assertEqual(
            [row["Raw Evidence"] for row in after],
            [row["Raw Evidence"] for row in rows],
        )

    def test_verdict_is_identical_across_layouts(self) -> None:
        # The reproducibility and gate-liveness evals re-derive the verdict from
        # the scored row, and `sendability` falls back to `Raw Evidence` for the
        # evidence profile and the review demand band. A row restored from the
        # sidecar must grade exactly like the row that was written.
        rows = _rows()
        inline_path = self.tmp / "inline.csv"
        slim_path = self.tmp / "slim.csv"
        score_leads.write_scored_csv(inline_path, rows)
        score_leads.write_scored_csv(slim_path, rows, inline_evidence=False)

        for inline_row, slim_row in zip(
            score_leads.read_scored_csv_rows(inline_path),
            score_leads.read_scored_csv_rows(slim_path),
        ):
            self.assertEqual(
                sendability.evaluate_sendability(inline_row)["send_blockers"],
                sendability.evaluate_sendability(slim_row)["send_blockers"],
            )

    def test_layout_is_selectable_by_env(self) -> None:
        from unittest import mock

        with mock.patch.dict(score_leads.os.environ, {"LPM_SCORED_CSV_EVIDENCE": "sidecar"}):
            self.assertFalse(score_leads.scored_csv_evidence_is_inline())
        with mock.patch.dict(score_leads.os.environ, {"LPM_SCORED_CSV_EVIDENCE": "inline"}):
            self.assertTrue(score_leads.scored_csv_evidence_is_inline())
        with mock.patch.dict(score_leads.os.environ, {}, clear=True):
            self.assertTrue(score_leads.scored_csv_evidence_is_inline())

    def test_empty_row_set_leaves_no_stale_sidecar(self) -> None:
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, _rows(), inline_evidence=False)
        self.assertTrue(score_leads.evidence_sidecar_path(csv_path).exists())

        score_leads.write_scored_csv(csv_path, [], inline_evidence=False)
        self.assertEqual(score_leads.read_scored_csv_rows(csv_path), [])
        self.assertFalse(score_leads.evidence_sidecar_path(csv_path).exists())

    def test_huge_evidence_survives_the_csv_field_limit(self) -> None:
        # A real Raw Evidence blob exceeds csv's 128 KB default field limit.
        rows = _rows()
        rows[0]["Raw Evidence"] = json.dumps({"pages": [{"text": "z" * 200_000}]})
        csv_path = self.tmp / "scored.csv"
        score_leads.write_scored_csv(csv_path, rows, inline_evidence=False)

        after = score_leads.read_scored_csv_rows(csv_path)
        self.assertEqual(after[0]["Raw Evidence"], rows[0]["Raw Evidence"])


if __name__ == "__main__":
    sys.exit(unittest.main())
