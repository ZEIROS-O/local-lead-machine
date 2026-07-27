from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lead_machine.review_velocity import ReviewSnapshotStore, build_review_velocity, mine_review_pain
from lead_machine.scoring_profiles import (
    ScoringProfileError,
    _deep_merge,
    infer_market,
    load_scoring_profile,
    validate_scoring_profile,
    validate_scoring_profile_directory,
)
from lead_machine.scoring_v3 import attach_scoring_v3, score_v3


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _review(index: int, days_ago: int, text: str = "Great work") -> dict:
    return {
        "review_id": f"r{index}",
        "timestamp": (NOW - timedelta(days=days_ago)).isoformat(),
        "rating": 5,
        "review_text": text,
    }


def _pilot_record(*, market: str = "cleveland", reviews: list[dict] | None = None) -> dict:
    city = "Cleveland" if market == "cleveland" else "Columbus"
    return {
        "vertical": "detailing",
        "google": {
            "business_name": "Mirror Finish Detailing",
            "category": "car_detailing_service",
            "city": city,
            "phone": "216-555-0100",
            "rating": "4.9",
            "reviews": "120",
            "place_id": "ChIJV3",
            "source_query": f"ceramic coating {city} OH",
        },
        "website": {"combined_text": "ceramic coating paint correction ppf"},
        "evidence": {
            "evidence_items": [
                {"id": "E1", "field": "google_rating", "value": "4.9 / 120"},
                {"id": "E2", "field": "form_strength", "value": "weak"},
            ]
        },
        "review_items": reviews or [],
        "review_collection": {"depth": 60, "observed_at": NOW.isoformat()},
    }


