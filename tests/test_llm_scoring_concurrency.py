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


def _valid_batch_result(batch_number: int, *, usage: dict | None = None, model: str = "") -> dict:
    result = {
        "bundle": {
            "batch_id": f"batch_{batch_number:03d}",
            "scores": [{"score_file": f"chij{batch_number - 1}.json", "score": _manual_score()}],
        },
        "usage": usage or {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    }
    if model:
        result["model"] = model
    return result


def _two_call_provider(usage: dict | None = None):
    """A provider where every batch bills exactly two calls.

    The first attempt comes back truncated, which `_score_batch_with_debug`
    retries with a larger `max_tokens` — one of the three call sites a single
    batch can spend (initial, truncation retry, validation repair). Returns the
    fake and the per-batch attempt counter so a test can assert on real calls
    made, not just on the number the run banked.
    """
    attempts: dict[int, int] = {}
    lock = threading.Lock()

    def fake_score_batch(prompt: str, *, model: str = "", max_tokens: int = 0) -> dict:
        from lead_machine import llm_client

        batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
        with lock:
            attempts[batch_number] = attempts.get(batch_number, 0) + 1
            first_attempt = attempts[batch_number] == 1
        if first_attempt:
            raise llm_client.InvalidLlmResponse(
                "Invalid LLM score bundle JSON: Unterminated string starting at line 4 column 12",
                raw_text='{"batch_id": "batch_%03d", "scores": [{"score_file": "chij' % batch_number,
            )
        return _valid_batch_result(batch_number, usage=usage)

    return fake_score_batch, attempts


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

    def _capped_run(self, *, concurrency: int) -> tuple[list[str], int, int, float, bool, str]:
        """Same run as `_run`, but with MAX_LLM_CALLS_PER_RUN actually binding."""
        from score_leads import score_records_api_run

        provider, _ = _two_call_provider()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=provider):
                run = score_records_api_run(
                    self._records(6),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=4,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                    concurrency=concurrency,
                )
        return (
            [row["Business Name"] for row in run.manual_run.rows],
            run.calls,
            run.tokens,
            run.usd,
            run.stopped_by_cap,
            run.stop_reason,
        )

    def test_concurrent_run_matches_serial_run_with_a_binding_calls_cap(self) -> None:
        """The uncapped parity test says nothing about cap behaviour — pin that too.

        Exact parity holds here because the concurrent gate reduces to the serial
        `_hit_llm_cap(banked usage)` check whenever nothing is in flight, which is
        where this scenario's cap trips. It is *not* a general guarantee: see
        `ConcurrentCapEnforcementTest.test_actual_usage_over_reservation_still_stops_the_run`,
        where the concurrent run gets one batch further than the serial one because
        batches already in flight are paid for. The general invariant those two
        share is the prefix property asserted below.
        """
        serial = self._capped_run(concurrency=1)
        concurrent = self._capped_run(concurrency=4)
        self.assertEqual(serial, concurrent)
        self.assertTrue(serial[4], "the cap must actually bind or this test proves nothing")
        self.assertEqual(serial[5], "MAX_LLM_CALLS_PER_RUN")


class ConcurrentCapEnforcementTest(unittest.TestCase):
    """Spend caps must still be able to stop a *concurrent* run.

    The serial loop re-checks `_hit_llm_cap(calls, tokens, usd)` against actual
    banked usage before every batch. The first concurrent implementation replaced
    that with an up-front reservation and then submitted every planned batch
    before banking any of them, so after planning there was no point at which a
    cap could still stop the run:

    - `MAX_LLM_CALLS_PER_RUN` was compared against the *planned-batch index*, but
      one batch can bill up to three calls (initial, truncation retry, validation
      repair), so a cap of 60 admitted ~180 billed calls.
    - The per-batch reservation covers exactly one call, so a batch that retries
      lands at 2-3x its reservation with nothing left to notice.
    - Reservations were priced with the *caller's* model. A `subscription:*` entry
      at the head of the chain is priced (0.0, 0.0), which zeroes every
      reservation and makes `MAX_LLM_USD_PER_RUN` not bind at all — even though a
      fall-through to Kimi or the Claude API spends real money.

    Every one of these was invisible to the older tests, which pass
    `max_calls=0, max_tokens_per_run=0, max_usd_per_run=0.0` — i.e. caps off.
    """

    def _records(self, count: int) -> list[dict]:
        return [
            {
                "google": _google_fixture(f"Landscaping {index}", f"ChIJ{index}"),
                "evidence": _evidence_fixture(),
            }
            for index in range(count)
        ]

    def test_two_call_batches_do_not_exceed_max_calls_per_run(self) -> None:
        """(a) The cap counts billed calls, not planned batches."""
        from score_leads import score_records_api_run

        provider, attempts = _two_call_provider()

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=provider):
                run = score_records_api_run(
                    self._records(6),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=4,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                    concurrency=4,
                )

        billed_calls = sum(attempts.values())
        self.assertTrue(
            all(count == 2 for count in attempts.values()),
            f"every batch was supposed to bill two calls, got {attempts}",
        )
        self.assertLessEqual(
            billed_calls, 4, f"MAX_LLM_CALLS_PER_RUN=4 admitted {billed_calls} billed calls"
        )
        self.assertEqual(run.calls, billed_calls, "the run must bank the calls it actually made")
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual(run.stop_reason, "MAX_LLM_CALLS_PER_RUN")

    def test_subscription_first_chain_still_stops_at_the_usd_cap(self) -> None:
        """(c) Reservations price the priciest model the chain can reach.

        `subscription:codex` is priced (0.0, 0.0). Pricing the reservation with it
        zeroes the whole run's projected spend, so `MAX_LLM_USD_PER_RUN` never
        binds — while the chain quietly falls through to kimi-k3 and bills for it.
        """
        from score_leads import score_records_api_run

        served_usage = {"input_tokens": 20_000, "output_tokens": 20_000, "total_tokens": 40_000}

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
            return _valid_batch_result(batch_number, usage=served_usage, model="kimi-k3")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch):
                run = score_records_api_run(
                    self._records(6),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="subscription:codex",
                    projection_model="kimi-k3",
                    max_tokens=40_000,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=1.0,
                    concurrency=4,
                )

        self.assertTrue(
            run.stopped_by_cap,
            f"MAX_LLM_USD_PER_RUN=1.0 never bound; the run spent ${run.usd:.4f}",
        )
        self.assertEqual(run.stop_reason, "MAX_LLM_USD_PER_RUN")
        self.assertLessEqual(run.usd, 1.0)

    def test_actual_usage_over_reservation_still_stops_the_run(self) -> None:
        """(b) Reservations cover one call; banked actuals are what the cap reads.

        `max_tokens=20` makes each reservation trivially small, and the served
        batches then bill $0.36 apiece. Nothing in the plan could see that — only
        reconciling against banked totals inside the collection loop can.
        """
        from score_leads import score_records_api_run

        served_usage = {"input_tokens": 20_000, "output_tokens": 20_000, "total_tokens": 40_000}

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
            return _valid_batch_result(batch_number, usage=served_usage)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch):
                run = score_records_api_run(
                    self._records(6),
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="kimi-k3",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.5,
                    concurrency=2,
                )

        self.assertTrue(
            run.stopped_by_cap,
            f"MAX_LLM_USD_PER_RUN=0.5 never bound; the run spent ${run.usd:.4f}",
        )
        self.assertEqual(run.stop_reason, "MAX_LLM_USD_PER_RUN")
        self.assertLess(run.calls, 6, "the cap must stop submission, not merely be reported")


