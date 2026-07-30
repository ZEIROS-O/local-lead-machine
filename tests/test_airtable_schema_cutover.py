from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from airtable_schema_cutover import (
    COMPUTED_FIELD_TYPES,
    DEFAULT_CUTOVER,
    LEGACY_FIELD_PAIRS,
    LEGACY_ORPHAN_FIELDS,
    FieldCutover,
    all_legacy_fields,
    backup_records,
    computed_field_names,
    cutover_from_snapshot,
    fetch_records,
    load_cutover_config,
    load_fields_config,
    main,
    parse_args,
    parse_field_list,
    prepare_cutover,
    resolve_cutover,
    run_prepare,
    run_verify,
    verify_cutover,
    write_json_atomic,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "airtable_cutover_fields_2026-07.json"


def json_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def schema_with_all_pairs() -> dict:
    names = ["Place Key", *all_legacy_fields(), *LEGACY_FIELD_PAIRS.values()]
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
                "Legacy Send Trigger": "Send Trigger",
                "Legacy Last Contacted": "Last Contacted",
                "Legacy Skip Reason": "Skip Reason",
            },
        )

    def test_orphans_are_legacy_fields_with_no_canonical_half(self) -> None:
        # Their canonical columns were demoted off the Airtable surface, so a
        # pair mapping would point at a column that no longer exists.
        self.assertEqual(
            LEGACY_ORPHAN_FIELDS,
            (
                "Legacy Current Website Quality",
                "Legacy CTA Strength",
                "Legacy Recommended Action",
                "Legacy Form Strength",
            ),
        )
        self.assertTrue(set(LEGACY_ORPHAN_FIELDS).isdisjoint(LEGACY_FIELD_PAIRS))
        self.assertEqual(
            set(all_legacy_fields()), set(LEGACY_FIELD_PAIRS) | set(LEGACY_ORPHAN_FIELDS)
        )

    def test_prepare_refuses_legacy_only_or_conflicting_values(self) -> None:
        records = [
            {
                "id": "rec1",
                "fields": {
                    "Place Key": "p1",
                    "Legacy Send Trigger": "old",
                    "Send Trigger": "",
                    "Legacy Skip Reason": "old reason",
                    "Skip Reason": "new reason",
                },
            }
        ]
        report = prepare_cutover(schema_with_all_pairs(), records)
        self.assertFalse(report["safe_to_delete"])
        self.assertEqual(report["legacy_only"]["Legacy Send Trigger"], ["rec1"])
        self.assertEqual(report["conflicts"]["Legacy Skip Reason"], ["rec1"])

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

    def test_orphan_values_are_counted_but_never_block_the_cutover(self) -> None:
        records = [
            {"id": "rec1", "fields": {"Legacy CTA Strength": "weak"}},
            {"id": "rec2", "fields": {"Legacy Current Website Quality": "Poor"}},
            {"id": "rec3", "fields": {}},
        ]
        report = prepare_cutover(schema_with_all_pairs(), records)
        # Every orphan value is legacy-only by definition; blocking on that would
        # make them undeletable forever. Report the exposure, do not refuse.
        self.assertTrue(report["safe_to_delete"])
        self.assertEqual(
            report["orphan_populated_counts"],
            {"Legacy CTA Strength": 1, "Legacy Current Website Quality": 1},
        )
        self.assertEqual(report["legacy_only"], {})

    def test_orphans_are_backed_up_before_deletion(self) -> None:
        records = [{"id": "rec1", "fields": {"Place Key": "p1", "Legacy CTA Strength": "weak"}}]
        backup = backup_records(records)
        self.assertEqual(backup["records"][0]["Legacy CTA Strength"], "weak")

    def test_verify_rejects_a_surviving_orphan(self) -> None:
        snapshot = {"record_count": 1}
        schema = {"fields": [{"id": "f1", "name": "Legacy CTA Strength"},
                             *[{"id": f"f{i}", "name": n}
                               for i, n in enumerate(LEGACY_FIELD_PAIRS.values(), start=2)]]}
        errors = verify_cutover(snapshot, schema, 1)
        self.assertIn("legacy fields still present: Legacy CTA Strength", errors)

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