class ReviewVelocityTest(unittest.TestCase):
    def test_one_snapshot_is_unknown(self) -> None:
        result = build_review_velocity(
            {"review_count_history": [{"observed_at": NOW.isoformat(), "review_count": 500}]},
            now=NOW,
        )

        self.assertEqual(result["velocity_band"], "unknown")
        self.assertEqual(result["source"], "insufficient_history")

    def test_snapshot_delta_calculates_velocity_without_count_only_shortcut(self) -> None:
        result = build_review_velocity(
            {
                "review_count_history": [
                    {"observed_at": (NOW - timedelta(days=30)).isoformat(), "review_count": 100},
                    {"observed_at": NOW.isoformat(), "review_count": 120},
                ]
            },
            cohort_key="detailing__cleveland",
            cohort_rates=list(range(20)),
            now=NOW,
        )

        self.assertEqual(result["source"], "snapshot_delta")
        self.assertEqual(result["reviews_per_30d"], 20.0)
        self.assertEqual(result["velocity_band"], "active")

    def test_timestamped_reviews_can_bootstrap_surging_band(self) -> None:
        record = _pilot_record(reviews=[_review(index, index % 20) for index in range(1, 21)])
        result = build_review_velocity(
            record,
            cohort_key="detailing__cleveland",
            cohort_rates=[float(value) for value in range(20)],
            now=NOW,
        )

        self.assertEqual(result["source"], "timestamp_bootstrap")
        self.assertEqual(result["reviews_added_30d"], 20)
        self.assertEqual(result["velocity_band"], "surging")

    def test_truncated_timestamp_history_is_marked_lower_bound(self) -> None:
        record = _pilot_record(reviews=[_review(index, index % 60) for index in range(1, 61)])
        result = build_review_velocity(
            record,
            cohort_key="detailing__cleveland",
            cohort_rates=[float(value) for value in range(20)],
            now=NOW,
        )

        self.assertEqual(result["bound"], "lower_bound")
        self.assertLess(result["confidence"], 0.8)

    def test_negative_delta_is_anomaly_not_negative_momentum(self) -> None:
        result = build_review_velocity(
            {
                "review_count_history": [
                    {"observed_at": (NOW - timedelta(days=30)).isoformat(), "review_count": 120},
                    {"observed_at": NOW.isoformat(), "review_count": 110},
                ]
            },
            cohort_rates=list(range(20)),
            now=NOW,
        )

        self.assertEqual(result["velocity_band"], "unknown")
        self.assertIn("negative_review_count_delta", result["anomalies"])

    def test_reliably_zero_over_ninety_days_is_stale_even_in_zero_heavy_cohort(self) -> None:
        result = build_review_velocity(
            {
                "review_count_history": [
                    {"observed_at": (NOW - timedelta(days=90)).isoformat(), "review_count": 100},
                    {"observed_at": NOW.isoformat(), "review_count": 100},
                ]
            },
            cohort_rates=[0.0] * 20,
            now=NOW,
        )

        self.assertEqual(result["reviews_added_90d"], 0)
        self.assertEqual(result["velocity_band"], "stale")
        self.assertEqual(result["score"], 2)

    def test_stale_review_observation_expires_to_unknown(self) -> None:
        observed = NOW - timedelta(days=120)
        result = build_review_velocity(
            {
                "review_count_history": [
                    {"observed_at": (observed - timedelta(days=30)).isoformat(), "review_count": 100},
                    {"observed_at": observed.isoformat(), "review_count": 120},
                ]
            },
            cohort_rates=list(range(20)),
            max_age_days=45,
            now=NOW,
        )

        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["velocity_band"], "unknown")
        self.assertIn("stale_review_observation", result["anomalies"])

    def test_same_rate_is_normalized_differently_by_cell(self) -> None:
        record = {
            "review_count_history": [
                {"observed_at": (NOW - timedelta(days=30)).isoformat(), "review_count": 100},
                {"observed_at": NOW.isoformat(), "review_count": 110},
            ]
        }
        slow_cell = build_review_velocity(record, cohort_rates=[float(value) for value in range(20)], now=NOW)
        fast_cell = build_review_velocity(record, cohort_rates=[float(value) for value in range(10, 30)], now=NOW)

        self.assertEqual(slow_cell["velocity_band"], "active")
        self.assertEqual(fast_cell["velocity_band"], "stale")

    def test_snapshot_store_persists_history_and_cohort_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewSnapshotStore(Path(temp_dir) / "velocity.sqlite3")
            for index in range(20):
                key = f"place-{index}"
                store.record_snapshot(
                    place_key=key,
                    review_count=100,
                    vertical="detailing",
                    market="cleveland",
                    observed_at=NOW - timedelta(days=30),
                )
                store.record_snapshot(
                    place_key=key,
                    review_count=105 + index,
                    vertical="detailing",
                    market="cleveland",
                    observed_at=NOW,
                )

            self.assertEqual(len(store.history("place-1")), 2)
            self.assertEqual(len(store.cohort_rates(vertical="detailing", market="cleveland")), 20)

    def test_enrichment_banks_the_observation_it_reads_history_from(self) -> None:
        """Enrichment used to read the store and never write to it.

        `record_snapshot` was called only from `places_search.main`, so on every
        other path the store could not accumulate a second count 14 days later
        and velocity was structurally unmeasurable. Enrichment is the one stage
        every entry point runs, so the observation has to be banked here.
        """
        import enrich_leads

        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewSnapshotStore(Path(temp_dir) / "velocity.sqlite3")
            row = {
                "business_name": "Patio Pros",
                "place_id": "place-xyz",
                "city": "Cleveland",
                "rating": "4.8",
                "reviews": "28",
                "website_url": "",
                "phone": "(216) 555-0100",
                "category": "hardscaping",
            }

            record = enrich_leads.enrich_row(
                row,
                screenshots_dir=Path(temp_dir) / "shots",
                take_shots=False,
                review_snapshot_store=store,
            )

            history = store.history("place-xyz")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["review_count"], 28)
            self.assertEqual(history[0]["source"], "enrichment")
            # The record carries the history it just contributed to, so a later
            # run has a prior count to measure a delta against.
            self.assertEqual(len(record["review_count_history"]), 1)

    def test_a_snapshot_failure_does_not_lose_the_lead(self) -> None:
        import enrich_leads

        class ExplodingStore(ReviewSnapshotStore):
            def record_snapshot(self, **kwargs):
                raise sqlite3.OperationalError("database is locked")

            def history(self, place_key):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            record = enrich_leads.enrich_row(
                {
                    "business_name": "Patio Pros",
                    "place_id": "place-xyz",
                    "city": "Cleveland",
                    "rating": "4.8",
                    "reviews": "28",
                    "website_url": "",
                    "category": "hardscaping",
                },
                screenshots_dir=Path(temp_dir) / "shots",
                take_shots=False,
                review_snapshot_store=ExplodingStore(Path(temp_dir) / "velocity.sqlite3"),
            )

        self.assertEqual(record["google"]["business_name"], "Patio Pros")