class BatchFailureIsolationTest(unittest.TestCase):
    """One unparseable batch must not throw away every other batch in the run.

    Real incident, 2026-08-06: a 133-lead run scored 24 of 27 batches, then
    kimi-k3 returned a `safe_outreach_claims` entry missing its `"claim":` key.
    That is a JSON *syntax* error, so it raises before schema validation and the
    repair path never engages; `_looks_truncated_llm_response` is also false for
    a complete-but-malformed body, so there is no retry either. The resulting
    ValueError propagated out of the collection loop and aborted the run — 120
    banked score JSONs on disk, 33 paid calls, and no scored CSV written at all.

    A failed batch is the same class of event as a cap stop: bound the loss to
    that batch, record it, and let the run finish with what it has.
    """

    def _records(self, count: int) -> list[dict]:
        return [
            {
                "google": _google_fixture(f"Landscaping {index}", f"ChIJ{index}"),
                "evidence": _evidence_fixture(),
            }
            for index in range(count)
        ]

    @staticmethod
    def _fake_score_batch_failing_batch_two(prompt: str, *, model: str, max_tokens: int) -> dict:
        """Batch 2 of 3 comes back malformed; batches 1 and 3 are fine."""
        from lead_machine import llm_client

        batch_number = int(_BATCH_ID_RE.search(prompt).group(1))
        if batch_number == 2:
            exc = llm_client.InvalidLlmResponse(
                "Invalid LLM score bundle JSON: Expecting ':' delimiter at line 58 column 65",
                raw_text='{"batch_id": "batch_002", "scores": [{"The site already offers forms"}]}',
                usage={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
            )
            exc.model = model
            raise exc
        return {
            "bundle": {
                "batch_id": f"batch_{batch_number:03d}",
                "scores": [
                    {"score_file": f"chij{batch_number - 1}.json", "score": _manual_score()}
                ],
            },
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }

    def _run(self, *, concurrency: int):
        from score_leads import score_records_api_run

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=self._fake_score_batch_failing_batch_two,
            ):
                run = score_records_api_run(
                    self._records(3),
                    scores_dir=scores_dir,
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                    concurrency=concurrency,
                )
            banked = sorted(path.name for path in scores_dir.glob("*.json"))
        return run, banked

    def _assert_survivors_kept(self, run, banked) -> None:
        self.assertEqual(
            banked,
            ["chij0.json", "chij2.json"],
            "the batches that succeeded must still be banked",
        )
        self.assertEqual(
            [row["Business Name"] for row in run.manual_run.rows],
            ["Landscaping 0", "Landscaping 2"],
            "the scored CSV must carry every lead the run did score",
        )
        self.assertEqual(
            [skip.reason for skip in run.skipped_records],
            ["batch_failed"],
            "the failed batch's leads must be reported as skipped, not silently dropped",
        )
        self.assertIn(
            "batch_002",
            " ".join(run.debug_notes),
            "the failure must name the batch so the raw response is findable",
        )
        # Usage from the failed attempt still counts — it was paid for.
        self.assertEqual(run.calls, 3)
        self.assertEqual(run.tokens, 23)

    def test_concurrent_run_survives_one_unparseable_batch(self) -> None:
        run, banked = self._run(concurrency=4)
        self._assert_survivors_kept(run, banked)

    def test_serial_run_survives_one_unparseable_batch(self) -> None:
        run, banked = self._run(concurrency=1)
        self._assert_survivors_kept(run, banked)
