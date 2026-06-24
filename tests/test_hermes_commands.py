from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lead_machine import hermes_commands as hc
from lead_machine.sendability import (
    PACKET_TIER_SENDABLE,
    PACKET_TIER_MANUAL,
    PACKET_TIER_WATCHLIST,
    PACKET_TIER_NONE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_VERTICAL_DIR = REPO_ROOT / "vertical_profiles"
REAL_MARKET_DIR = REPO_ROOT / "market_profiles"


class SlugAndSizeTest(unittest.TestCase):
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


class ParseBatchTest(unittest.TestCase):
    def _dirs(self) -> dict:
        return dict(vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR)

    def test_simple_roundtrip(self) -> None:
        p = hc.parse_batch("epoxy_cleveland_medium_001", **self._dirs())
        self.assertEqual((p.vertical, p.market, p.size, p.seq), ("epoxy", "cleveland", "medium", "001"))

    def test_multiword_vertical(self) -> None:
        # Synthetic dirs so 'pool_builders' + 'pittsburgh' are known.
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp) / "v"
            mdir = Path(tmp) / "m"
            vdir.mkdir()
            mdir.mkdir()
            (vdir / "pool_builders.json").write_text("{}", encoding="utf-8")
            (mdir / "pittsburgh.json").write_text("{}", encoding="utf-8")
            p = hc.parse_batch(
                "pool_builders_pittsburgh_full_003", vertical_dir=vdir, market_dir=mdir
            )
            self.assertEqual(
                (p.vertical, p.market, p.size, p.seq),
                ("pool_builders", "pittsburgh", "full", "003"),
            )

    def test_rejects_bad_size_token(self) -> None:
        with self.assertRaises(hc.CommandError):
            hc.parse_batch("epoxy_cleveland_001", **self._dirs())

    def test_rejects_too_few_tokens(self) -> None:
        with self.assertRaises(hc.CommandError):
            hc.parse_batch("foo", **self._dirs())

    def test_rejects_unknown_vertical_market(self) -> None:
        with self.assertRaises(hc.CommandError):
            hc.parse_batch("nope_nowhere_medium_001", **self._dirs())


class BuildRunCommandsTest(unittest.TestCase):
    def test_two_commands_no_yes(self) -> None:
        cmds = hc.build_run_commands("epoxy", "cleveland", "medium", cap=1.0)
        self.assertEqual(len(cmds), 2)
        search, dry = cmds
        self.assertEqual(
            search,
            [sys.executable, "run_agent_pipeline.py",
             "--vertical", "epoxy", "--market", "cleveland", "--size", "medium",
             "--mode", "search-only", "--max-llm-usd", "1.00"],
        )
        self.assertEqual(
            dry,
            [sys.executable, "run_agent_pipeline.py",
             "--vertical", "epoxy", "--market", "cleveland", "--size", "medium",
             "--batch", hc.BATCH_PLACEHOLDER,
             "--mode", "score-dry-run", "--max-llm-usd", "1.00"],
        )
        for cmd in cmds:
            self.assertNotIn("--yes", cmd)
            self.assertNotIn("full", cmd)
            self.assertIn("--max-llm-usd", cmd)

    def test_batch_substituted(self) -> None:
        _, dry = hc.build_run_commands("epoxy", "cleveland", "medium", batch="epoxy_cleveland_medium_002")
        self.assertIn("epoxy_cleveland_medium_002", dry)
        self.assertNotIn(hc.BATCH_PLACEHOLDER, dry)


class BuildConfirmCommandTest(unittest.TestCase):
    def test_exact_argv(self) -> None:
        cmd = hc.build_confirm_command("epoxy", "cleveland", "medium", "epoxy_cleveland_medium_001", cap=1.0)
        self.assertEqual(
            cmd,
            [sys.executable, "run_agent_pipeline.py",
             "--vertical", "epoxy", "--market", "cleveland", "--size", "medium",
             "--batch", "epoxy_cleveland_medium_001",
             "--mode", "score-confirmed", "--yes",
             "--max-llm-usd", "1.00", "--airtable", "live"],
        )
        self.assertIn("--yes", cmd)
        self.assertIn("--airtable", cmd)
        self.assertNotIn("--include-manual-review-airtable", cmd)