class ReviewPainTest(unittest.TestCase):
    def test_two_independent_reviews_verify_operational_pain(self) -> None:
        profile = mine_review_pain(
            _pilot_record(
                reviews=[
                    _review(1, 5, "I called twice and no one answered."),
                    _review(2, 20, "Could not reach anyone for an appointment."),
                ]
            )
        )

        self.assertIn("unreachable", profile["verified_categories"])
        self.assertIn("Answer It", profile["offer_lanes"])
        self.assertEqual(len(profile["evidence_refs"]), 2)

    def test_workmanship_complaint_is_risk_not_positive_pain(self) -> None:
        profile = mine_review_pain(
            _pilot_record(reviews=[_review(1, 5, "Terrible job and they damaged my paint.")])
        )

        self.assertEqual(profile["verified_categories"], [])
        self.assertIn("service_quality", profile["reputation_risk_categories"])

    def test_old_operational_complaints_do_not_create_current_pain(self) -> None:
        profile = mine_review_pain(
            _pilot_record(
                reviews=[
                    _review(1, 400, "No one answered when I called."),
                    _review(2, 420, "Could not reach anyone."),
                ]
            ),
            now=NOW,
            recency_days=180,
        )

        self.assertEqual(profile["verified_categories"], [])

    def test_classifier_matches_do_not_verify_without_independent_verifier(self) -> None:
        profile = mine_review_pain(
            _pilot_record(
                reviews=[
                    _review(1, 5, "No one answers emails."),
                    _review(2, 10, "No one answers messages."),
                ]
            ),
            now=NOW,
        )

        self.assertEqual(profile["verified_categories"], [])

    def test_one_severe_review_can_pass_with_explicit_structural_corroboration(self) -> None:
        record = _pilot_record(reviews=[_review(1, 5, "No one answered when I called.")])
        record["review_items"][0]["rating"] = 1
        record["review_pain_corroboration"] = {"unreachable": ["phone-test:three-unanswered-calls"]}
        record["evidence"]["evidence_items"].append(
            {"id": "phone-test:three-unanswered-calls", "field": "phone_test", "value": "three unanswered calls"}
        )

        profile = mine_review_pain(record, now=NOW)

        self.assertEqual(profile["verified_categories"], ["unreachable"])
        self.assertTrue(profile["findings"][0]["verifier_result"].startswith("PASS:"))
        self.assertEqual(
            profile["findings"][0]["corroborating_evidence_refs"],
            ["phone-test:three-unanswered-calls"],
        )

    def test_stale_review_collection_cannot_keep_pain_active(self) -> None:
        record = _pilot_record(
            reviews=[
                _review(1, 120, "No one answered when I called."),
                _review(2, 125, "Could not reach anyone for an appointment."),
            ]
        )
        record["review_collection"]["observed_at"] = (NOW - timedelta(days=100)).isoformat()
        profile = mine_review_pain(record, now=NOW, max_observation_age_days=45)

        self.assertEqual(profile["state"], "unknown")
        self.assertEqual(profile["verified_categories"], [])
        self.assertIn("stale_review_observation", profile["anomalies"])


