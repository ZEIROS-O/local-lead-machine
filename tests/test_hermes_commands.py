from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lead_machine import hermes_commands as hc


class ValidationTest(unittest.TestCase):
    def test_valid_slugs(self) -> None:
        for slug in ("epoxy", "pool_builders", "a1", "x_y_z"):
            self.assertEqual(hc.validate_slug(slug), slug)

    def test_invalid_slugs(self) -> None:
        for slug in ("Epoxy", "../x", "_x", "a/b", "", "1abc", "a-b", "a..b"):
            with self.assertRaises(hc.CommandError):
                hc.validate_slug(slug)

    def test_size(self) -> None:
        for size in ("small", "medium", "full"):
            self.assertEqual(hc.validate_size(size), size)
        with self.assertRaises(hc.CommandError):
            hc.validate_size("huge")

    def test_split_terms(self) -> None:
        self.assertEqual(hc.split_terms("a; b ;c"), ["a", "b", "c"])
        self.assertEqual(hc.split_terms("  "), [])
        self.assertEqual(hc.split_terms(None), [])


class CreateProfileTest(unittest.TestCase):
    def test_create_vertical_preview_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            res = hc.cmd_create_vertical("pool_builders", terms="pool builder; pool contractor", vertical_dir=vdir)
            self.assertFalse((vdir / "pool_builders.json").exists())
            self.assertIn("preview", res.message)

    def test_create_vertical_execute_writes_expected_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            res = hc.cmd_create_vertical(
                "pool_builders",
                display="Pool Builders",
                airtable="Pool Builders",
                terms="pool builder; inground pool contractor",
                execute=True,
                vertical_dir=vdir,
            )
            path = vdir / "pool_builders.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["vertical"], "pool_builders")
            self.assertEqual(data["search_terms"], ["pool builder", "inground pool contractor"])
            self.assertIn("written", res.message)

    def test_create_market_execute_writes_expected_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp)
            hc.cmd_create_market(
                "pittsburgh",
                state="PA",
                small="Pittsburgh PA",
                medium="Pittsburgh PA; Mt Lebanon PA",
                execute=True,
                market_dir=mdir,
            )
            data = json.loads((mdir / "pittsburgh.json").read_text(encoding="utf-8"))
            self.assertEqual(data["geos"], ["Pittsburgh PA", "Mt Lebanon PA"])
            self.assertEqual(data["sizes"]["small"], ["Pittsburgh PA"])


class HelpAndDispatchTest(unittest.TestCase):
    def test_help_contains_staged_commands_and_deprecated_confirm(self) -> None:
        msg = hc.cmd_help().message
        for verb in ("/run", "/score-dry", "/score", "/score-rest", "/packets", "/sync-dry", "/sync-live", "/status"):
            self.assertIn(verb, msg)
        self.assertIn("/confirm - deprecated", msg)
        self.assertLess(len(msg), hc.TELEGRAM_BUDGET)

    def test_leading_slash_tolerated(self) -> None:
        self.assertEqual(hc.main(["/help"]), 0)

    def test_invalid_size_refusal_returns_2(self) -> None:
        self.assertEqual(hc.main(["run", "epoxy", "cleveland", "huge"]), 2)

    def test_score_parser_requires_limit(self) -> None:
        with self.assertRaises(SystemExit):
            hc.build_parser().parse_args(["score", "detailing_cleveland_medium_001", "--timeout", "20"])


if __name__ == "__main__":
    unittest.main()
