"""Phase 5: concurrent API-LLM scoring.

The serial `while index < len(selected)` loop in `score_records_api_run` is the
single largest wall-clock cost in a run (27 batches x ~20-30s for a 133-lead
batch). These tests pin the concurrency contract:

- `concurrency` defaults to 1, so every existing caller keeps serial semantics.
- With `concurrency > 1`, batches are genuinely in flight together.
- Results are merged in *submit* order, never completion order, because
  `batch_id` names the on-disk bundle and `import_score_bundle` is order-
  sensitive in the same way `dedupe_search_rows` is.
"""

from __future__ import annotations

import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


def _google_fixture(name: str, place_id: str) -> dict:
    return {
        "business_name": name,
        "city": "Kirtland",
        "phone": "(440) 637-5848",
        "website_url": "https://example.com",
        "google_maps_url": "https://maps.google.com/?cid=123",
        "rating": "4.9",
        "reviews": "130",
        "place_id": place_id,
    }


def _evidence_fixture() -> dict:
    return {
        "site_status": "weak",
        "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
        "visible_pain_score": 35,
        "visible_pain_reasons": ["No clear estimate CTA"],
        "send_trigger": "No clear estimate CTA",
        "evidence_items": [
            {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
            {"id": "E2", "field": "site_issue_flags", "value": "no_clear_cta_detected"},
            {"id": "E3", "field": "send_trigger", "value": "No clear estimate CTA"},
        ],
    }


def _manual_score() -> dict:
    return {
        "score_schema_version": 2,
        "total_score": 75,
        "grade": "B",
        "subscores": {
            "icp_fit": 14,
            "reputation_demand_proof": 12,
            "web_conversion_pain": 18,
            "mockup_leverage": 10,
            "reachability": 7,
            "offer_angle_clarity": 9,
            "evidence_confidence": 5,
        },
        "confidence": "medium",
        "hard_disqualifiers": [],
        "best_angle": "Estimate Funnel",
        "commercial_fit_state": "commercially-sendable",
        "commercial_materiality": "Premium estimate requests can fail to reach a clear conversion path.",
        "recurring_consequence": "The same path affects each new high-intent visitor.",
        "root_cause_confidence": "Fresh rendered evidence shows the CTA-path weakness.",
        "offer_component": "Get the Lead",
        "packageable_solution": "Rebuild the estimate path and trust placement.",
        "value_mechanism": "Reduce friction between existing demand and estimate requests.",
        "beyond_free_fix": "The CTA, mobile path, trust proof, and routing require one coherent system.",
        "self_fix_risk": "The existing multi-step path has persisted and needs coordinated implementation.",
        "intentionality": "The verified customer path contradicts an intentional low-conversion design.",
        "contact_eligibility": "contact-now",
        "asset_state": "none",
        "promise_state": "asset-free",
        "asset_id": "",
        "asset_verifier_verdict": "",
        "recommended_action": "Build mockup then SMS owner",
        "why_this_lead": ["Strong reviews", "Visible quote-path issue"],
        "why_this_might_be_wrong": [],
        "opener": "Your reviews are strong, but the quote path looks hard to find.",
        "evidence_refs": ["E1", "E2"],
        "safe_outreach_claims": [
            {"claim": "Your quote path looks hard to find.", "evidence_refs": ["E2"]},
            {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
        ],
        "unsafe_claims": ["Do not claim they are losing jobs."],
        "airtable_fields": {},
    }


_BATCH_ID_RE = re.compile(r"batch_(\d+)")


class ConcurrentApiLlmScoringTest(unittest.TestCase):
    def _records(self, count: int) -> list[dict]:
        return [
            {
                "google": _google_fixture(f"Landscaping {index}", f"ChIJ{index}"),
                "evidence": _evidence_fixture(),
            }
            for index in range(count)
        ]

    def test_batches_are_in_flight_together_when_concurrency_exceeds_one(self) -> None:
        """Two batches must overlap in time, not merely both complete.

        A serial loop makes the first call block on the barrier until it times
        out, raising BrokenBarrierError. Only genuine concurrency lets both
        calls reach the barrier.
        """
        from score_leads import score_records_api_run

        barrier = threading.Barrier(2, timeout=10)
        overlapped = threading.Event()

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
            barrier.wait()
            overlapped.set()
            return {
                "bundle": {
                    "batch_id": f"batch_{batch_number:03d}",
                    "scores": [
                        {"score_file": f"chij{batch_number - 1}.json", "score": _manual_score()}
                    ],
                },
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch):
                run = score_records_api_run(
                    self._records(2),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                    concurrency=2,
                )

        self.assertTrue(overlapped.is_set(), "batches never overlapped — the loop is still serial")
        self.assertEqual(run.calls, 2)

    def test_concurrency_defaults_to_one_so_existing_callers_stay_serial(self) -> None:
        """Callers that never pass `concurrency` must keep exact serial semantics."""
        from score_leads import score_records_api_run

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
            try:
                return {
                    "bundle": {
                        "batch_id": f"batch_{batch_number:03d}",
                        "scores": [
                            {"score_file": f"chij{batch_number - 1}.json", "score": _manual_score()}
                        ],
                    },
                    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                }
            finally:
                with lock:
                    in_flight -= 1

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch):
                run = score_records_api_run(
                    self._records(3),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(max_in_flight, 1, "default concurrency must stay serial")
        self.assertEqual(run.calls, 3)


if __name__ == "__main__":
    unittest.main()


class DotenvThreadSafetyTest(unittest.TestCase):
    """`load_dotenv` mutates os.environ; N threads calling it per-request is a race."""

    def test_concurrent_score_batch_loads_dotenv_once(self) -> None:
        from lead_machine import llm_client

        llm_client._reset_dotenv_loaded_for_tests()
        load_calls = 0
        counter_lock = threading.Lock()

        def counting_load_dotenv(*args, **kwargs) -> bool:
            nonlocal load_calls
            with counter_lock:
                load_calls += 1
            return True

        def fake_provider(prompt, *, model, max_tokens):
            return {"bundle": {"batch_id": "batch_001", "scores": []}, "usage": {}}

        with patch.object(llm_client, "load_dotenv", side_effect=counting_load_dotenv):
            with patch.dict(llm_client._PROVIDERS, {"kimi": fake_provider}, clear=False):
                with patch.object(llm_client, "_provider_chain", return_value=["kimi"]):
                    threads = [
                        threading.Thread(
                            target=llm_client.score_batch,
                            args=("prompt",),
                            kwargs={"max_tokens": 20},
                        )
                        for _ in range(8)
                    ]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join()

        self.assertEqual(load_calls, 1, "load_dotenv must be guarded, not run per request")


class ConcurrencyResolutionTest(unittest.TestCase):
    """`LLM_SCORING_CONCURRENCY` follows the getenv_int convention in AGENTS.md."""

    def test_defaults_to_four_when_unset(self) -> None:
        from score_leads import resolve_llm_scoring_concurrency

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_llm_scoring_concurrency(), 4)

    def test_reads_the_env_override(self) -> None:
        from score_leads import resolve_llm_scoring_concurrency

        with patch.dict("os.environ", {"LLM_SCORING_CONCURRENCY": "8"}, clear=True):
            self.assertEqual(resolve_llm_scoring_concurrency(), 8)

    def test_clamps_to_at_least_one(self) -> None:
        from score_leads import resolve_llm_scoring_concurrency

        with patch.dict("os.environ", {"LLM_SCORING_CONCURRENCY": "0"}, clear=True):
            self.assertEqual(resolve_llm_scoring_concurrency(), 1)
        with patch.dict("os.environ", {"LLM_SCORING_CONCURRENCY": "-3"}, clear=True):
            self.assertEqual(resolve_llm_scoring_concurrency(), 1)


class SerialConcurrentEquivalenceTest(unittest.TestCase):
    """Concurrency is a speed change, not a scoring change."""

    def _records(self, count: int) -> list[dict]:
        return [
            {
                "google": _google_fixture(f"Landscaping {index}", f"ChIJ{index}"),
                "evidence": _evidence_fixture(),
            }
            for index in range(count)
        ]

    @staticmethod
    def _fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
        batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
        return {
            "bundle": {
                "batch_id": f"batch_{batch_number:03d}",
                "scores": [
                    {"score_file": f"chij{batch_number - 1}.json", "score": _manual_score()}
                ],
            },
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }

    def _run(self, *, concurrency: int) -> tuple[list[str], int, int, float]:
        from score_leads import score_records_api_run

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=self._fake_score_batch):
                run = score_records_api_run(
                    self._records(6),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                    concurrency=concurrency,
                )
        return (
            [row["Business Name"] for row in run.manual_run.rows],
            run.calls,
            run.tokens,
            run.usd,
        )

    def test_concurrent_run_matches_serial_run_row_for_row(self) -> None:
        serial = self._run(concurrency=1)
        concurrent = self._run(concurrency=4)
        self.assertEqual(serial, concurrent)