class ScoringV3Test(unittest.TestCase):
    def _row(self) -> dict:
        return {
            "Email": "owner@example.com",
            "Phone": "216-555-0100",
            "Rating": "4.9",
            "Reviews": "120",
            "Visible Pain Score": "40",
            "Send Trigger": "Weak estimate path",
            "Offer Fit": "Estimate Funnel",
            "LPM Offer Component": "Get the Lead",
            "Sendable": "No",
        }

    def test_exact_pilot_profile_is_required(self) -> None:
        profile = load_scoring_profile("detailing", "cleveland")
        self.assertEqual(sum(profile.weights.values()), 100)
        with self.assertRaises(ScoringProfileError):
            load_scoring_profile("detailing", "toledo")

    def test_plumbing_cleveland_shadow_profile_is_exact_and_versioned(self) -> None:
        profile = load_scoring_profile("plumbing", "cleveland")

        self.assertEqual(profile.profile_id, "plumbing__cleveland")
        self.assertEqual(profile.profile_version, "1.0.0")
        self.assertEqual(profile.launch_mode, "shadow")
        self.assertIn("sewer line replacement", profile.high_value_services)
        self.assertIn("plumbing supply", profile.exclusions)

    def test_batch_market_scope_precedes_overlapping_query_geography(self) -> None:
        record = {
            "google": {
                "city": "Parma",
                "source_batch": "plumbing_cleveland_medium_001",
                "source_geo": "Parma OH",
                "source_query": "plumber Parma OH",
            }
        }

        self.assertEqual(infer_market(record), "cleveland")

    def test_conflicting_explicit_markets_fail_closed(self) -> None:
        record = {
            "Metro Context": "cleveland",
            "market": "parma",
            "google": {"source_batch": "plumbing_cleveland_medium_001"},
        }

        self.assertEqual(infer_market(record), "")

    def test_plumbing_profile_rejects_known_franchise_and_supplier_noise(self) -> None:
        for business_name in ("ARS / Rescue Rooter", "Menyhart Plumbing & Heating Supply Co"):
            with self.subTest(business_name=business_name):
                record = {
                    "vertical": "plumbing",
                    "google": {
                        "business_name": business_name,
                        "category": "plumber",
                        "source_batch": "plumbing_cleveland_medium_001",
                    },
                }

                result = score_v3(record, self._row())

                self.assertEqual(result.disposition, "reject")
                self.assertIn("excluded_business_type_or_service", result.hard_disqualifiers)

    def test_profile_hierarchy_and_directory_validation(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scoring_profiles"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in (
                "global.json",
                "vertical__detailing.json",
                "metro__cleveland.json",
                "detailing__cleveland.json",
            ):
                (root / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
            global_raw = json.loads((root / "global.json").read_text(encoding="utf-8"))
            global_raw["supported_cells"] = ["detailing__cleveland"]
            (root / "global.json").write_text(json.dumps(global_raw), encoding="utf-8")
            metro_raw = json.loads((root / "metro__cleveland.json").read_text(encoding="utf-8"))
            metro_raw["minimum_cohort_size"] = 25
            (root / "metro__cleveland.json").write_text(json.dumps(metro_raw), encoding="utf-8")

            self.assertEqual(validate_scoring_profile_directory(root), ("detailing__cleveland",))
            profile = load_scoring_profile("detailing", "cleveland", profiles_dir=root)
            self.assertEqual(profile.minimum_cohort_size, 20)
            self.assertIn("ceramic coating", profile.vertical_terms)

    def test_profile_validation_rejects_duplicate_and_conflicting_terms(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scoring_profiles"
        raw = {}
        for name in (
            "global.json",
            "vertical__detailing.json",
            "metro__cleveland.json",
            "detailing__cleveland.json",
        ):
            raw = _deep_merge(raw, json.loads((source / name).read_text(encoding="utf-8")))
        raw["vertical_terms"] = list(raw["vertical_terms"]) + [raw["vertical_terms"][0].upper()]
        with self.assertRaisesRegex(ScoringProfileError, "duplicate terms"):
            validate_scoring_profile(raw, expected_vertical="detailing", expected_market="cleveland")

    def test_review_velocity_alone_cannot_make_lead_auto_eligible(self) -> None:
        record = _pilot_record(reviews=[_review(index, index % 15) for index in range(1, 21)])
        row = self._row()
        row["Visible Pain Score"] = "0"
        row["Send Trigger"] = ""
        result = score_v3(record, row, cohort_rates=list(range(20)))

        self.assertEqual(result.review_velocity["velocity_band"], "surging")
        self.assertEqual(result.disposition, "manual-review")
        self.assertIn("missing_verified_named_leak", result.blocking_gates)

    def test_missing_independent_sendability_verdict_fails_closed(self) -> None:
        record = _pilot_record(reviews=[_review(index, index % 15) for index in range(1, 21)])
        row = self._row()
        row.pop("Sendable")
        result = score_v3(record, row, cohort_rates=list(range(20)))

        self.assertEqual(result.disposition, "manual-review")
        self.assertIn("independent_sendability_gate_not_passed", result.blocking_gates)

    def test_duplicate_evidence_cannot_unlock_confidence_gate(self) -> None:
        record = _pilot_record()
        record["evidence"]["evidence_items"] = [{"id": "E1"}, {"id": "E1"}]
        row = self._row()
        row["Sendable"] = "Yes"
        result = score_v3(record, row)

        self.assertEqual(result.component_scores["evidence_confidence"], 1)
        self.assertEqual(result.disposition, "manual-review")
        self.assertIn("insufficient_evidence_confidence", result.blocking_gates)

    def test_known_outside_market_is_rejected(self) -> None:
        record = _pilot_record()
        record["market"] = "toledo"
        result = score_v3(record, self._row())

        self.assertEqual(result.disposition, "reject")
        self.assertIn("outside_target_geography", result.hard_disqualifiers)

    def test_fresh_external_observation_contributes_named_leak_and_offer(self) -> None:
        record = _pilot_record()
        record["signal_observations"] = [
            {
                "signal_id": "pagespeed:mobile:https://example.com",
                "kind": "website_performance",
                "state": "present",
                "value": {"performance_score": 0.3},
                "source": "pagespeed_insights_v5",
                "observed_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(days=30)).isoformat(),
                "confidence": 0.9,
                "evidence_refs": [],
                "metadata": {"url": "https://example.com", "strategy": "mobile"},
            }
        ]
        row = self._row()
        row["Sendable"] = "Yes"
        row["Visible Pain Score"] = "0"
        row["Send Trigger"] = ""
        row["LPM Offer Component"] = ""
        row["Offer Fit"] = ""

        result = score_v3(record, row)

        self.assertEqual(result.offer_lane, "Get Found")
        self.assertIn("poor mobile performance", result.named_leak.lower())
        self.assertEqual(result.disposition, "auto-eligible")
        self.assertTrue(result.contributing_observations)

    def test_stale_external_observation_becomes_unknown_and_cannot_supply_leak(self) -> None:
        record = _pilot_record()
        observed = NOW - timedelta(days=90)
        record["signal_observations"] = [
            {
                "signal_id": "pagespeed:old",
                "kind": "website_performance",
                "state": "present",
                "value": {"performance_score": 0.2},
                "source": "pagespeed_insights_v5",
                "observed_at": observed.isoformat(),
                "expires_at": (observed + timedelta(days=30)).isoformat(),
                "confidence": 0.9,
                "evidence_refs": [],
                "metadata": {},
            }
        ]
        row = self._row()
        row["Sendable"] = "Yes"
        row["Visible Pain Score"] = "0"
        row["Send Trigger"] = ""
        row["LPM Offer Component"] = ""
        row["Offer Fit"] = ""

        result = score_v3(record, row)

        self.assertEqual(result.named_leak, "")
        self.assertEqual(result.disposition, "manual-review")
        self.assertEqual(result.unknown_observations[0]["state"], "unknown")

    def test_verified_velocity_and_named_leak_produce_high_rank(self) -> None:
        record = _pilot_record(reviews=[_review(index, index % 15) for index in range(1, 21)])
        row = self._row()
        row["Sendable"] = "Yes"
        result = score_v3(record, row, cohort_rates=list(range(20)))

        self.assertEqual(result.rank_score, 98)
        self.assertEqual(result.disposition, "auto-eligible")

    def test_shadow_fields_do_not_overwrite_current_sendability(self) -> None:
        record = _pilot_record(reviews=[_review(index, index % 15) for index in range(1, 21)])
        updated = attach_scoring_v3(record, self._row(), cohort_rates=list(range(20)))

        self.assertEqual(updated["Sendable"], "No")
        self.assertEqual(updated["Scoring V3 Mode"], "shadow")
        self.assertEqual(updated["Scoring V3 Disposition"], "manual-review")
        self.assertIn("independent_sendability_gate_not_passed", updated["Scoring V3 Blockers"])


if __name__ == "__main__":
    unittest.main()
