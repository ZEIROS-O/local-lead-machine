from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import make_search_batch
from lead_machine.airtable_sync import build_airtable_payload


def _config(**overrides):
    data = {
        "batch": "test_hardscaping_001",
        "vertical": "hardscaping",
        "location": "Cleveland OH",
        "limit_per_query": 5,
        "queries": ["hardscaping Cleveland OH", "patio contractor Cleveland OH"],
        "geo_slices": ["Cleveland OH", "Lakewood OH"],
        "min_rating": 4.0,
        "min_reviews": 10,
        "allow_missing_website": True,
        "exclude_terms": ["garden center", "nursery", "tree service", "snow removal only"],
        "boost_terms": ["patio", "pavers", "retaining wall", "masonry", "hardscape", "concrete"],
    }
    data.update(overrides)
    return make_search_batch.SearchConfig.from_dict(data, source_path=Path("search_configs/test.json"))


def _row(**overrides):
    row = {
        "business_name": "Patio Pros",
        "category": "landscaper",
        "city": "Cleveland",
        "address": "123 Main St, Cleveland, OH 44101, USA",
        "phone": "(216) 555-0100",
        "website_url": "https://patiopros.example",
        "google_maps_url": "https://maps.google.com/?cid=1",
        "rating": "4.8",
        "reviews": "28",
        "place_id": "place-1",
        "source_query": "hardscaping Cleveland OH",
        "source_geo": "Cleveland OH",
        "source_search": "hardscaping Cleveland OH",
        "source_batch": "test_hardscaping_001",
    }
    row.update(overrides)
    return row


