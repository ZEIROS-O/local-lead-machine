from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertNotIn("--include-manual", msg)
        self.assertLess(len(msg), hc.TELEGRAM_BUDGET)

    def test_leading_slash_tolerated(self) -> None:
        self.assertEqual(hc.main(["/help"]), 0)

    def test_invalid_size_refusal_returns_2(self) -> None:
        self.assertEqual(hc.main(["run", "epoxy", "cleveland", "huge"]), 2)

    def test_score_parser_requires_limit(self) -> None:
        with self.assertRaises(SystemExit):
            hc.build_parser().parse_args(["score", "detailing_cleveland_medium_001", "--timeout", "20"])

    def test_retired_include_manual_flag_is_rejected_by_every_old_surface(self) -> None:
        commands = (
            ["run", "epoxy", "cleveland", "medium", "--auto", "--include-manual"],
            ["sync-dry", "epoxy_cleveland_medium_001", "--include-manual"],
            ["sync-live", "epoxy_cleveland_medium_001", "--include-manual"],
        )
        for argv in commands:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                hc.build_parser().parse_args(argv)

    def test_hermes_skill_and_docs_describe_only_total_upload(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "hermes/skills/local-lead-machine/SKILL.md",
            "docs/hermes-commands.md",
        ):
            with self.subTest(path=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertNotIn("--include-manual", text)
                self.assertIn("all scored", text.lower())


class TelegramRouterTest(unittest.TestCase):
    def test_run_slash_run_routes_to_hermes_autopilot_execute(self) -> None:
        routed = hc.route_telegram_message(
            "run /run epoxy cleveland medium --auto --score-budget 5"
        )

        self.assertIsNotNone(routed)
        assert routed is not None
        self.assertEqual(
            routed.argv,
            ["run", "epoxy", "cleveland", "medium", "--auto", "--score-budget", "5", "--execute"],
        )
        self.assertEqual(routed.cwd, Path("~/local-lead-machine-hermes").expanduser())
        self.assertEqual(routed.env["LOCAL_LEAD_MACHINE_ROOT"], str(Path("~/local-lead-machine").expanduser()))
        self.assertEqual(
            routed.env["LOCAL_LEAD_MACHINE_PYTHON"],
            str(Path("~/local-lead-machine/.venv/bin/python").expanduser()),
        )
        self.assertEqual(
            routed.command,
            [
                str(Path("~/local-lead-machine/.venv/bin/python").expanduser()),
                "-m",
                "lead_machine.hermes_commands",
                "run",
                "epoxy",
                "cleveland",
                "medium",
                "--auto",
                "--score-budget",
                "5",
                "--execute",
            ],
        )
        self.assertNotIn("run_agent_pipeline.py", " ".join(routed.command))

    def test_run_score_dry_routes_to_hermes_cli(self) -> None:
        routed = hc.route_telegram_message("run /score-dry epoxy_cleveland_medium_001 --limit 25 --budget 2")

        self.assertIsNotNone(routed)
        assert routed is not None
        self.assertEqual(
            routed.argv,
            ["score-dry", "epoxy_cleveland_medium_001", "--limit", "25", "--budget", "2", "--execute"],
        )

    def test_raw_slash_score_dry_may_route_to_hermes_cli(self) -> None:
        routed = hc.route_telegram_message("/score-dry epoxy_cleveland_medium_001 --limit 25 --budget 2")

        self.assertIsNotNone(routed)
        assert routed is not None
        self.assertEqual(routed.argv[0], "score-dry")

    def test_yes_and_no_route_to_pending_approval_continuation(self) -> None:
        yes = hc.route_telegram_message("yes")
        no = hc.route_telegram_message("no")

        self.assertIsNotNone(yes)
        self.assertIsNotNone(no)
        assert yes is not None
        assert no is not None
        self.assertEqual(yes.argv, ["yes"])
        self.assertEqual(no.argv, ["no"])

    def test_unknown_message_not_intercepted(self) -> None:
        self.assertIsNone(hc.route_telegram_message("run the detailing batch"))

    def test_router_preview_does_not_execute(self) -> None:
        routed = hc.route_telegram_message(
            "run /run epoxy cleveland medium --auto --score-budget 5"
        )
        assert routed is not None

        with patch("subprocess.run") as run:
            preview = routed.preview()

        run.assert_not_called()
        self.assertIn("cd ", preview)
        self.assertIn("python -m lead_machine.hermes_commands run epoxy cleveland medium", preview)
        self.assertEqual(os.fspath(routed.cwd), str(Path("~/local-lead-machine-hermes").expanduser()))


if __name__ == "__main__":
    unittest.main()
