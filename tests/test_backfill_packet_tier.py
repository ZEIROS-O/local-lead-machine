import csv
import json
import tempfile
import unittest
from pathlib import Path

import backfill_packet_tier as backfill


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class ResolveTiersTests(unittest.TestCase):
    def test_newest_csv_wins_and_older_disagreement_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            newest = root / "newest.csv"
            older = root / "older.csv"
            _write_csv(newest, [{"Place Key": "ChIJ1", "Packet Tier": "No Packet"}])
            _write_csv(older, [{"Place Key": "ChIJ1", "Packet Tier": "Sendable Packet"}])

            resolved = backfill.resolve_tiers_from_csvs([newest, older])

        self.assertEqual(resolved["ChIJ1"].tier, "No Packet")
        self.assertEqual(resolved["ChIJ1"].source, "newest.csv")
        # The disagreement is kept, not swallowed: a tier that changed between
        # runs is a signal about the scorer, and the operator should see it.
        self.assertEqual(resolved["ChIJ1"].conflicts, [("older.csv", "Sendable Packet")])

    def test_blank_tiers_and_unknown_labels_never_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blank = root / "blank.csv"
            bogus = root / "bogus.csv"
            good = root / "good.csv"
            _write_csv(blank, [{"Place Key": "ChIJ1", "Packet Tier": ""}])
            _write_csv(bogus, [{"Place Key": "ChIJ2", "Packet Tier": "Definitely Send It"}])
            _write_csv(good, [{"Place Key": "ChIJ1", "Packet Tier": "Watchlist"}])

            resolved = backfill.resolve_tiers_from_csvs([blank, bogus, good])

        # A blank tier must not shadow a real one from an older file, and a
        # label outside the select's options would 422 the whole batch.
        self.assertEqual(resolved["ChIJ1"].tier, "Watchlist")
        self.assertNotIn("ChIJ2", resolved)

    def test_csv_without_a_place_key_column_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no_key.csv"
            _write_csv(path, [{"Business Name": "Anon", "Packet Tier": "Sendable Packet"}])

            self.assertEqual(backfill.resolve_tiers_from_csvs([path]), {})


class PlanBackfillTests(unittest.TestCase):
    def _records(self) -> list[dict]:
        return [
            {"id": "rec1", "fields": {"Place Key": "ChIJ1", "Business Name": "Empty Co"}},
            {"id": "rec2", "fields": {"Place Key": "ChIJ2", "Business Name": "Agrees Co", "Packet Tier": "Watchlist"}},
            {"id": "rec3", "fields": {"Place Key": "ChIJ3", "Business Name": "Differs Co", "Packet Tier": "No Packet"}},
            {"id": "rec4", "fields": {"Business Name": "Keyless Co"}},
            {"id": "rec5", "fields": {"Place Key": "ChIJ9", "Business Name": "Unknown Co"}},
        ]

    def _resolved(self) -> dict:
        return {
            "ChIJ1": backfill.ResolvedTier("ChIJ1", "Sendable Packet", "a.csv"),
            "ChIJ2": backfill.ResolvedTier("ChIJ2", "Watchlist", "a.csv"),
            "ChIJ3": backfill.ResolvedTier("ChIJ3", "Sendable Packet", "a.csv"),
        }

    def test_only_empty_tiers_are_written_by_default(self) -> None:
        plan = backfill.plan_backfill(self._records(), self._resolved())

        self.assertEqual([entry["record_id"] for entry in plan["write"]], ["rec1"])
        self.assertEqual([entry["record_id"] for entry in plan["already_set"]], ["rec2"])
        self.assertEqual([entry["record_id"] for entry in plan["differs"]], ["rec3"])
        self.assertEqual([entry["record_id"] for entry in plan["no_place_key"]], ["rec4"])
        self.assertEqual([entry["record_id"] for entry in plan["unresolved"]], ["rec5"])

    def test_overwrite_conflicts_promotes_differing_records_to_writes(self) -> None:
        plan = backfill.plan_backfill(self._records(), self._resolved(), overwrite_conflicts=True)

        self.assertEqual(sorted(entry["record_id"] for entry in plan["write"]), ["rec1", "rec3"])
        self.assertEqual([entry["record_id"] for entry in plan["differs"]], ["rec3"])

    def test_place_key_allowlist_restricts_writes_to_verified_keys(self) -> None:
        """Most stored tiers predate the commercial-fit gate and are contradicted
        by the current scorer, so the tool must be able to apply only the subset
        that has actually been re-verified."""
        plan = backfill.plan_backfill(self._records(), self._resolved(), only_place_keys=["ChIJ1"])

        self.assertEqual([entry["record_id"] for entry in plan["write"]], ["rec1"])
        self.assertEqual(
            sorted(entry["record_id"] for entry in plan["filtered_out"]),
            ["rec2", "rec3", "rec5"],
        )

    def test_allowlist_still_refuses_a_keyless_record(self) -> None:
        plan = backfill.plan_backfill(self._records(), self._resolved(), only_place_keys=["ChIJ1", ""])

        self.assertEqual([entry["record_id"] for entry in plan["no_place_key"]], ["rec4"])
        self.assertNotIn("rec4", {entry["record_id"] for entry in plan["write"]})

    def test_a_record_without_a_place_key_is_never_matched_by_anything_else(self) -> None:
        """Fuzzy matching is how a write lands on the wrong business."""
        plan = backfill.plan_backfill(self._records(), self._resolved())

        written_ids = {entry["record_id"] for entry in plan["write"]}
        self.assertNotIn("rec4", written_ids)