class RunPreflightTest(unittest.TestCase):
    def test_missing_vertical_refuses_no_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp) / "v"
            mdir = Path(tmp) / "m"
            vdir.mkdir()
            mdir.mkdir()
            (mdir / "cleveland.json").write_text("{}", encoding="utf-8")
            with patch.object(hc, "_exec") as mock_exec:
                with self.assertRaises(hc.CommandError):
                    hc.cmd_run("epoxy", "cleveland", "medium",
                               vertical_dir=vdir, market_dir=mdir, execute=True)
                mock_exec.assert_not_called()

    def test_missing_market_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp) / "v"
            mdir = Path(tmp) / "m"
            vdir.mkdir()
            mdir.mkdir()
            (vdir / "epoxy.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(hc.CommandError):
                hc.cmd_run("epoxy", "cleveland", "medium", vertical_dir=vdir, market_dir=mdir)

    def test_preview_builds_commands_no_subprocess(self) -> None:
        with patch.object(hc, "_exec") as mock_exec:
            res = hc.cmd_run("epoxy", "cleveland", "medium",
                             vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR)
            mock_exec.assert_not_called()
        self.assertIn("run_agent_pipeline.py", res.message)
        self.assertIn("search-only", res.message)
        self.assertIn("score-dry-run", res.message)
        self.assertEqual(res.exit_code, 0)


class ConfirmTest(unittest.TestCase):
    def test_refuses_unknown_batch(self) -> None:
        with self.assertRaises(hc.CommandError):
            hc.cmd_confirm("nope_nowhere_medium_001",
                           vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR)

    def test_refuses_when_enriched_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            out.mkdir()
            with self.assertRaises(hc.CommandError):
                hc.cmd_confirm("epoxy_cleveland_medium_001",
                               vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR,
                               output_dir=out)

    def test_preview_builds_command_no_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            out.mkdir()
            (out / "enriched_epoxy_cleveland_medium_001.jsonl").write_text("", encoding="utf-8")
            with patch.object(hc, "_exec") as mock_exec:
                res = hc.cmd_confirm("epoxy_cleveland_medium_001",
                                     vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR,
                                     output_dir=out)
                mock_exec.assert_not_called()
            self.assertIn("score-confirmed", res.message)
            self.assertIn("--yes", res.message)
            self.assertIn("--airtable live", res.message)


class StatusTest(unittest.TestCase):
    def _make_layout(self, tmp: Path) -> dict:
        out = tmp / "output"
        raw = tmp / "data" / "raw"
        out.mkdir(parents=True)
        raw.mkdir(parents=True)
        return dict(output_dir=out, raw_dir=raw, packets_root=tmp)

    def test_nothing_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dirs = self._make_layout(Path(tmp))
            res = hc.cmd_status("epoxy_cleveland_medium_001", **dirs)
            self.assertIn("nothing run yet", res.message)

    def test_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dirs = self._make_layout(tmp)
            batch = "epoxy_cleveland_medium_001"
            (dirs["raw_dir"] / f"{batch}.csv").write_text("x", encoding="utf-8")
            (dirs["output_dir"] / f"enriched_{batch}.jsonl").write_text("x", encoding="utf-8")
            rep = hc.status_report(batch, **dirs)
            self.assertTrue(rep.searched)
            self.assertTrue(rep.enriched)
            self.assertFalse(rep.scored)
            self.assertFalse(rep.packets)
            res = hc.cmd_status(batch, **dirs)
            self.assertIn("search ✓", res.message)
            self.assertIn("score ✗", res.message)
            self.assertIn("Next stage: score", res.message)

    def test_complete_with_tier_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dirs = self._make_layout(tmp)
            batch = "epoxy_cleveland_medium_001"
            (dirs["raw_dir"] / f"{batch}.csv").write_text("x", encoding="utf-8")
            (dirs["output_dir"] / f"enriched_{batch}.jsonl").write_text("x", encoding="utf-8")
            (dirs["output_dir"] / f"scored_{batch}_api.csv").write_text("x", encoding="utf-8")
            pdir = tmp / f"packets_{batch}"
            pdir.mkdir()
            index = pdir / "packet_index.csv"
            rows = [
                f"Name,Packet Tier",
                f"A,{PACKET_TIER_SENDABLE}",
                f"B,{PACKET_TIER_SENDABLE}",
                f"C,{PACKET_TIER_MANUAL}",
                f"D,{PACKET_TIER_WATCHLIST}",
                f"E,{PACKET_TIER_NONE}",
            ]
            index.write_text("\n".join(rows) + "\n", encoding="utf-8")
            rep = hc.status_report(batch, **dirs)
            self.assertTrue(rep.searched and rep.enriched and rep.scored and rep.packets)
            self.assertEqual(rep.tier_counts[PACKET_TIER_SENDABLE], 2)
            self.assertEqual(rep.tier_counts[PACKET_TIER_MANUAL], 1)
            self.assertEqual(rep.tier_counts[PACKET_TIER_WATCHLIST], 1)
            self.assertEqual(rep.tier_counts[PACKET_TIER_NONE], 1)
            res = hc.cmd_status(batch, **dirs)
            self.assertIn("Sendable 2", res.message)
            self.assertIn("Manual 1", res.message)


class CreateVerticalTest(unittest.TestCase):
    def test_preview_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            res = hc.cmd_create_vertical("pool_builders", terms="pool builder; pool contractor",
                                         vertical_dir=vdir)
            self.assertFalse((vdir / "pool_builders.json").exists())
            self.assertIn("preview", res.message)

    def test_execute_writes_expected_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            res = hc.cmd_create_vertical(
                "pool_builders", display="Pool Builders", airtable="Pool Builders",
                terms="pool builder; inground pool contractor",
                high_intent="custom pool", exclude="pool supplies",
                execute=True, vertical_dir=vdir,
            )
            path = vdir / "pool_builders.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["vertical"], "pool_builders")
            self.assertEqual(data["search_terms"], ["pool builder", "inground pool contractor"])
            self.assertEqual(data["high_intent_terms"], ["custom pool"])
            self.assertEqual(data["exclude_terms"], ["pool supplies"])
            self.assertNotIn("min_rating", data)
            self.assertIn("written", res.message)

    def test_refuses_existing_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            (vdir / "x.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(hc.CommandError):
                hc.cmd_create_vertical("x", terms="a", execute=True, vertical_dir=vdir)

    def test_overwrite_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            (vdir / "x.json").write_text("{}", encoding="utf-8")
            res = hc.cmd_create_vertical("x", terms="a", overwrite=True, execute=True, vertical_dir=vdir)
            self.assertIn("written", res.message)
            data = json.loads((vdir / "x.json").read_text(encoding="utf-8"))
            self.assertEqual(data["search_terms"], ["a"])

    def test_empty_terms_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(hc.CommandError):
                hc.cmd_create_vertical("x", terms="", execute=True, vertical_dir=Path(tmp))

    def test_min_rating_included_when_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            hc.cmd_create_vertical("x", terms="a", min_rating=4.5, min_reviews=10,
                                   execute=True, vertical_dir=vdir)
            data = json.loads((vdir / "x.json").read_text(encoding="utf-8"))
            self.assertEqual(data["min_rating"], 4.5)
            self.assertEqual(data["min_reviews"], 10)


class CreateMarketTest(unittest.TestCase):
    def test_preview_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp)
            res = hc.cmd_create_market("pittsburgh", state="PA",
                                       small="Pittsburgh PA", medium="Pittsburgh PA; Mt Lebanon PA",
                                       market_dir=mdir)
            self.assertFalse((mdir / "pittsburgh.json").exists())
            self.assertIn("preview", res.message)

    def test_full_omitted_geos_equals_medium(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp)
            hc.cmd_create_market("pittsburgh", state="PA",
                                 small="Pittsburgh PA",
                                 medium="Pittsburgh PA; Mt Lebanon PA",
                                 execute=True, market_dir=mdir)
            data = json.loads((mdir / "pittsburgh.json").read_text(encoding="utf-8"))
            self.assertEqual(data["geos"], ["Pittsburgh PA", "Mt Lebanon PA"])
            self.assertEqual(data["sizes"]["small"], ["Pittsburgh PA"])
            self.assertEqual(data["sizes"]["medium"], ["Pittsburgh PA", "Mt Lebanon PA"])

    def test_full_given_used_as_geos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp)
            hc.cmd_create_market("p", small="A", medium="A; B", full="A; B; C; D",
                                 execute=True, market_dir=mdir)
            data = json.loads((mdir / "p.json").read_text(encoding="utf-8"))
            self.assertEqual(data["geos"], ["A", "B", "C", "D"])

    def test_refuses_existing_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp)
            (mdir / "p.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(hc.CommandError):
                hc.cmd_create_market("p", small="A", execute=True, market_dir=mdir)

    def test_no_geos_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(hc.CommandError):
                hc.cmd_create_market("p", execute=True, market_dir=Path(tmp))


class ExecuteParsingTest(unittest.TestCase):
    """run/confirm execute paths parse pipeline stdout (subprocess mocked)."""

    def _completed(self, stdout: str, returncode: int = 0, stderr: str = ""):
        from subprocess import CompletedProcess
        return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_run_execute_parses_batch_and_cost(self) -> None:
        search_out = "[PIPELINE] batch=epoxy_cleveland_medium_002 config=x\n  Enriched rows:          17\n[DONE] x"
        dry_out = "[COST] projected_usd=0.0423 max_llm_usd=1.00\n"
        with patch.object(hc, "_exec", side_effect=[self._completed(search_out), self._completed(dry_out)]) as m:
            res = hc.cmd_run("epoxy", "cleveland", "medium",
                             vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR,
                             execute=True)
            self.assertEqual(m.call_count, 2)
        self.assertIn("epoxy_cleveland_medium_002", res.message)
        self.assertIn("17 leads", res.message)
        self.assertIn("$0.04", res.message)
        self.assertEqual(res.exit_code, 0)

    def test_run_execute_relays_refusal(self) -> None:
        out = "[REFUSE] Another pipeline run holds the lock\n"
        with patch.object(hc, "_exec", return_value=self._completed(out, returncode=2)):
            res = hc.cmd_run("epoxy", "cleveland", "medium",
                             vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR,
                             execute=True)
        self.assertEqual(res.exit_code, 2)
        self.assertIn("failed:", res.message)
        self.assertIn("lock", res.message)

    def test_confirm_execute_summarizes(self) -> None:
        out = (
            "[COST] projected_usd=0.0500 max_llm_usd=1.00\n"
            "  Scored rows:            12\n"
            "  Sendable Packets:       5\n"
            "  Manual Review Packets:  3\n"
            "  Airtable synced:        5 (live)\n"
            "[DONE] Pipeline summary\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "output"
            outdir.mkdir()
            (outdir / "enriched_epoxy_cleveland_medium_001.jsonl").write_text("", encoding="utf-8")
            with patch.object(hc, "_exec", return_value=self._completed(out)):
                res = hc.cmd_confirm("epoxy_cleveland_medium_001",
                                     vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR,
                                     output_dir=outdir, execute=True)
        self.assertIn("scored 12", res.message)
        self.assertIn("sendable 5", res.message)
        self.assertIn("packets 8", res.message)
        self.assertIn("Airtable synced 5", res.message)
        self.assertIn("$0.05", res.message)


class HelpAndBudgetTest(unittest.TestCase):
    def test_help_contains_all_verbs(self) -> None:
        msg = hc.cmd_help().message
        for verb in ("/run", "/confirm", "/status", "/create-vertical", "/create-market"):
            self.assertIn(verb, msg)
        self.assertLess(len(msg), hc.TELEGRAM_BUDGET)

    def test_all_previews_under_budget(self) -> None:
        msgs = [
            hc.cmd_run("epoxy", "cleveland", "medium",
                       vertical_dir=REAL_VERTICAL_DIR, market_dir=REAL_MARKET_DIR).message,
            hc.cmd_help().message,
            hc.cmd_status("epoxy_cleveland_medium_001",
                          output_dir=Path("/nonexistent"), raw_dir=Path("/nonexistent"),
                          packets_root=Path("/nonexistent")).message,
        ]
        for m in msgs:
            self.assertLess(len(m), hc.TELEGRAM_BUDGET)


class MainDispatchTest(unittest.TestCase):
    def test_leading_slash_tolerated(self) -> None:
        rc = hc.main(["/help"])
        self.assertEqual(rc, 0)

    def test_run_preview_no_subprocess(self) -> None:
        with patch.object(hc, "_exec") as mock_exec:
            rc = hc.main(["run", "epoxy", "cleveland", "medium"])
            mock_exec.assert_not_called()
        self.assertEqual(rc, 0)

    def test_refusal_returns_2(self) -> None:
        rc = hc.main(["run", "epoxy", "cleveland", "huge"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
