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
        # Medium geo subset trimmed to ~6 dense slices (pagination carries depth).
        self.assertEqual(len(cfg["geo_slices"]), 6)
        self.assertIn("Cleveland OH", cfg["geo_slices"])
        self.assertNotIn("Akron OH", cfg["geo_slices"])  # full-only slice excluded
        # epoxy IS in verticals.py now, so boost/exclude come from there — the
        # JSON profile's own exclude_terms are merged in rather than replaced.
        self.assertIn("metallic epoxy flooring", cfg["boost_terms"])
        self.assertIn("resin art", cfg["exclude_terms"])
        self.assertEqual(cfg["min_rating"], 4.3)
        self.assertEqual(cfg["min_reviews"], 10)
        # page_size defaults to 20; limit_per_query mirrors it for legacy readers;
        # medium depth = 2 pages per query.
        self.assertEqual(cfg["page_size"], 20)
        self.assertEqual(cfg["limit_per_query"], 20)
        self.assertEqual(cfg["max_pages_per_query"], 2)

    def test_size_based_search_depth(self) -> None:
        for size, expected_pages in (("small", 1), ("medium", 2), ("full", 3)):
            path = self._generate(
                vertical="epoxy", market="cleveland", size=size, overwrite=True
            )
            cfg = self._load(path)
            self.assertEqual(cfg["page_size"], 20, size)
            self.assertEqual(cfg["max_pages_per_query"], expected_pages, size)

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

    def test_generate_water_restoration_medium_configs_for_initial_ohio_metros(self) -> None:
        expected_geos = {
            "cleveland": "Cleveland OH",
            "cincinnati": "Cincinnati OH",
            "columbus": "Columbus OH",
        }

        for market, primary_geo in expected_geos.items():
            path = self._generate(
                vertical="water_restoration",
                market=market,
                size="medium",
            )
            self.assertEqual(path.name, f"water_restoration_{market}_medium_001.json")
            cfg = self._load(path)
            self.assertEqual(cfg["vertical"], "water_restoration")
            self.assertEqual(cfg["airtable_vertical"], "Water Restoration")
            self.assertIn(f"water damage restoration {cfg['location']}", cfg["queries"])
            self.assertIn(primary_geo, cfg["geo_slices"])
            self.assertEqual(cfg["min_rating"], 4.3)
            self.assertEqual(cfg["min_reviews"], 10)

            canonical = list(VERTICAL_PROFILES["water_restoration"].high_ticket_terms)
            self.assertEqual(cfg["boost_terms"], canonical)
            self.assertIn("carpet cleaning", cfg["exclude_terms"])
            self.assertIn("servpro", cfg["exclude_terms"])

    def test_generate_water_restoration_medium_configs_for_additional_ohio_metros(self) -> None:
        expected_geos = {
            "toledo": "Toledo OH",
            "akron": "Akron OH",
            "parma": "Parma OH",
            "hamilton": "Hamilton OH",
        }

        for market, primary_geo in expected_geos.items():
            path = self._generate(
                vertical="water_restoration",
                market=market,
                size="medium",
            )
            self.assertEqual(path.name, f"water_restoration_{market}_medium_001.json")
            cfg = self._load(path)
            self.assertEqual(cfg["vertical"], "water_restoration")
            self.assertEqual(cfg["airtable_vertical"], "Water Restoration")
            self.assertIn(f"water damage restoration {cfg['location']}", cfg["queries"])
            self.assertIn(primary_geo, cfg["geo_slices"])
            self.assertEqual(cfg["min_rating"], 4.3)
            self.assertEqual(cfg["min_reviews"], 10)

            canonical = list(VERTICAL_PROFILES["water_restoration"].high_ticket_terms)
            self.assertEqual(cfg["boost_terms"], canonical)
            self.assertIn("carpet cleaning", cfg["exclude_terms"])
            self.assertIn("servpro", cfg["exclude_terms"])

    def test_generate_garage_doors_medium_configs_for_ohio_secondary_cities(self) -> None:
        expected_geos = {
            "elyria": "Elyria OH",
            "lorain": "Lorain OH",
            "cuyahoga_falls": "Cuyahoga Falls OH",
            "kettering": "Kettering OH",
            "springfield": "Springfield OH",
            "newark": "Newark OH",
            "middletown": "Middletown OH",
            "youngstown": "Youngstown OH",
            "cleveland": "Cleveland OH",
            "columbus": "Columbus OH",
            "cincinnati": "Cincinnati OH",
        }

        for market, primary_geo in expected_geos.items():
            path = self._generate(
                vertical="garage_doors",
                market=market,
                size="medium",
            )
            self.assertEqual(path.name, f"garage_doors_{market}_medium_001.json")
            cfg = self._load(path)
            self.assertEqual(cfg["vertical"], "garage_doors")
            self.assertEqual(cfg["airtable_vertical"], "Garage Doors")
            self.assertIn(f"garage door repair {cfg['location']}", cfg["queries"])
            self.assertIn(primary_geo, cfg["geo_slices"])
            self.assertEqual(cfg["min_rating"], 4.3)
            self.assertEqual(cfg["min_reviews"], 10)

            canonical = list(VERTICAL_PROFILES["garage_doors"].high_ticket_terms)
            self.assertEqual(cfg["boost_terms"], canonical)
            self.assertIn("garage floor epoxy", cfg["exclude_terms"])
            self.assertIn("home depot", cfg["exclude_terms"])

    def test_generate_plumbing_cleveland_medium(self) -> None:
        path = self._generate(vertical="plumbing", market="cleveland", size="medium")
        self.assertEqual(path.name, "plumbing_cleveland_medium_001.json")
        cfg = self._load(path)
        self.assertEqual(cfg["vertical"], "plumbing")
        self.assertEqual(cfg["airtable_vertical"], "Plumbing")
        self.assertIn("plumber Cleveland OH", cfg["queries"])
        self.assertEqual(len(cfg["geo_slices"]), 6)
        self.assertEqual(cfg["max_pages_per_query"], 2)
        self.assertEqual(cfg["min_rating"], 4.3)
        self.assertEqual(cfg["min_reviews"], 5)
        self.assertEqual(
            cfg["boost_terms"],
            list(VERTICAL_PROFILES["plumbing"].high_ticket_terms),
        )
        self.assertIn("plumbing supply", cfg["exclude_terms"])
        self.assertIn("roto-rooter", cfg["exclude_terms"])

    def test_generate_roofing_cleveland_medium(self) -> None:
        path = self._generate(vertical="roofing", market="cleveland", size="medium")
        self.assertEqual(path.name, "roofing_cleveland_medium_001.json")
        cfg = self._load(path)
        self.assertEqual(cfg["vertical"], "roofing")
        self.assertEqual(cfg["airtable_vertical"], "Roofing")
        self.assertIn("roof replacement Cleveland OH", cfg["queries"])
        self.assertEqual(len(cfg["geo_slices"]), 6)
        self.assertEqual(cfg["min_rating"], 4.3)
        self.assertEqual(cfg["min_reviews"], 10)
        self.assertEqual(
            cfg["boost_terms"],
            list(VERTICAL_PROFILES["roofing"].high_ticket_terms),
        )
        # Supply houses (deny) and low-ticket roof work both get excluded.
        self.assertIn("abc supply", cfg["exclude_terms"])
        self.assertIn("gutter cleaning", cfg["exclude_terms"])

    def test_the_checked_in_roofing_config_still_matches_a_fresh_generation(self) -> None:
        # roofing_cleveland_medium_001.json was generated, not hand-authored, so
        # it must survive regeneration byte-for-byte. If this fails, either the
        # config was edited by hand or verticals.py/roofing.json drifted apart.
        generated = self._generate(vertical="roofing", market="cleveland", size="medium")
        checked_in = REPO_ROOT / "search_configs" / "roofing_cleveland_medium_001.json"

        self.assertEqual(generated.read_text(encoding="utf-8"), checked_in.read_text(encoding="utf-8"))

    def test_a_canonical_verticals_review_floor_comes_from_one_number(self) -> None:
        # The generated config's min_reviews is read from the JSON profile while
        # `icp.hard_icp_gate` reads VerticalProfile.min_reviews. epoxy and roofing
        # shipped 10 in the JSON and 8 in verticals.py, so the same lead could be
        # searched for and then skipped for having too few reviews. One number.
        for vertical in ("epoxy", "roofing", "detailing", "garage_doors", "plumbing", "water_restoration"):
            with self.subTest(vertical=vertical):
                path = self._generate(vertical=vertical, market="cleveland", size="medium")
                cfg = self._load(path)
                self.assertEqual(cfg["min_reviews"], VERTICAL_PROFILES[vertical].min_reviews)

    def test_epoxy_exclude_terms_are_the_canonical_deny_plus_low_ticket_plus_json(self) -> None:
        # Pinned deliberately: once epoxy joined verticals.py, regeneration went
        # from the JSON's 7 exclude terms to 21. The extra 14 are exactly the
        # deny + low-ticket terms `icp.hard_icp_gate` already skips as
        # `low_ticket_economics`, so search-time now refuses to pay for leads
        # score-time would throw away. The three live epoxy_cleveland_medium_*
        # configs still carry the old 7 and are stale relative to this.
        path = self._generate(vertical="epoxy", market="cleveland", size="medium")
        cfg = self._load(path)
        profile = VERTICAL_PROFILES["epoxy"]
        json_excludes = json.loads(
            (REAL_VERTICAL_DIR / "epoxy.json").read_text(encoding="utf-8")
        )["exclude_terms"]

        self.assertEqual(len(cfg["exclude_terms"]), 21)
        self.assertEqual(
            set(cfg["exclude_terms"]),
            set(profile.deny_terms) | set(profile.low_ticket_terms) | set(json_excludes),
        )
        # Boost terms are untouched by the merge — still the canonical 12.
        self.assertEqual(cfg["boost_terms"], list(profile.high_ticket_terms))

    def test_no_exclude_term_can_cancel_the_batchs_own_queries(self) -> None:
        # `make_search_batch.prefilter_row` matches exclude terms against a text
        # blob that includes source_query and source_geo. An exclude term that is
        # a substring of the batch's own queries or geos would drop every
        # unboosted row in the batch — the failure mode that makes widening the
        # exclude list dangerous.
        for vertical in ("epoxy", "roofing"):
            with self.subTest(vertical=vertical):
                path = self._generate(vertical=vertical, market="cleveland", size="medium")
                cfg = self._load(path)
                haystacks = [q.lower() for q in cfg["queries"]] + [g.lower() for g in cfg["geo_slices"]]
                for term in cfg["exclude_terms"]:
                    for haystack in haystacks:
                        self.assertNotIn(term.lower(), haystack)

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
