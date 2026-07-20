from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lead_machine.airtable_receipts import write_verified_cohort_receipt
from lead_machine.dashboard.data import DashboardRepository
from lead_machine.hermes_batch import BatchPaths
from lead_machine.hermes_state import current_sync_hash
from lead_machine.sendability import PACKET_TIER_MANUAL, PACKET_TIER_SENDABLE


BATCH = "detailing_cleveland_medium_001"


def make_root(root: Path) -> BatchPaths:
    for directory in (
        "search_configs",
        "vertical_profiles",
        "market_profiles",
        "data/raw",
        "data/deduped",
        "output",
        "run_state",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "vertical_profiles/detailing.json").write_text("{}\n", encoding="utf-8")
    (root / "market_profiles/cleveland.json").write_text("{}\n", encoding="utf-8")
    (root / f"search_configs/{BATCH}.json").write_text(
        json.dumps({"batch": BATCH, "vertical": "detailing", "location": "Cleveland OH", "queries": ["ceramic coating"]}),
        encoding="utf-8",
    )
    return BatchPaths.for_batch(BATCH, root=root)


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = fieldnames or list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def enriched_rows(count: int) -> list[dict]:
    return [
        {
            "google": {
                "business_name": f"Lead {index}",
                "city": "Cleveland",
                "website_url": f"https://lead-{index}.example",
                "place_key": f"place-{index}",
                "phone": "555-secret",
                "email": "secret@example.com",
            }
        }
        for index in range(count)
    ]


def write_enriched(path: Path, count: int) -> None:
    path.write_text("\n".join(json.dumps(row) for row in enriched_rows(count)) + "\n", encoding="utf-8")


def scored_rows(count: int) -> list[dict[str, str]]:
    rows = []
    for index in range(count):
        sendable = index % 2 == 0
        rows.append(
            {
                "Place Key": f"place-{index}",
                "Business Name": f"Lead {index}",
                "City": "Cleveland",
                "Phone": "555-secret",
                "Email": "secret@example.com",
                "Website URL": f"https://lead-{index}.example",
                "Google Maps URL": f"https://maps.example/{index}",
                "Score Total": str(90 - index),
                "Lead Priority": "A" if sendable else "B",
                "Packet Tier": PACKET_TIER_SENDABLE if sendable else PACKET_TIER_MANUAL,
                "Sendable": "Yes" if sendable else "No",
                "Packet Eligible": "Yes",
                "Score Schema Version": "2",
                "Commercial Fit State": "commercially-sendable" if sendable else "needs-broader-system-evidence",
                "Commercial Materiality": "Material consequence",
                "Recurring Consequence": "Recurring consequence",
                "Root Cause Confidence": "High confidence",
                "LPM Offer Component": "Get the Lead",
                "Packageable Solution": "Estimate funnel",
                "Value Mechanism": "More completed inquiries",
                "Beyond Free Fix": "Multi-step conversion path",
                "Self Fix Risk": "Low",
                "Intentionality": "Not intentional",
                "Contact Eligibility": "contact-now" if sendable else "hold-commercial-case",
                "Asset State": "none",
                "Promise State": "asset-free",
                "Asset ID": "",
                "Asset Verifier Verdict": "",
                "Money Signal": "Yes" if sendable else "No",
                "AI Confidence": "high",
                "Send Blockers": "" if sendable else "Needs review",
                "Opener": "Sensitive opener",
            }
        )
    return rows


class DashboardRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = make_root(self.root)
        self.repo = DashboardRepository(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_config_only_batch_is_available_and_healthy(self) -> None:
        batch = self.repo.batch(BATCH)
        assert batch is not None
        self.assertEqual(batch["progress"], "configured")
        self.assertEqual(batch["health"], "healthy")
        self.assertEqual(batch["next_action"], "/run detailing cleveland medium")
        self.assertEqual(self.repo.overview()["totals"]["batches"], 1)

    def test_partial_scoring_uses_artifact_counts_not_stale_state(self) -> None:
        write_enriched(self.paths.enriched, 3)
        write_csv(self.paths.scored_csv, scored_rows(2))
        self.paths.state_json.write_text(
            json.dumps({"batch": BATCH, "enriched_rows": 99, "scored_rows": 0}),
            encoding="utf-8",
        )
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["progress"], "partial_scoring")
        self.assertEqual(batch["counts"]["enriched"], 3)
        self.assertEqual(batch["counts"]["scored"], 2)
        self.assertEqual(batch["counts"]["unscored"], 1)
        self.assertIn("state_count_drift", {issue["code"] for issue in batch["issues"]})
        self.assertEqual(batch["next_action"], f"/score-dry {BATCH}")

    def test_legacy_batch_uses_literal_counts_and_historical_packets(self) -> None:
        write_enriched(self.paths.enriched, 2)
        legacy_rows = scored_rows(2)
        for row in legacy_rows:
            for key in (
                "Score Schema Version",
                "Commercial Fit State",
                "Contact Eligibility",
                "Asset State",
                "Promise State",
            ):
                row.pop(key, None)
        write_csv(self.root / f"output/scored_{BATCH}.csv", legacy_rows)
        registry = self.root / "lead_machine/dashboard/legacy_batches.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "batches": [
                        {
                            "batch": BATCH,
                            "legacy_since": "2026-07-20",
                            "reason": "pre_commercial_readiness_schema",
                            "note": "Preserved history",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        batch = DashboardRepository(self.root).batch(BATCH, force=True)

        assert batch is not None
        self.assertEqual(batch["lifecycle"], "legacy")
        self.assertEqual(batch["counts"]["scored"], 2)
        self.assertEqual(batch["current_sendability"]["status"], "incompatible")
        self.assertIsNone(batch["current_sendability"]["count"])
        self.assertEqual(batch["historical_packets"]["sendable"], 1)

    def test_unreconciled_active_batch_reports_unknown_unscored(self) -> None:
        write_enriched(self.paths.enriched, 2)
        rows = scored_rows(2)
        rows[1]["Place Key"] = "not-in-enrichment"
        rows[1]["Phone"] = ""
        rows[1]["Website URL"] = ""
        rows[1]["Business Name"] = "Different"
        rows[1]["City"] = "Elsewhere"
        write_csv(self.paths.scored_csv, rows)

        batch = self.repo.batch(BATCH, force=True)

        assert batch is not None
        self.assertEqual(batch["counts"]["scored"], 2)
        self.assertIsNone(batch["counts"]["unscored"])
        self.assertEqual(batch["reconciliation"]["status"], "unknown")

    def test_manual_scored_csv_is_a_canonical_dashboard_fallback(self) -> None:
        write_enriched(self.paths.enriched, 2)
        write_csv(self.paths.manual_scored_csv, scored_rows(2))

        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["progress"], "scored")
        self.assertEqual(batch["counts"]["scored"], 2)
        self.assertEqual(batch["counts"]["unscored"], 0)
        self.assertEqual(batch["artifacts"]["scored"]["path"], f"output/scored_{BATCH}_manual.csv")
        self.assertEqual(self.repo.leads(BATCH, state="scored", force=True)["total"], 2)

    def test_api_scored_csv_takes_precedence_over_manual_fallback(self) -> None:
        write_enriched(self.paths.enriched, 2)
        write_csv(self.paths.manual_scored_csv, scored_rows(2))
        write_csv(self.paths.scored_csv, scored_rows(1))

        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["progress"], "partial_scoring")
        self.assertEqual(batch["counts"]["scored"], 1)
        self.assertEqual(batch["artifacts"]["scored"]["path"], f"output/scored_{BATCH}_api.csv")

    def test_verified_custom_airtable_cohort_is_visible_but_not_full_completion(self) -> None:
        write_enriched(self.paths.enriched, 2)
        write_csv(self.paths.manual_scored_csv, scored_rows(2))
        write_verified_cohort_receipt(
            self.paths,
            cohort="grades-a-b-plus-sendable",
            identities=["place-0", "place-1"],
            live_post_count=2,
            live_patch_count=0,
            verification_patch_count=2,
        )

        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["progress"], "scored")
        self.assertEqual(batch["stages"]["airtable"], {"status": "partial", "count": 2})
        self.assertEqual(batch["airtable"]["live"]["status"], "partial")
        self.assertEqual(batch["airtable"]["custom"]["status"], "verified")
        self.assertEqual(batch["airtable"]["custom"]["records"], 2)
        self.assertIn("partial_airtable_sync", {issue["code"] for issue in batch["issues"]})
        self.assertTrue(batch["artifacts"]["airtable_custom_receipt"]["exists"])

    def test_current_packets_and_matching_live_receipt_are_finished(self) -> None:
        write_enriched(self.paths.enriched, 2)
        rows = scored_rows(2)
        write_csv(self.paths.scored_csv, rows)
        write_csv(self.paths.tiered_csv, rows)
        self.paths.packets_dir.mkdir(parents=True)
        write_csv(
            self.paths.packet_index,
            [
                {"Place Key": "place-0", "Business Name": "Lead 0", "City": "Cleveland", "Packet Tier": PACKET_TIER_SENDABLE},
                {"Place Key": "place-1", "Business Name": "Lead 1", "City": "Cleveland", "Packet Tier": PACKET_TIER_MANUAL},
            ],
        )
        sync_hash = current_sync_hash(self.paths, include_manual=True)
        now = datetime.now(timezone.utc).isoformat()
        self.paths.state_json.write_text(
            json.dumps(
                {
                    "batch": BATCH,
                    "enriched_rows": 2,
                    "scored_rows": 2,
                    "packet_count": 2,
                    "sendable_count": 1,
                    "manual_review_count": 1,
                    "last_airtable_dry_run_timestamp": now,
                    "dry_run_input_hash": sync_hash,
                    "dry_run_include_manual": True,
                    "dry_run_post_count": 1,
                    "dry_run_patch_count": 1,
                    "dry_run_skip_count": 0,
                    "dry_run_warnings": [],
                    "last_live_sync_timestamp": now,
                    "live_sync_input_hash": sync_hash,
                    "live_post_count": 1,
                    "live_patch_count": 1,
                    "live_skip_count": 0,
                }
            ),
            encoding="utf-8",
        )
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["progress"], "finished")
        self.assertEqual(batch["airtable"]["live"]["status"], "current")

        state = json.loads(self.paths.state_json.read_text(encoding="utf-8"))
        state["dry_run_skip_count"] = []
        self.paths.state_json.write_text(json.dumps(state), encoding="utf-8")
        malformed = self.repo.batch(BATCH, force=True)
        assert malformed is not None
        self.assertEqual(malformed["health"], "blocked")
        self.assertNotEqual(malformed["progress"], "finished")
        self.assertIn("invalid_run_state_dry_run_skip_count", {issue["code"] for issue in malformed["issues"]})
        state["dry_run_skip_count"] = 0
        self.paths.state_json.write_text(json.dumps(state), encoding="utf-8")

        write_csv(
            self.paths.packet_index,
            [
                {"Place Key": "place-0", "Business Name": "Lead 0", "City": "Cleveland", "Packet Tier": PACKET_TIER_SENDABLE},
                {"Place Key": "place-substituted", "Business Name": "Lead 1", "City": "Cleveland", "Packet Tier": PACKET_TIER_MANUAL},
            ],
        )
        changed = self.repo.batch(BATCH, force=True)
        assert changed is not None
        self.assertNotEqual(changed["progress"], "finished")
        self.assertIn("stale_packets", {issue["code"] for issue in changed["issues"]})
        self.assertIn("packet_identity_drift", {issue["code"] for issue in changed["issues"]})

    def test_sendable_tier_without_shared_readiness_is_blocked_and_not_counted(self) -> None:
        write_enriched(self.paths.enriched, 1)
        rows = scored_rows(1)
        rows[0]["Contact Eligibility"] = "hold-commercial-case"
        write_csv(self.paths.scored_csv, rows)
        write_csv(self.paths.tiered_csv, rows)
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["counts"]["sendable"], 0)
        self.assertEqual(batch["health"], "blocked")
        self.assertIn("sendable_readiness_mismatch", {issue["code"] for issue in batch["issues"]})

        leads = self.repo.leads(BATCH, force=True)
        assert leads is not None
        self.assertEqual(leads["items"][0]["sendable"], False)
        self.assertEqual(leads["items"][0]["declared_sendable"], True)
        self.assertEqual(leads["items"][0]["readiness_mismatch"], True)
        filtered = self.repo.leads(BATCH, sendable=True, force=True)
        assert filtered is not None
        self.assertEqual(filtered["total"], 0)

    def test_nonfinite_and_negative_usage_values_fail_visible_without_invalid_json(self) -> None:
        self.paths.usage_json.write_text(
            '{"calls": Infinity, "total_tokens": -10, "estimated_usd": NaN, '
            '"caps": {"MAX_LLM_CALLS_PER_RUN": 10, "MAX_LLM_TOKENS_PER_RUN": 1000, '
            '"MAX_LLM_USD_PER_RUN": -2}}',
            encoding="utf-8",
        )
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["health"], "blocked")
        self.assertEqual(batch["llm_usage"]["calls"], 0)
        self.assertEqual(batch["llm_usage"]["tokens"], 0)
        self.assertEqual(batch["llm_usage"]["estimated_usd"], 0.0)
        self.assertEqual(batch["llm_usage"]["usd_cap"], 0.0)
        codes = {issue["code"] for issue in batch["issues"]}
        self.assertIn("invalid_llm_usage_calls", codes)
        self.assertIn("invalid_llm_usage_total_tokens", codes)
        self.assertIn("invalid_llm_usage_estimated_usd", codes)
        self.assertIn("invalid_llm_usage_caps_max_llm_usd_per_run", codes)
        json.dumps(batch, allow_nan=False)

    def test_oversized_usage_integers_fail_visible_without_crashing(self) -> None:
        oversized = 10**400
        self.paths.usage_json.write_text(
            json.dumps(
                {
                    "estimated_usd": oversized,
                    "caps": {"MAX_LLM_USD_PER_RUN": oversized},
                }
            ),
            encoding="utf-8",
        )
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["health"], "blocked")
        self.assertEqual(batch["llm_usage"]["estimated_usd"], 0.0)
        self.assertEqual(batch["llm_usage"]["usd_cap"], 0.0)
        codes = {issue["code"] for issue in batch["issues"]}
        self.assertIn("invalid_llm_usage_estimated_usd", codes)
        self.assertIn("invalid_llm_usage_caps_max_llm_usd_per_run", codes)
        json.dumps(batch, allow_nan=False)

    def test_expired_approval_is_attention_not_actionable(self) -> None:
        write_enriched(self.paths.enriched, 1)
        self.paths.state_json.write_text(
            json.dumps(
                {
                    "batch": BATCH,
                    "pending_approval": {
                        "stage": "score",
                        "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                    },
                }
            ),
            encoding="utf-8",
        )
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertIn("expired_approval", {issue["code"] for issue in batch["issues"]})
        self.assertEqual(batch["next_action"], f"/score-dry {BATCH}")

    def test_malformed_enriched_row_fails_visible(self) -> None:
        self.paths.enriched.write_text('{}\n{"broken"\n', encoding="utf-8")
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["health"], "blocked")
        self.assertIn("malformed_enriched_rows", {issue["code"] for issue in batch["issues"]})
        attention = self.repo.batches(health="needs_attention")
        self.assertEqual([item["batch"] for item in attention["items"]], [BATCH])

    def test_equal_row_counts_with_identity_mismatch_are_not_scored_complete(self) -> None:
        write_enriched(self.paths.enriched, 2)
        rows = scored_rows(2)
        rows[1]["Place Key"] = "place-extra"
        rows[1]["Phone"] = ""
        rows[1]["Website URL"] = ""
        rows[1]["Business Name"] = "Different"
        rows[1]["City"] = "Elsewhere"
        write_csv(self.paths.scored_csv, rows)
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["progress"], "partial_scoring")
        self.assertEqual(batch["counts"]["scored"], 2)
        self.assertIsNone(batch["counts"]["unscored"])
        self.assertEqual(batch["artifacts"]["scored"]["rows"], 2)
        codes = {issue["code"] for issue in batch["issues"]}
        self.assertIn("identity_reconciliation_unknown", codes)

    def test_duplicate_and_missing_scored_identities_block_completion(self) -> None:
        write_enriched(self.paths.enriched, 2)
        rows = scored_rows(3)
        rows[1]["Place Key"] = "place-0"
        rows[2]["Place Key"] = ""
        rows[2]["Business Name"] = ""
        rows[2]["City"] = ""
        write_csv(self.paths.scored_csv, rows)
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        codes = {issue["code"] for issue in batch["issues"]}
        self.assertIn("identity_reconciliation_unknown", codes)
        self.assertNotEqual(batch["progress"], "scored")

    def test_malformed_run_state_is_blocked_but_other_batches_still_scan(self) -> None:
        write_enriched(self.paths.enriched, 1)
        self.paths.state_json.write_text('{"batch":', encoding="utf-8")
        batch = self.repo.batch(BATCH, force=True)
        assert batch is not None
        self.assertEqual(batch["health"], "blocked")
        self.assertIn("invalid_run_state", {issue["code"] for issue in batch["issues"]})
        self.assertEqual(self.repo.overview(force=True)["totals"]["batches"], 1)

    def test_lead_api_joins_unscored_and_excludes_sensitive_fields(self) -> None:
        write_enriched(self.paths.enriched, 2)
        write_csv(self.paths.scored_csv, scored_rows(1))
        result = self.repo.leads(BATCH, force=True)
        assert result is not None
        self.assertEqual(result["total"], 2)
        self.assertEqual({item["scoring_state"] for item in result["items"]}, {"scored", "unscored"})
        serialized = json.dumps(result)
        self.assertNotIn("555-secret", serialized)
        self.assertNotIn("secret@example.com", serialized)
        self.assertNotIn("Sensitive opener", serialized)

    def test_noncanonical_debug_artifacts_are_ignored(self) -> None:
        (self.root / "output/enriched_debug_10.jsonl").write_text("{}\n", encoding="utf-8")
        overview = self.repo.overview(force=True)
        self.assertEqual(overview["totals"]["batches"], 1)
        self.assertGreaterEqual(overview["source_health"]["ignored_noncanonical_artifacts"], 1)


class DashboardApiTest(unittest.TestCase):
    def test_api_is_read_only_and_rejects_unknown_batch(self) -> None:
        try:
            import httpx
            from lead_machine.dashboard.api import create_app
        except ImportError:
            self.skipTest("Dashboard dependencies are not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_root(root)
            app = create_app(root, frontend_dist=root / "missing-dist")

            async def assert_responses() -> None:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    self.assertEqual((await client.get("/api/health")).status_code, 200)
                    self.assertEqual((await client.get("/api/batches/not-a-batch")).status_code, 404)
                    self.assertEqual((await client.post("/api/batches")).status_code, 405)

            asyncio.run(assert_responses())


if __name__ == "__main__":
    unittest.main()
