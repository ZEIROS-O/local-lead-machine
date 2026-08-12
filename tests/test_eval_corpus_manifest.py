"""The manifest that decides which corpora the evals replay.

Before it, all three evals globbed `scored_*.csv` over `output/` — 49 files back
to 2026-05-31 on the operator machine, including `scored_debug_1.csv` and every
superseded recovery run. Completeness and reproducibility replayed the lot, so
they could never go green: a lead re-scored across three passes contributed three
rows, two of them decided by a gate nobody operates against any more.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evals.corpus import (
    DEFAULT_PATTERN,
    MANIFEST_PATH,
    CorpusManifestError,
    describe,
    load_manifest,
    resolve_scored_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class ShippedManifestTests(unittest.TestCase):
    def test_the_checked_in_manifest_parses_and_names_corpora(self) -> None:
        names = load_manifest()
        self.assertIsNotNone(names, f"{MANIFEST_PATH} must exist")
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(name.startswith("scored_"))
                self.assertTrue(name.endswith(".csv"))

    def test_the_manifest_lists_no_duplicates(self) -> None:
        names = load_manifest()
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_the_superseded_claimfix_corpus_is_not_replayed_alongside_its_refresh(self) -> None:
        """Both would double-count every claimfix lead, once under a verdict the
        gate has withdrawn — which is the 2026-08-12 re-verdict this manifest
        was written after."""
        names = set(load_manifest())
        if "scored_rescore_claimfix_reverdict_2026-08-12.csv" in names:
            self.assertNotIn("scored_rescore_claimfix.csv", names)


class ResolutionTests(unittest.TestCase):
    def _manifest(self, directory: Path, names: list[str]) -> Path:
        path = directory / "manifest.json"
        path.write_text(json.dumps({"active": names}), encoding="utf-8")
        return path

    def _corpus(self, directory: Path, name: str) -> Path:
        path = directory / name
        path.write_text("Place Key\n", encoding="utf-8")
        return path

    def test_explicit_files_win_over_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            named = self._corpus(directory, "scored_named.csv")
            self._corpus(directory, "scored_other.csv")
            manifest = self._manifest(directory, ["scored_other.csv"])

            paths, source = resolve_scored_paths(
                output_dir=directory, scored_files=[named], manifest_path=manifest
            )

        self.assertEqual(paths, [named])
        self.assertEqual(source, "explicit")

    def test_an_explicit_glob_overrides_the_manifest(self) -> None:
        """The evals are also used to inspect one batch by hand; `pattern=None`
        means "not specified", which is what makes this distinguishable."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._corpus(directory, "scored_a.csv")
            self._corpus(directory, "scored_b.csv")
            manifest = self._manifest(directory, ["scored_a.csv"])

            manifested, manifest_source = resolve_scored_paths(
                output_dir=directory, manifest_path=manifest
            )
            globbed, glob_source = resolve_scored_paths(
                output_dir=directory, pattern=DEFAULT_PATTERN, manifest_path=manifest
            )

        self.assertEqual([path.name for path in manifested], ["scored_a.csv"])
        self.assertEqual(manifest_source, "manifest")
        self.assertEqual([path.name for path in globbed], ["scored_a.csv", "scored_b.csv"])
        self.assertEqual(glob_source, "glob")

    def test_a_manifest_naming_a_missing_file_is_an_error_not_a_skip(self) -> None:
        """A silently-skipped entry is how a corpus falls out of coverage while
        the gate stays green — the exact failure this manifest exists to end."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._corpus(directory, "scored_a.csv")
            manifest = self._manifest(directory, ["scored_a.csv", "scored_gone.csv"])

            with self.assertRaises(CorpusManifestError) as caught:
                resolve_scored_paths(output_dir=directory, manifest_path=manifest)

        self.assertIn("scored_gone.csv", str(caught.exception))

    def test_an_empty_active_list_is_a_configuration_error(self) -> None:
        """Distinct from "no manifest", which legitimately falls back to the glob.
        An empty list silently replaying everything would undo the whole point."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), [])
            with self.assertRaises(CorpusManifestError):
                load_manifest(manifest)

    def test_no_manifest_falls_back_to_the_glob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._corpus(directory, "scored_a.csv")
            missing = directory / "absent.json"

            self.assertIsNone(load_manifest(missing))
            paths, source = resolve_scored_paths(output_dir=directory, manifest_path=missing)

        self.assertEqual([path.name for path in paths], ["scored_a.csv"])
        self.assertEqual(source, "glob")

    def test_a_malformed_manifest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for payload in ("[]", '{"active": "scored_a.csv"}', "{", '{"active": [1]}'):
                path = directory / "manifest.json"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(payload=payload):
                    with self.assertRaises(CorpusManifestError):
                        load_manifest(path)


class DescribeTests(unittest.TestCase):
    def test_the_coverage_block_names_what_was_replayed(self) -> None:
        block = describe([Path("output/scored_b.csv"), Path("output/scored_a.csv")], "manifest")

        self.assertEqual(
            block,
            {"source": "manifest", "count": 2, "files": ["scored_a.csv", "scored_b.csv"]},
        )


class ReportCoverageTests(unittest.TestCase):
    """Every eval report carries the block, so a green run states its coverage."""

    def test_each_eval_reports_the_corpora_it_replayed(self) -> None:
        import evals.completeness as completeness
        import evals.gate_liveness as gate_liveness
        import evals.reproducibility as reproducibility

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scored_a.csv"
            path.write_text(
                "Place Key,Business Name,Verdict\nplace-1,Acme,reject\n", encoding="utf-8"
            )

            _, completeness_report = completeness.run_eval(
                scored_files=[path], base_records=[]
            )
            _, reproducibility_report = reproducibility.run_eval(
                scored_files=[path], base_records=[]
            )
            _, liveness_report = gate_liveness.run_eval(scored_files=[path])

        for name, report in (
            ("completeness", completeness_report),
            ("reproducibility", reproducibility_report),
            ("gate_liveness", liveness_report),
        ):
            with self.subTest(eval=name):
                self.assertEqual(report["corpora"]["source"], "explicit")
                self.assertEqual(report["corpora"]["files"], ["scored_a.csv"])


if __name__ == "__main__":
    unittest.main()