class SearchWorkflowTest(unittest.TestCase):
    def test_search_config_parsing_requires_core_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(_config().to_dict()), encoding="utf-8")

            config = make_search_batch.load_search_config(path)

        self.assertEqual(config.batch, "test_hardscaping_001")
        self.assertEqual(config.vertical, "hardscaping")
        self.assertEqual(config.limit_per_query, 5)

        broken = _config().to_dict()
        broken.pop("queries")
        with self.assertRaises(ValueError) as caught:
            make_search_batch.SearchConfig.from_dict(broken, source_path=Path("broken.json"))
        self.assertIn("queries", str(caught.exception))

    def test_search_config_can_load_geo_slices_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "search_configs"
            geo_dir = config_dir / "geo_slices"
            geo_dir.mkdir(parents=True)
            (geo_dir / "cleveland.json").write_text(
                json.dumps(["Cleveland OH", "Lakewood OH", "Solon OH"]),
                encoding="utf-8",
            )
            data = _config().to_dict()
            data.pop("geo_slices")
            data["geo_slices_ref"] = "cleveland"

            config = make_search_batch.SearchConfig.from_dict(data, source_path=config_dir / "detailing.json")

        self.assertEqual(config.geo_slices, ["Cleveland OH", "Lakewood OH", "Solon OH"])

    def test_query_geo_expansion_replaces_base_location(self) -> None:
        expanded = make_search_batch.expand_searches(_config())

        self.assertEqual(
            [(item.source_query, item.source_geo, item.search_text) for item in expanded],
            [
                ("hardscaping Cleveland OH", "Cleveland OH", "hardscaping Cleveland OH"),
                ("hardscaping Cleveland OH", "Lakewood OH", "hardscaping Lakewood OH"),
                ("patio contractor Cleveland OH", "Cleveland OH", "patio contractor Cleveland OH"),
                ("patio contractor Cleveland OH", "Lakewood OH", "patio contractor Lakewood OH"),
            ],
        )

    def test_dedupe_prefers_place_id_then_phone(self) -> None:
        rows = [
            _row(business_name="First", place_id="same-place", phone="(216) 555-0100"),
            _row(business_name="Duplicate place", place_id="same-place", phone="(216) 555-0199"),
            _row(business_name="Duplicate phone", place_id="", phone="+1 216 555 0100"),
            _row(business_name="Unique", place_id="", phone="(216) 555-0111", website_url="https://unique.example"),
        ]

        kept, duplicate_reasons = make_search_batch.dedupe_search_rows(rows)

        self.assertEqual([row["business_name"] for row in kept], ["First", "Unique"])
        self.assertEqual(duplicate_reasons["duplicate_place_id"], 1)
        self.assertEqual(duplicate_reasons["duplicate_phone"], 1)
        self.assertEqual(len({row["place_id"] for row in kept if row.get("place_id")}), 1)
        self.assertEqual(len({make_search_batch.normalize_phone(row["phone"]) for row in kept}), 2)

    def test_normalized_output_includes_required_downstream_fields(self) -> None:
        normalized = make_search_batch.normalize_search_row(
            _row(place_id="", website_url="", phone="216.555.0100"),
            config=_config(),
            source_query="hardscaping Cleveland OH",
            source_geo="Lakewood OH",
            source_search="hardscaping Lakewood OH",
        )

        for field in make_search_batch.REQUIRED_FINAL_FIELDS:
            self.assertIn(field, normalized)

        self.assertEqual(normalized["Business Name"], "Patio Pros")
        self.assertEqual(normalized["Vertical"], "hardscaping")
        self.assertEqual(normalized["Source Geo"], "Lakewood OH")
        self.assertEqual(normalized["Source Batch"], "test_hardscaping_001")
        self.assertEqual(normalized["Place Key"], "phone:2165550100")
        self.assertEqual(normalized["phone"], "216.555.0100")

    def test_icp_prefilter_skips_obvious_junk_but_allows_strong_no_site_fit(self) -> None:
        config = _config()

        missing_phone = make_search_batch.prefilter_row(_row(phone=""), config)
        low_rating = make_search_batch.prefilter_row(_row(rating="3.9"), config)
        garden_center = make_search_batch.prefilter_row(
            _row(business_name="Cleveland Garden Center", category="garden center"),
            config,
        )
        strong_no_site = make_search_batch.prefilter_row(
            _row(
                business_name="Lakewood Patio Pavers",
                category="patio contractor",
                website_url="",
                reviews="2",
                rating="4.9",
                source_query="paver patio Lakewood OH",
            ),
            config,
        )

        self.assertEqual(missing_phone.reason, "missing_phone")
        self.assertEqual(low_rating.reason, "rating_below_min")
        self.assertEqual(garden_center.reason, "nursery_or_garden_center")
        self.assertFalse(strong_no_site.should_skip)
        self.assertIn("patio", strong_no_site.boost_terms)

    def test_green_industry_exclusions_do_not_apply_outside_hardscaping(self) -> None:
        """These five term lists were applied to every vertical.

        "property maintenance" and "facility maintenance" are ordinary
        vocabulary for a plumber or a restoration firm describing its commercial
        customers, but the prefilter dropped those leads before enrichment ever
        ran. The terms only mean "wrong trade" next to hardscaping, so they now
        live on that profile.
        """
        hardscaping = _config()
        plumbing = _config(
            vertical="plumbing",
            boost_terms=["plumber", "plumbing", "sewer", "water heater"],
            exclude_terms=[],
        )

        row = _row(
            business_name="Wilson Plumbing - property maintenance",
            category="plumber",
            source_query="emergency plumber Cleveland OH",
        )

        skipped = make_search_batch.prefilter_row(row, hardscaping)
        kept = make_search_batch.prefilter_row(row, plumbing)

        self.assertEqual(skipped.reason, "commercial_maintenance_only")
        self.assertFalse(kept.should_skip, kept.reason)

    def test_search_report_generation_lists_counts_outputs_and_next_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = make_search_batch.SearchRunResult(
                batch="test_hardscaping_001",
                vertical="hardscaping",
                config_path=Path("search_configs/test_hardscaping_001.json"),
                raw_path=root / "data/raw/test_hardscaping_001.csv",
                deduped_path=root / "data/deduped/test_hardscaping_001.csv",
                enriched_path=root / "output/enriched_test_hardscaping_001.jsonl",
                report_path=root / "reports/search_test_hardscaping_001.md",
                queries_run=["hardscaping Cleveland OH"],
                geo_slices=["Cleveland OH"],
                raw_count=3,
                deduped_count=2,
                icp_pass_count=1,
                enriched_count=1,
                skipped_count=1,
                skip_reason_counts={"missing_phone": 1},
                duplicate_reason_counts={"duplicate_place_id": 1},
                top_candidate_names=["Patio Pros"],
                warnings=[],
                errors=[],
            )

            make_search_batch.write_search_report(result)
            report = result.report_path.read_text(encoding="utf-8")

        self.assertIn("Raw result count: 3", report)
        self.assertIn("Deduped count: 2", report)
        self.assertIn("ICP pass count: 1", report)
        self.assertIn("Enriched count: 1", report)
        self.assertIn("missing_phone: 1", report)
        self.assertIn("data/raw/test_hardscaping_001.csv", report)
        self.assertIn("make batch BATCH=test_hardscaping_001 BATCH_SIZE=5 LIMIT=10", report)

    def test_legacy_config_backfills_page_size_and_defaults_to_single_page(self) -> None:
        # A config that only declares limit_per_query (no pagination fields) must keep
        # current single-page behavior and cost.
        config = _config()  # limit_per_query=5, no page_size/max_pages_per_query
        self.assertEqual(config.page_size, 5)
        self.assertEqual(config.effective_page_size, 5)
        self.assertEqual(config.max_pages_per_query, 1)
        self.assertIsNone(config.max_results_per_query)
        first = make_search_batch.expand_searches(config)[0]
        self.assertEqual(first.page_size, 5)
        self.assertEqual(first.max_pages, 1)

    def test_config_reads_explicit_page_size_and_max_pages(self) -> None:
        config = _config(page_size=20, max_pages_per_query=3)
        self.assertEqual(config.page_size, 20)
        self.assertEqual(config.max_pages_per_query, 3)
        first = make_search_batch.expand_searches(config)[0]
        self.assertEqual(first.max_pages, 3)
        self.assertEqual(first.total_max_results, 60)

    def test_runner_threads_pages_fetched_into_efficiency_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(_config(page_size=20, max_pages_per_query=2).to_dict()), encoding="utf-8"
            )

            def fake_paged(api_key, search_text, *, page_size, max_pages=1, max_results=None, sleep_seconds=0.0):
                return [{"id": f"{search_text}-1"}], 2

            def fake_details(api_key, place_id):
                suffix = sum(ord(char) for char in place_id) % 10000
                return {
                    "id": place_id,
                    "displayName": {"text": f"Patio Pros {suffix}"},
                    "formattedAddress": f"{suffix} Main St, Cleveland, OH 44101, USA",
                    "nationalPhoneNumber": f"(216) 555-{suffix:04d}",
                    "websiteUri": f"https://patiopros{suffix}.example",
                    "primaryType": "patio contractor",
                    "rating": 4.8,
                    "userRatingCount": 28,
                }

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

            with patch("make_search_batch.text_search_paged", side_effect=fake_paged), patch(
                "make_search_batch.place_details", side_effect=fake_details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(io.StringIO()):
                result = make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                    sleep_seconds=0,
                )

            report = result.report_path.read_text(encoding="utf-8")

        # 2 queries x 2 geos = 4 searches, 2 pages each = 8 pages fetched.
        self.assertEqual(result.efficiency.searches_run, 4)
        self.assertEqual(result.efficiency.max_pages_per_query, 2)
        self.assertEqual(result.efficiency.pages_fetched, 8)
        self.assertEqual(result.efficiency.page_size, 20)
        self.assertIn("Pages actually fetched: 8", report)
        self.assertIn("Max pages per query: 2", report)

    def test_report_includes_search_efficiency_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = make_search_batch.SearchRunResult(
                batch="test_hardscaping_001",
                vertical="hardscaping",
                config_path=Path("search_configs/test_hardscaping_001.json"),
                raw_path=root / "data/raw/test_hardscaping_001.csv",
                deduped_path=root / "data/deduped/test_hardscaping_001.csv",
                enriched_path=root / "output/enriched_test_hardscaping_001.jsonl",
                report_path=root / "reports/search_test_hardscaping_001.md",
                queries_run=["hardscaping Cleveland OH"],
                geo_slices=["Cleveland OH"],
                raw_count=30,
                deduped_count=20,
                icp_pass_count=12,
                enriched_count=12,
                skipped_count=8,
                skip_reason_counts={"missing_phone": 8},
                duplicate_reason_counts={"duplicate_place_id_early": 10},
                top_candidate_names=["Patio Pros"],
                warnings=[],
                errors=[],
                efficiency=make_search_batch.SearchEfficiency(
                    searches_run=6,
                    page_size=20,
                    max_pages_per_query=2,
                    pages_fetched=9,
                    total_places_returned=40,
                    unique_raw_count=30,
                    early_duplicate_count=10,
                ),
            )

            make_search_batch.write_search_report(result)
            report = result.report_path.read_text(encoding="utf-8")

        self.assertIn("## Search Efficiency", report)
        self.assertIn("Total query×geo searches: 6", report)
        self.assertIn("Configured page size: 20", report)
        self.assertIn("Max pages per query: 2", report)
        self.assertIn("Pages actually fetched: 9", report)
        self.assertIn("Raw results: 40", report)
        self.assertIn("Unique raw results: 30", report)
        self.assertIn("Early duplicate count: 10", report)
        self.assertIn("Unique enriched leads per search: 2.00", report)
        self.assertIn("Unique enriched leads per Places page: 1.33", report)

    def test_runner_writes_raw_deduped_enriched_and_report_without_real_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(_config().to_dict()), encoding="utf-8")

            text_results = [{"id": "place-1"}, {"id": "place-dup"}]
            details = {
                "place-1": {
                    "id": "place-1",
                    "displayName": {"text": "Patio Pros"},
                    "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                    "nationalPhoneNumber": "(216) 555-0100",
                    "websiteUri": "https://patiopros.example",
                    "googleMapsUri": "https://maps.google.com/?cid=1",
                    "rating": 4.8,
                    "userRatingCount": 28,
                    "primaryType": "landscaper",
                },
                "place-dup": {
                    "id": "place-dup",
                    "displayName": {"text": "Patio Pros Duplicate"},
                    "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                    "nationalPhoneNumber": "(216) 555-0100",
                    "websiteUri": "https://duplicate.example",
                    "googleMapsUri": "https://maps.google.com/?cid=2",
                    "rating": 4.8,
                    "userRatingCount": 28,
                    "primaryType": "landscaper",
                },
            }

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

            with patch("make_search_batch.text_search_paged", return_value=(text_results, 1)), patch(
                "make_search_batch.place_details", side_effect=lambda api_key, place_id: details[place_id]
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(io.StringIO()):
                result = make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                )

            self.assertEqual(result.raw_count, 2)
            self.assertEqual(result.deduped_count, 1)
            self.assertEqual(result.icp_pass_count, 1)
            self.assertEqual(result.enriched_count, 1)
            self.assertEqual(result.duplicate_reason_counts["duplicate_place_id_early"], 6)
            self.assertTrue(result.raw_path.exists())
            self.assertTrue(result.deduped_path.exists())
            self.assertTrue(result.enriched_path.exists())
            self.assertTrue(result.report_path.exists())

    def test_runner_gives_enrichment_a_review_snapshot_store(self) -> None:
        """Review velocity is only computable if something banks the counts.

        `record_snapshot` was called from `places_search.main` alone — the legacy
        `data/searches.csv` entry point. This runner never passed a store, so
        run_state/review_velocity.sqlite3 stayed empty, `build_review_velocity`
        was permanently stuck on `insufficient_history`, and a signal the rubric
        asks for could never be supplied for any lead the pipeline produced.
        """
        from lead_machine.review_velocity import ReviewSnapshotStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(_config().to_dict()), encoding="utf-8")

            snapshot_db = root / "run_state" / "review_velocity.sqlite3"
            received: list = []

            def fake_enrich(
                row,
                screenshots_dir,
                take_shots,
                rendered_site_check=False,
                suppression_index=None,
                review_snapshot_store=None,
                per_lead_timeout=None,
            ):
                received.append(review_snapshot_store)
                return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

            details = {
                "id": "place-1",
                "displayName": {"text": "Patio Pros"},
                "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0100",
                "websiteUri": "https://patiopros.example",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "rating": 4.8,
                "userRatingCount": 28,
                "primaryType": "landscaper",
            }

            with patch("make_search_batch.text_search_paged", return_value=([{"id": "place-1"}], 1)), patch(
                "make_search_batch.place_details", return_value=details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(io.StringIO()):
                make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    review_snapshot_db=snapshot_db,
                )

            self.assertEqual(len(received), 1)
            self.assertIsInstance(received[0], ReviewSnapshotStore)
            self.assertEqual(received[0].path, snapshot_db)

    def test_runner_can_pass_rendered_site_check_to_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(_config(queries=["hardscaping Cleveland OH"], geo_slices=["Cleveland OH"]).to_dict()),
                encoding="utf-8",
            )

            details = {
                "id": "place-1",
                "displayName": {"text": "Patio Pros"},
                "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0100",
                "websiteUri": "https://patiopros.example",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "rating": 4.8,
                "userRatingCount": 28,
                "primaryType": "landscaper",
            }
            calls = []

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                calls.append(
                    {
                        "take_shots": take_shots,
                        "rendered_site_check": rendered_site_check,
                        "screenshots_dir": str(screenshots_dir),
                    }
                )
                return {"google": row, "website": {"site_status": "Loaded"}, "derived": {}, "screenshots": {}}

            with patch("make_search_batch.text_search_paged", return_value=([{"id": "place-1"}], 1)), patch(
                "make_search_batch.place_details", return_value=details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(io.StringIO()):
                make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=False,
                    rendered_site_check=True,
                )

            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]["take_shots"])
            self.assertTrue(calls[0]["rendered_site_check"])
            self.assertTrue(calls[0]["screenshots_dir"].endswith("screenshots/test_hardscaping_001"))

    def test_runner_logs_completed_enrichment_elapsed_status_pages_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(_config(queries=["hardscaping Cleveland OH"], geo_slices=["Cleveland OH"]).to_dict()),
                encoding="utf-8",
            )

            details = {
                "id": "place-1",
                "displayName": {"text": "Patio Pros"},
                "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0100",
                "websiteUri": "https://patiopros.example",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "rating": 4.8,
                "userRatingCount": 28,
                "primaryType": "landscaper",
            }

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                return {
                    "google": row,
                    "website": {
                        "site_status": "Loaded",
                        "pages": {"contact": {"url": "https://patiopros.example/contact"}},
                        "homepage": {"error": ""},
                    },
                    "derived": {},
                    "screenshots": {},
                    "enrichment": {"elapsed_seconds": 2.4},
                }

            stdout = io.StringIO()
            with patch("make_search_batch.text_search_paged", return_value=([{"id": "place-1"}], 1)), patch(
                "make_search_batch.place_details", return_value=details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(stdout):
                make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                )

            log = stdout.getvalue()
            self.assertIn(
                "[ENRICH 1/1] Patio Pros elapsed=2.4s site_status=Loaded pages_checked=1 errors=",
                log,
            )

    def test_runner_skips_early_duplicate_place_ids_before_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(_config(queries=["hardscaping Cleveland OH"], geo_slices=["Cleveland OH"]).to_dict()), encoding="utf-8")

            text_results = [{"id": "same-place"}, {"id": "same-place"}]
            details = {
                "id": "same-place",
                "displayName": {"text": "Patio Pros"},
                "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0100",
                "websiteUri": "https://patiopros.example",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "rating": 4.8,
                "userRatingCount": 28,
                "primaryType": "landscaper",
            }

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

            with patch("make_search_batch.text_search_paged", return_value=(text_results, 1)), patch(
                "make_search_batch.place_details", return_value=details
            ) as details_call, patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(io.StringIO()):
                result = make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                    sleep_seconds=0,
                )

            self.assertEqual(details_call.call_count, 1)
            self.assertEqual(result.raw_count, 1)
            self.assertEqual(result.deduped_count, 1)
            self.assertEqual(result.duplicate_reason_counts["duplicate_place_id_early"], 1)

    def test_runner_applies_max_search_detail_and_enrich_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(_config().to_dict()), encoding="utf-8")

            def fake_text_search(api_key, search_text, *, page_size, max_pages=1, max_results=None, sleep_seconds=0.0):
                return [{"id": f"{search_text}-1"}, {"id": f"{search_text}-2"}], 1

            def fake_details(api_key, place_id):
                suffix = sum(ord(char) for char in place_id) % 10000
                return {
                    "id": place_id,
                    "displayName": {"text": f"Patio Pros {suffix}"},
                    "formattedAddress": f"{suffix} Main St, Cleveland, OH 44101, USA",
                    "nationalPhoneNumber": f"(216) 555-{suffix:04d}",
                    "websiteUri": f"https://patiopros{suffix}.example",
                    "googleMapsUri": f"https://maps.google.com/?cid={suffix}",
                    "rating": 4.8,
                    "userRatingCount": 28,
                    "primaryType": "patio contractor",
                }

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

            with patch("make_search_batch.text_search_paged", side_effect=fake_text_search) as search_call, patch(
                "make_search_batch.place_details", side_effect=fake_details
            ) as details_call, patch("make_search_batch.enrich_row", side_effect=fake_enrich) as enrich_call, redirect_stdout(io.StringIO()):
                result = make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                    sleep_seconds=0,
                    max_searches=2,
                    max_details=3,
                    max_enrich=1,
                )

            self.assertEqual(search_call.call_count, 2)
            self.assertEqual(details_call.call_count, 3)
            self.assertEqual(enrich_call.call_count, 1)
            self.assertEqual(len(result.queries_run), 2)
            self.assertEqual(result.raw_count, 3)
            self.assertEqual(result.icp_pass_count, 3)
            self.assertEqual(result.enriched_count, 1)

    def test_progress_logging_run_still_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(_config(queries=["hardscaping Cleveland OH"], geo_slices=["Cleveland OH"]).to_dict()), encoding="utf-8")

            text_results = [{"id": "place-1"}]
            details = {
                "id": "place-1",
                "displayName": {"text": "Patio Pros"},
                "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
                "nationalPhoneNumber": "(216) 555-0100",
                "websiteUri": "https://patiopros.example",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "rating": 4.8,
                "userRatingCount": 28,
                "primaryType": "patio contractor",
            }

            def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False, suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
                return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

            progress = io.StringIO()
            with patch("make_search_batch.text_search_paged", return_value=(text_results, 1)), patch(
                "make_search_batch.place_details", return_value=details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), redirect_stdout(progress):
                result = make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                    sleep_seconds=0,
                )

            output = progress.getvalue()
            self.assertIn("Total query×geo searches: 1", output)
            self.assertIn("[SEARCH 1/1] hardscaping Cleveland OH", output)
            self.assertIn("Raw count: 1", output)
            self.assertIn("Report:", output)
            self.assertIn("Next command: make batch BATCH=test_hardscaping_001 BATCH_SIZE=5 LIMIT=10", output)
            self.assertTrue(result.report_path.exists())

    def test_makefile_has_search_targets_and_preserves_manual_batch_default(self) -> None:
        makefile = Path("Makefile").read_text(encoding="utf-8")

        self.assertIn("BATCH ?= manual_existing", makefile)
        self.assertIn("MAX_SEARCHES ?=", makefile)
        self.assertIn("MAX_DETAILS ?=", makefile)
        self.assertIn("MAX_ENRICH ?=", makefile)
        self.assertRegex(makefile, r"(?m)^search:")
        self.assertRegex(makefile, r"(?m)^search-summary:")
        self.assertRegex(makefile, r"(?m)^full-from-search:")
        self.assertIn("$(if $(MAX_SEARCHES),--max-searches $(MAX_SEARCHES),)", makefile)
        self.assertIn("$(if $(MAX_DETAILS),--max-details $(MAX_DETAILS),)", makefile)
        self.assertIn("$(if $(MAX_ENRICH),--max-enrich $(MAX_ENRICH),)", makefile)
        self.assertIn("make_llm_batch.py --input $(ENRICHED)", makefile)

    def test_airtable_payload_omits_internal_debug_fields(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "A+ Landscaping",
                "Phone": "(216) 555-0100",
                "Evidence Refs": '["ev-google"]',
                "Safe Outreach Claims": "claim",
                "Unsafe Claims": "unsafe",
                "Minimum Evidence Passed": "Yes",
                "Packet Eligibility Reason": "debug reason",
                "Recommended Action": "Build mockup",
                "Site Status": "broken",
                "Rendered Site Status": "Loaded",
                "Rendered Status Reason": "Browser loaded after static fetch failed.",
                "Visual Review Needed": "Yes",
                "Visual Quality Score": "Unknown",
            },
        }

        fields = build_airtable_payload(card)["fields"]

        self.assertEqual(fields["Business Name"], "A+ Landscaping")
        self.assertNotIn("Recommended Action", fields)
        self.assertEqual(fields["Site Status"], "Broken")
        self.assertEqual(fields["Evidence Refs"], '["ev-google"]')
        self.assertEqual(fields["Safe Outreach Claims"], "claim")
        for internal_field in make_search_batch.INTERNAL_AIRTABLE_FIELDS:
            self.assertNotIn(internal_field, fields)
        self.assertNotIn("Rendered Site Status", fields)
        self.assertNotIn("Rendered Status Reason", fields)
        self.assertNotIn("Visual Review Needed", fields)
        self.assertNotIn("Visual Quality Score", fields)

    def test_airtable_payload_normalizes_site_status_single_select_values(self) -> None:
        cases = {
            "missing": "Missing",
            "broken": "Broken",
            "no_website": "No Website",
            "no website": "No Website",
            "loaded": "Loaded",
            "loads": "Loaded",
            "unknown": "Unknown",
            "redirect_issue": "Redirect Issue",
            "redirect issue": "Redirect Issue",
            "social_only": "Social Only",
            "social only": "Social Only",
            "facebook_only": "Social Only",
            "facebook only": "Social Only",
        }
        for site_status, expected in cases.items():
            with self.subTest(site_status=site_status):
                card = {
                    "place_id_or_unique_key": "ChIJ123",
                    "airtable_fields": {
                        "Business Name": "A+ Landscaping",
                        "Site Status": site_status,
                    },
                }

                fields = build_airtable_payload(card)["fields"]

                self.assertEqual(fields["Site Status"], expected)

    def test_airtable_payload_omits_unknown_site_status(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "A+ Landscaping",
                "Site Status": "parked_or_weird",
            },
        }

        fields = build_airtable_payload(card)["fields"]

        self.assertNotIn("Site Status", fields)

    def test_airtable_payload_keeps_ai_confidence_labels_lowercase(self) -> None:
        for confidence in ("medium", "high", "low"):
            with self.subTest(confidence=confidence):
                card = {
                    "place_id_or_unique_key": "ChIJ123",
                    "airtable_fields": {
                        "Business Name": "A+ Landscaping",
                        "AI Confidence": confidence,
                    },
                }

                fields = build_airtable_payload(card)["fields"]

                self.assertEqual(fields["AI Confidence"], confidence)

    def test_airtable_payload_maps_numeric_ai_confidence_to_label(self) -> None:
        cases = {
            "0.91": "high",
            "0.65": "medium",
            "0.35": "low",
        }
        for confidence, expected in cases.items():
            with self.subTest(confidence=confidence):
                card = {
                    "place_id_or_unique_key": "ChIJ123",
                    "airtable_fields": {
                        "Business Name": "A+ Landscaping",
                        "AI Confidence": confidence,
                    },
                }

                fields = build_airtable_payload(card)["fields"]

                self.assertEqual(fields["AI Confidence"], expected)

    def test_airtable_payload_omits_unknown_ai_confidence(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "A+ Landscaping",
                "AI Confidence": "certain-ish",
            },
        }

        fields = build_airtable_payload(card)["fields"]

        self.assertNotIn("AI Confidence", fields)

    def test_airtable_payload_coerces_typed_number_fields(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "A+ Landscaping",
                "Reviews": "130",
                "Rating": "4.9",
                "Score Total": "86",
            },
        }

        fields = build_airtable_payload(card)["fields"]

        self.assertEqual(fields["Reviews"], 130)
        self.assertEqual(fields["Rating"], 4.9)
        self.assertEqual(fields["Score Total"], 86)


if __name__ == "__main__":
    unittest.main()