class ApplyBackfillTests(unittest.TestCase):
    class _Session:
        def __init__(self, statuses: list[int] | None = None) -> None:
            self.calls: list[dict] = []
            self.statuses = statuses or []
            self.slept: list[float] = []

        def patch(self, url, headers=None, json=None, timeout=None):  # noqa: A002
            self.calls.append(json)
            status = self.statuses.pop(0) if self.statuses else 200

            class _Response:
                status_code = status
                text = "error"

                @staticmethod
                def json():
                    return {"records": []}

            return _Response()

    def _writes(self, count: int) -> list[dict]:
        return [
            {"record_id": f"rec{index}", "place_key": f"ChIJ{index}", "tier": "Sendable Packet", "source": "a.csv"}
            for index in range(count)
        ]

    def test_writes_are_batched_at_ten_and_only_touch_packet_tier(self) -> None:
        session = self.__class__._Session()

        applied = backfill.apply_backfill(
            "base", "table", "token", self._writes(23), session=session, sleep_seconds=0
        )

        self.assertEqual(applied, 23)
        self.assertEqual([len(call["records"]) for call in session.calls], [10, 10, 3])
        for call in session.calls:
            for record in call["records"]:
                self.assertEqual(list(record["fields"]), ["Packet Tier"])

    def test_receipts_record_every_applied_write(self) -> None:
        session = self.__class__._Session()
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp) / "receipts.jsonl"

            backfill.apply_backfill(
                "base", "table", "token", self._writes(12),
                receipts_path=receipts, session=session, sleep_seconds=0,
            )
            lines = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[0]["place_key"], "ChIJ0")
        self.assertEqual(lines[0]["tier"], "Sendable Packet")

    def test_transient_status_is_retried_and_permanent_status_is_not(self) -> None:
        transient = self.__class__._Session(statuses=[429, 200])
        backfill.apply_backfill(
            "base", "table", "token", self._writes(1),
            session=transient, sleep_seconds=0, backoff_seconds=0,
        )
        self.assertEqual(len(transient.calls), 2)

        permanent = self.__class__._Session(statuses=[422])
        with self.assertRaises(RuntimeError):
            backfill.apply_backfill(
                "base", "table", "token", self._writes(1),
                session=permanent, sleep_seconds=0, backoff_seconds=0,
            )
        self.assertEqual(len(permanent.calls), 1)

    def test_a_failure_midway_leaves_receipts_for_what_already_landed(self) -> None:
        """An aborted run must say what it did, not leave the base unknown."""
        session = self.__class__._Session(statuses=[200, 422])
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp) / "receipts.jsonl"
            with self.assertRaises(RuntimeError):
                backfill.apply_backfill(
                    "base", "table", "token", self._writes(15),
                    receipts_path=receipts, session=session, sleep_seconds=0, backoff_seconds=0,
                )
            lines = receipts.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 10)


if __name__ == "__main__":
    unittest.main()