class FieldOverrideTest(unittest.TestCase):
    def test_parse_field_list_strips_dedupes_and_requires_a_name(self) -> None:
        self.assertEqual(
            parse_field_list("Form Strength, Asset ID ,Form Strength"),
            ("Form Strength", "Asset ID"),
        )
        for bad in ("", " , ,"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_field_list(bad)

    def test_cli_refuses_an_explicit_empty_fields_override(self) -> None:
        args = [
            "--base-id",
            "appX",
            "--table",
            "Prospects",
            "--token",
            "pat",
            "prepare",
            "--fields",
            "",
        ]
        with patch("airtable_schema_cutover.fetch_table_schema") as schema, patch(
            "airtable_schema_cutover.fetch_records", return_value=[]
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(args)

        self.assertEqual(code, 2)
        self.assertIn("--fields must name at least one field", stdout.getvalue())
        schema.assert_not_called()

    def test_shipped_config_covers_this_cutovers_field_set(self) -> None:
        fields = load_fields_config(SHIPPED_CONFIG)
        self.assertEqual(
            fields,
            (
                "Commercial Materiality",
                "Recurring Consequence",
                "Root Cause Confidence",
                "Packageable Solution",
                "Value Mechanism",
                "Beyond Free Fix",
                "Self Fix Risk",
                "Intentionality",
                "Evidence Snapshot",
                "Form Strength",
                "Recommended Action",
                "Asset ID",
                "Asset Verifier Verdict",
                "Sales — Stage",
                "Sales — Attempts",
                "Sales — Last Activity",
                "Sales — Opener Arm",
                "Sales — Consent to Text",
                "Sales — Notes",
                "Sales — Assigned Rep",
                "Sales — Batch",
                "Sales — Booked Out?",
                "Sales — Ask Arm",
            ),
        )

    def test_shipped_config_records_the_consent_migration_before_the_delete(self) -> None:
        """`Sales — Consent to Text` is a checkbox holding live consent.

        The tool cannot see the difference between a dead column and one whose
        value has to survive somewhere else first, so the obligation is written
        down next to the delete list instead of only in a runbook step that a
        second operator would never read.
        """
        config = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
        migrations = config["migrate_before_delete"]
        consent = migrations["Sales — Consent to Text"]
        self.assertEqual(consent["into"], "Consent Posture")
        self.assertTrue(consent["why"].strip())
        self.assertNotIn("tooling_gap", consent)

    def test_every_declared_migration_is_deleted_and_lands_somewhere_surviving(self) -> None:
        config = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
        deleted = set(load_fields_config(SHIPPED_CONFIG))
        for source, migration in config["migrate_before_delete"].items():
            with self.subTest(field=source):
                # Declaring a migration for a field nobody deletes is noise; a
                # target that is itself on the delete list is a data loss the
                # checklist would read as safe.
                self.assertIn(source, deleted)
                self.assertNotIn(migration["into"], deleted)

    def test_load_fields_config_rejects_bad_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                load_fields_config(root / "missing.json")
            cases = {
                "not_json.json": "{nope",
                "list.json": '["A"]',
                "wrong_type.json": '{"fields": "A"}',
                "empty.json": '{"fields": []}',
                "blank.json": '{"fields": ["  "]}',
                "non_string.json": '{"fields": ["A", 3]}',
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_fields_config(path)

    def test_resolve_cutover_defaults_and_overrides(self) -> None:
        args = parse_args(["prepare"])
        self.assertIs(resolve_cutover(args), DEFAULT_CUTOVER)
        args = parse_args(["prepare", "--fields", "Asset ID, Form Strength"])
        self.assertEqual(
            resolve_cutover(args), FieldCutover({}, ("Asset ID", "Form Strength"))
        )
        args = parse_args(["prepare", "--fields-config", str(SHIPPED_CONFIG)])
        self.assertEqual(
            resolve_cutover(args), load_cutover_config(SHIPPED_CONFIG)
        )

    def test_shipped_config_promotes_consent_to_a_blocking_migration(self) -> None:
        cutover = load_cutover_config(SHIPPED_CONFIG)
        self.assertEqual(
            cutover.migrations,
            {"Sales — Consent to Text": "Consent Posture"},
        )
        self.assertNotIn("Sales — Consent to Text", cutover.plain)
        self.assertIn("Sales — Consent to Text", cutover.delete_fields)
        self.assertIn("Consent Posture", cutover.backup_fields)

    def test_prepare_refuses_checked_consent_without_canonical_posture(self) -> None:
        cutover = FieldCutover(
            plain=("Sales — Notes",),
            migrations={"Sales — Consent to Text": "Consent Posture"},
        )
        schema = {
            "fields": [
                {"id": "f1", "name": "Place Key"},
                {"id": "f2", "name": "Sales — Consent to Text"},
                {"id": "f3", "name": "Consent Posture"},
                {"id": "f4", "name": "Sales — Notes"},
            ]
        }
        blocked = prepare_cutover(
            schema,
            [
                {
                    "id": "rec-consent",
                    "fields": {
                        "Sales — Consent to Text": True,
                        "Consent Posture": "",
                    },
                }
            ],
            cutover,
        )
        self.assertFalse(blocked["safe_to_delete"])
        self.assertEqual(
            blocked["unmigrated"]["Sales — Consent to Text"],
            ["rec-consent"],
        )

        safe = prepare_cutover(
            schema,
            [
                {
                    "id": "rec-consent",
                    "fields": {
                        "Sales — Consent to Text": True,
                        "Consent Posture": "express verbal consent recorded 2026-07-01",
                    },
                },
                {
                    "id": "rec-no-consent",
                    "fields": {"Sales — Consent to Text": False},
                },
            ],
            cutover,
        )
        self.assertTrue(safe["safe_to_delete"])
        self.assertEqual(safe["unmigrated"], {})

    def test_field_override_args_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["prepare", "--fields", "A", "--fields-config", "x.json"])

    def test_prepare_with_plain_fields_reports_populated_but_never_blocks(self) -> None:
        cutover = FieldCutover(plain=("Form Strength", "Asset ID"))
        schema = {
            "fields": [
                {"id": "f1", "name": "Place Key"},
                {"id": "f2", "name": "Form Strength"},
                {"id": "f3", "name": "Asset ID"},
            ]
        }
        records = [
            {"id": "rec1", "fields": {"Form Strength": "weak", "Asset ID": "a1"}},
            {"id": "rec2", "fields": {}},
        ]
        report = prepare_cutover(schema, records, cutover)
        self.assertTrue(report["safe_to_delete"])
        self.assertEqual(
            report["orphan_populated_counts"], {"Form Strength": 1, "Asset ID": 1}
        )
        self.assertEqual(report["legacy_only"], {})
        self.assertEqual(report["conflicts"], {})

    def test_a_computed_field_is_never_counted_as_data_the_deletion_drops(self) -> None:
        # `Sales — Last Activity` is a lastModifiedTime: it reads as populated on
        # every record and always will, but there is no stored cell to back up.
        # Counting it as data loss overstates the cost of the one checklist that
        # gates an irreversible deletion.
        cutover = FieldCutover(plain=("Sales — Last Activity", "Form Strength"))
        schema = {
            "fields": [
                {"id": "f1", "name": "Place Key", "type": "singleLineText"},
                {
                    "id": "f2",
                    "name": "Sales — Last Activity",
                    "type": "lastModifiedTime",
                },
                {"id": "f3", "name": "Form Strength", "type": "singleLineText"},
            ]
        }
        records = [
            {
                "id": f"rec{index}",
                "fields": {
                    "Sales — Last Activity": "2026-07-27T04:46:23.000Z",
                    "Form Strength": "weak",
                },
            }
            for index in range(3)
        ]
        report = prepare_cutover(schema, records, cutover)
        self.assertEqual(report["orphan_populated_counts"], {"Form Strength": 3})
        self.assertEqual(report["computed_orphan_fields"], ["Sales — Last Activity"])
        self.assertTrue(report["safe_to_delete"])

    def test_every_computed_type_is_split_out_of_the_data_loss_counts(self) -> None:
        computed = sorted(COMPUTED_FIELD_TYPES)
        cutover = FieldCutover(plain=tuple(f"Derived {kind}" for kind in computed))
        schema = {
            "fields": [
                {"id": "f0", "name": "Place Key", "type": "singleLineText"},
                *[
                    {"id": f"f{index + 1}", "name": f"Derived {kind}", "type": kind}
                    for index, kind in enumerate(computed)
                ],
            ]
        }
        records = [
            {"id": "rec1", "fields": {f"Derived {kind}": "x" for kind in computed}}
        ]
        report = prepare_cutover(schema, records, cutover)
        self.assertEqual(report["orphan_populated_counts"], {})
        self.assertEqual(
            report["computed_orphan_fields"], [f"Derived {kind}" for kind in computed]
        )

    def test_computed_field_names_reads_the_type_not_the_name(self) -> None:
        schema = {
            "fields": [
                {"id": "f1", "name": "Looks Computed", "type": "singleLineText"},
                {"id": "f2", "name": "Plain Name", "type": "formula"},
            ]
        }
        self.assertEqual(computed_field_names(schema), {"Plain Name"})

    def test_prepare_refuses_an_overridden_field_missing_from_the_schema(self) -> None:
        # Typo protection: silently backing up nothing would lose the values.
        cutover = FieldCutover(plain=("Form Strenth",))
        report = prepare_cutover(schema_with_all_pairs(), [], cutover)
        self.assertFalse(report["safe_to_delete"])
        self.assertEqual(report["missing_legacy_fields"], ["Form Strenth"])

    def test_backup_includes_plain_fields(self) -> None:
        cutover = FieldCutover(plain=("Asset ID",))
        records = [{"id": "rec1", "fields": {"Place Key": "p1", "Asset ID": "a1"}}]
        backup = backup_records(records, cutover)
        self.assertEqual(backup["records"][0]["Asset ID"], "a1")
        self.assertNotIn("Send Trigger", backup["records"][0])

    def test_verify_with_plain_fields_checks_absence_not_canonicals(self) -> None:
        cutover = FieldCutover(plain=("Asset ID",))
        surviving = {"fields": [{"id": "f1", "name": "Asset ID"}]}
        errors = verify_cutover({"record_count": 1}, surviving, 1, cutover)
        self.assertIn("cutover fields still present: Asset ID", errors)
        deleted = {"fields": [{"id": "f1", "name": "Place Key"}]}
        self.assertEqual(verify_cutover({"record_count": 1}, deleted, 1, cutover), [])

    def test_cutover_from_snapshot_round_trips_and_validates(self) -> None:
        self.assertIs(cutover_from_snapshot({}), DEFAULT_CUTOVER)
        stored = {
            "cutover": {
                "pairs": {"Legacy A": "A"},
                "migrations": {"Consent A": "Consent Posture"},
                "plain": ["B"],
            }
        }
        self.assertEqual(
            cutover_from_snapshot(stored),
            FieldCutover(
                {"Legacy A": "A"},
                ("B",),
                {"Consent A": "Consent Posture"},
            ),
        )
        for bad in (
            {"cutover": "B"},
            {"cutover": {"pairs": {}, "plain": []}},
            {"cutover": {"pairs": [], "plain": []}},
            {"cutover": {"pairs": {"A": 1}, "plain": []}},
            {"cutover": {"pairs": {}, "plain": [1]}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cutover_from_snapshot(bad)

    def test_prepare_with_fields_override_stays_get_only(self) -> None:
        schema_payload = {
            "tables": [
                {
                    "id": "tblX",
                    "name": "Prospects",
                    "fields": [
                        {"id": "f1", "name": "Place Key"},
                        {"id": "f2", "name": "Form Strength"},
                        {"id": "f3", "name": "Asset ID"},
                    ],
                }
            ]
        }
        records_payload = {
            "records": [
                {
                    "id": "rec1",
                    "fields": {"Place Key": "p1", "Form Strength": "weak", "Asset ID": "a1"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args(
                [
                    "--base-id", "appX",
                    "--table", "Prospects",
                    "--token", "pat",
                    "prepare",
                    "--fields", "Form Strength, Asset ID",
                    "--output-dir", tmp,
                ]
            )
            with patch("airtable_schema_cutover.requests") as mock_requests:
                mock_requests.get.side_effect = [
                    json_response(schema_payload),
                    json_response(records_payload),
                ]
                with contextlib.redirect_stdout(io.StringIO()):
                    code = run_prepare(args)
            self.assertEqual(code, 0)
            mock_requests.get.assert_called()
            for verb in ("post", "put", "patch", "delete"):
                getattr(mock_requests, verb).assert_not_called()
            # The fetch asked Airtable for exactly the override's backup fields.
            params = mock_requests.get.call_args_list[1].kwargs["params"]
            self.assertEqual(
                [value for key, value in params if key == "fields[]"],
                ["Place Key", "Form Strength", "Asset ID"],
            )
            snapshot = json.loads((Path(tmp) / "pre_cutover.json").read_text(encoding="utf-8"))
            self.assertEqual(
                snapshot["cutover"],
                {
                    "pairs": {},
                    "migrations": {},
                    "plain": ["Form Strength", "Asset ID"],
                },
            )
            backup = json.loads((Path(tmp) / "backup.json").read_text(encoding="utf-8"))
            self.assertEqual(backup["records"][0]["Form Strength"], "weak")
            checklist = (Path(tmp) / "delete_checklist.md").read_text(encoding="utf-8")
            self.assertIn("- [ ] `Form Strength`", checklist)
            self.assertIn("- [ ] `Asset ID`", checklist)

    def _run_verify(self, tmp: str, schema_fields: list[str]) -> tuple[int, MagicMock]:
        snapshot_path = Path(tmp) / "pre_cutover.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "base_id": "appX",
                    "table": {"id": "tblX", "name": "Prospects"},
                    "record_count": 1,
                    "cutover": {"pairs": {}, "plain": ["Asset ID"]},
                }
            ),
            encoding="utf-8",
        )
        args = parse_args(
            [
                "--base-id", "appX",
                "--table", "Prospects",
                "--token", "pat",
                "verify",
                str(snapshot_path),
            ]
        )
        schema_payload = {
            "tables": [
                {
                    "id": "tblX",
                    "name": "Prospects",
                    "fields": [
                        {"id": f"f{i}", "name": name}
                        for i, name in enumerate(schema_fields, start=1)
                    ],
                }
            ]
        }
        with patch("airtable_schema_cutover.requests") as mock_requests:
            mock_requests.get.side_effect = [
                json_response(schema_payload),
                json_response({"records": [{"id": "rec1"}]}),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_verify(args)
        return code, mock_requests

    def test_verify_uses_the_snapshot_cutover_and_stays_get_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, mock_requests = self._run_verify(tmp, ["Place Key"])
        self.assertEqual(code, 0)
        for verb in ("post", "put", "patch", "delete"):
            getattr(mock_requests, verb).assert_not_called()

    def test_verify_fails_while_a_snapshot_field_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run_verify(tmp, ["Place Key", "Asset ID"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
