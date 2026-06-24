from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import generate_search_config as gsc
from lead_machine.verticals import VERTICAL_PROFILES

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_VERTICAL_DIR = REPO_ROOT / "vertical_profiles"
REAL_MARKET_DIR = REPO_ROOT / "market_profiles"


class GenerateSearchConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.configs_dir = Path(self._tmp.name) / "search_configs"
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.geo_dir = self.configs_dir / "geo_slices"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _generate(self, **kwargs) -> Path:
        params = dict(
            vertical_dir=REAL_VERTICAL_DIR,
            market_dir=REAL_MARKET_DIR,
            configs_dir=self.configs_dir,
            geo_dir=self.geo_dir,
            quiet=True,
        )
        params.update(kwargs)
        return gsc.generate_config(**params)

    def _load(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- happy path ----------------------------------------------------- #
    def test_generate_epoxy_cleveland_medium(self) -> None:
        path = self._generate(vertical="epoxy", market="cleveland", size="medium")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "epoxy_cleveland_medium_001.json")
        cfg = self._load(path)
        self.assertEqual(cfg["batch"], "epoxy_cleveland_medium_001")
        self.assertEqual(cfg["vertical"], "epoxy")
        self.assertEqual(cfg["location"], "Cleveland OH")
        # Queries are search terms appended with the primary location.
        self.assertIn("epoxy flooring Cleveland OH", cfg["queries"])
        self.assertIn("garage floor epoxy Cleveland OH", cfg["queries"])
        # Medium geo subset (13 slices for cleveland), inlined for self-containment.
        self.assertEqual(len(cfg["geo_slices"]), 13)
        self.assertIn("Cleveland OH", cfg["geo_slices"])
        self.assertNotIn("Akron OH", cfg["geo_slices"])  # full-only slice excluded
        # epoxy is not in verticals.py -> boost/exclude come from the JSON profile.
        self.assertIn("metallic epoxy flooring", cfg["boost_terms"])
        self.assertIn("resin art", cfg["exclude_terms"])
        self.assertEqual(cfg["min_rating"], 4.3)
        self.assertEqual(cfg["min_reviews"], 5)
        self.assertEqual(cfg["limit_per_query"], 10)

    def test_full_size_uses_all_geos(self) -> None:
        path = self._generate(vertical="epoxy", market="cleveland", size="full")
        cfg = self._load(path)
        self.assertEqual(len(cfg["geo_slices"]), 24)
        self.assertIn("Akron OH", cfg["geo_slices"])

    def test_detailing_boost_terms_sourced_from_verticals_py(self) -> None:
        # detailing IS in verticals.py, so boost terms must be the canonical
        # high_ticket_terms (single source of truth), not duplicated JSON.
        path = self._generate(vertical="detailing", market="cleveland", size="medium")
        cfg = self._load(path)
        canonical = list(VERTICAL_PROFILES["detailing"].high_ticket_terms)
        self.assertEqual(cfg["boost_terms"], canonical)
        # Canonical deny terms are merged with the profile's extra noise terms.
        self.assertIn("express wash", cfg["exclude_terms"])
        self.assertIn("dealership service", cfg["exclude_terms"])

    def test_refreshes_geo_slices_file(self) -> None:
        self._generate(vertical="epoxy", market="cleveland", size="small")
        geo_file = self.geo_dir / "cleveland.json"
        self.assertTrue(geo_file.exists())
        geos = json.loads(geo_file.read_text(encoding="utf-8"))
        # The shared geo_slices file holds the FULL market list so geo_slices_ref
        # keeps working for hand-authored configs.
        self.assertEqual(len(geos), 24)

    # ---- refusals ------------------------------------------------------- #
    def test_refuses_unknown_vertical(self) -> None:
        with self.assertRaises(gsc.ConfigGenError) as ctx:
            self._generate(vertical="nope", market="cleveland", size="medium")
        self.assertIn("[REFUSE] Missing vertical profile", str(ctx.exception))
        self.assertIn("vertical_profiles/nope.json", str(ctx.exception))

    def test_refuses_unknown_market(self) -> None:
        with self.assertRaises(gsc.ConfigGenError) as ctx:
            self._generate(vertical="epoxy", market="gotham", size="medium")
        self.assertIn("[REFUSE] Missing market profile", str(ctx.exception))
        self.assertIn("market_profiles/gotham.json", str(ctx.exception))

    def test_refuses_overwrite_unless_flag(self) -> None:
        self._generate(vertical="epoxy", market="cleveland", size="medium")
        with self.assertRaises(gsc.ConfigGenError) as ctx:
            self._generate(vertical="epoxy", market="cleveland", size="medium")
        self.assertIn("already exists", str(ctx.exception))
        # With overwrite it succeeds.
        path = self._generate(
            vertical="epoxy", market="cleveland", size="medium", overwrite=True
        )
        self.assertTrue(path.exists())

    def test_invalid_size_refused(self) -> None:
        with self.assertRaises(gsc.ConfigGenError):
            self._generate(vertical="epoxy", market="cleveland", size="huge")

    def test_explicit_batch_name_respected(self) -> None:
        path = self._generate(
            vertical="epoxy", market="cleveland", size="medium",
            batch="epoxy_cleveland_medium_007",
        )
        self.assertEqual(path.name, "epoxy_cleveland_medium_007.json")
        self.assertEqual(self._load(path)["batch"], "epoxy_cleveland_medium_007")


if __name__ == "__main__":
    unittest.main()
