from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from airtable_schema_cutover import (
    LEGACY_FIELD_PAIRS,
    backup_records,
    fetch_records,
    prepare_cutover,
    verify_cutover,
    write_json_atomic,
)


def schema_with_all_pairs() -> dict:
    names = ["Place Key", *LEGACY_FIELD_PAIRS, *LEGACY_FIELD_PAIRS.values()]
    return {
        "fields": [
            {"id": f"fld{index}", "name": name}
            for index, name in enumerate(dict.fromkeys(names))
        ]
    }


def schema_without_legacy_fields() -> dict:
    names = ["Place Key", *LEGACY_FIELD_PAIRS.values()]
    return {
        "fields": [
            {"id": f"fld{index}", "name": name}
            for index, name in enumerate(dict.fromkeys(names))
        ]
    }


class AirtableSchemaCutoverTest(unittest.TestCase):
    def test_exact_legacy_field_pairs(self) -> None:
        self.assertEqual(
            LEGACY_FIELD_PAIRS,
            {
                "Legacy Current Website Quality": "Current Website Quality",
                "Legacy Send Trigger": "Send Trigger",
                "Legacy Last Contacted": "Last Contacted",
                "Legacy Recommended Action": "Recommended Action",
                "Legacy Form Strength": "Form Strength",
                "Legacy CTA Strength": "CTA Strength",
                "Legacy Skip Reason": "Skip Reason",
            },
        )

    def test_prepare_refuses_legacy_only_or_conflicting_values(self) -> None:
        records = [
            {
                "id": "rec1",
                "fields": {
                    "Place Key": "p1",
                    "Legacy Send Trigger": "old",
                    "Send Trigger": "",
                    "Legacy CTA Strength": "weak",
                    "CTA Strength": "strong",
                },
            }
        ]
        report = prepare_cutover(schema_with_all_pairs(), records)
        self.assertFalse(report["safe_to_delete"])
        self.assertEqual(report["legacy_only"]["Legacy Send Trigger"], ["rec1"])
        self.assertEqual(report["conflicts"]["Legacy CTA Strength"], ["rec1"])

    def test_last_contacted_iso_timestamp_equals_same_date(self) -> None:
        records = [
            {
                "id": "rec1",
                "fields": {
                    "Legacy Last Contacted": "2026-06-24T17:27:00.000Z",
                    "Last Contacted": "2026-06-24",
                },
            }
        ]
        self.assertTrue(prepare_cutover(schema_with_all_pairs(), records)["safe_to_delete"])

    def test_verify_requires_count_canonical_presence_and_legacy_absence(self) -> None:
        snapshot = {"record_count": 132}
        errors = verify_cutover(snapshot, schema_without_legacy_fields(), 131)
        self.assertIn("record count changed: expected 132, found 131", errors)
        self.assertEqual(verify_cutover(snapshot, schema_without_legacy_fields(), 132), [])

    def test_backup_contains_only_identity_and_paired_values(self) -> None:
        records = [{"id": "rec1", "fields": {"Place Key": "p1", "Legacy Send Trigger": "old", "Secret": "omit"}}]
        backup = backup_records(records)
        self.assertEqual(backup["records"][0]["record_id"], "rec1")
        self.assertEqual(backup["records"][0]["Place Key"], "p1")
        self.assertNotIn("Secret", backup["records"][0])

    def test_fetch_records_uses_get_and_paginates(self) -> None:
        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {"records": [{"id": "rec1"}], "offset": "next"}
        second = MagicMock()
        second.raise_for_status.return_value = None
        second.json.return_value = {"records": [{"id": "rec2"}]}
        with patch("airtable_schema_cutover.requests.get", side_effect=[first, second]) as get:
            records = fetch_records("appBase", "Prospects", "pat", fields=["Place Key"])
        self.assertEqual([record["id"] for record in records], ["rec1", "rec2"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"][-1], ("offset", "next"))

    def test_atomic_json_writer_replaces_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text("old", encoding="utf-8")
            write_json_atomic(path, {"ok": True})
            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "ok": true\n}\n')
            self.assertFalse((path.parent / ".snapshot.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
