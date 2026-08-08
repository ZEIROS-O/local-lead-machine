"""Place Details are fetched concurrently, and the details budget is a hard cap.

The cap is the load-bearing part: every Place Details call is billed, so
`--max-details` is a money ceiling, not a hint. The pre-concurrency code read
`details_count` and then incremented it as two separate statements, which is
only safe because a single thread ran both. These tests pin the cap as a
property of the budget object itself, so a future change that moves the reserve
onto a worker thread cannot silently over-fetch.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import make_search_batch
import places_search


def _config(**overrides):
    data = {
        "batch": "test_hardscaping_001",
        "vertical": "hardscaping",
        "location": "Cleveland OH",
        "limit_per_query": 50,
        "queries": ["hardscaping Cleveland OH"],
        "geo_slices": ["Cleveland OH"],
        "min_rating": 4.0,
        "min_reviews": 10,
        "allow_missing_website": True,
        "exclude_terms": [],
        "boost_terms": ["patio", "pavers"],
    }
    data.update(overrides)
    return make_search_batch.SearchConfig.from_dict(
        data, source_path=Path("search_configs/test.json")
    )


def _details(place_id: str, index: int) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": f"Patio Pros {index:03d}"},
        "formattedAddress": f"{index} Main St, Cleveland, OH 44101, USA",
        "nationalPhoneNumber": f"(216) 555-{index:04d}",
        "websiteUri": f"https://patiopros{index}.example",
        "googleMapsUri": f"https://maps.google.com/?cid={index}",
        "rating": 4.8,
        "userRatingCount": 28,
        "primaryType": "patio contractor",
    }


def _collect(config, *, details_fn, search_fn, sleep_seconds=0.0, **kwargs):
    warnings: list[str] = []
    errors: list[str] = []
    with patch("make_search_batch.text_search_paged", side_effect=search_fn), patch(
        "make_search_batch.place_details", side_effect=details_fn
    ), redirect_stdout(io.StringIO()):
        raw_rows, queries_run, duplicate_counts, efficiency = make_search_batch.collect_raw_rows(
            config,
            api_key="test-key",
            sleep_seconds=sleep_seconds,
            warnings=warnings,
            errors=errors,
            **kwargs,
        )
    return raw_rows, warnings, errors


def _search_returning(count: int):
    def fake_search(api_key, search_text, *, page_size, max_pages=1, max_results=None, sleep_seconds=0.0):
        return [{"id": f"place-{i:03d}"} for i in range(count)], 1

    return fake_search


class DetailsBudgetTest(unittest.TestCase):
    """The hard money cap, pinned independently of who calls it."""

    def test_reserve_and_increment_happen_under_one_lock(self) -> None:
        # The decisive half, and deliberately white-box.
        #
        # The bug being pinned is a check-then-act: read the count, then
        # increment it, so every thread past the check gets a slot before any
        # of them increments. A pure stress test cannot prove that is gone --
        # under CPython the window is a couple of bytecodes wide and the GIL
        # hides it (measured: 32 threads x 25 rounds, even at a 1us switch
        # interval, never over-fetched against a deliberately unlocked
        # reserve). It only over-fetches once a yield point lands in the
        # window, which is precisely what a future refactor would add.
        #
        # So assert the structure instead of sampling the symptom: hold the
        # budget's lock and show that reserve() cannot make progress. An
        # unlocked check-then-act returns immediately and fails here.
        budget = make_search_batch.DetailsBudget(10)
        entered = threading.Event()
        returned = threading.Event()

        def worker() -> None:
            entered.set()
            budget.reserve()
            returned.set()

        with budget._lock:
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(entered.wait(2.0))
            self.assertFalse(
                returned.wait(0.25),
                "reserve() completed while the budget lock was held -- "
                "the check and the increment are not atomic",
            )

        self.assertTrue(returned.wait(2.0))
        thread.join(2.0)
        self.assertEqual(budget.count, 1)

    def test_concurrent_workers_never_collectively_exceed_the_cap(self) -> None:
        # The aggregate invariant, stressed. Weaker than the test above (see
        # its note on why a stress test alone cannot prove atomicity here), but
        # it is the property that actually costs money, stated in the terms the
        # caller cares about: N workers, one cap, exactly cap billed calls.
        previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for attempt in range(25):
                cap = 10
                workers = 32
                budget = make_search_batch.DetailsBudget(cap)
                barrier = threading.Barrier(workers)
                granted: list[bool] = []
                granted_lock = threading.Lock()

                def worker() -> None:
                    barrier.wait()
                    ok = budget.reserve()
                    with granted_lock:
                        granted.append(ok)

                threads = [threading.Thread(target=worker) for _ in range(workers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(
                    sum(1 for ok in granted if ok),
                    cap,
                    f"attempt {attempt}: granted more (or fewer) slots than the cap",
                )
                self.assertEqual(budget.count, cap)
                self.assertEqual(len(granted), workers)
        finally:
            sys.setswitchinterval(previous_interval)

    def test_reserve_without_a_cap_always_grants(self) -> None:
        budget = make_search_batch.DetailsBudget(None)
        self.assertTrue(all(budget.reserve() for _ in range(50)))
        self.assertEqual(budget.count, 50)
        self.assertFalse(budget.exhausted)

    def test_exhausted_reports_the_cap_being_hit(self) -> None:
        budget = make_search_batch.DetailsBudget(2)
        self.assertFalse(budget.exhausted)
        self.assertTrue(budget.reserve())
        self.assertTrue(budget.reserve())
        self.assertTrue(budget.exhausted)
        self.assertFalse(budget.reserve())
        self.assertEqual(budget.count, 2)


class DetailsBudgetDefaultTest(unittest.TestCase):
    """A run that names no cap still gets one.

    `DetailsBudget` was always a correct ceiling; the hole was that nobody
    handed it a number. `run_agent_pipeline.py` never passes `--max-details`,
    so `max_details=None` flowed all the way down and the budget was
    unlimited -- and since the per-call pacing sleep is gone and details are
    fetched six-wide, unbounded billed spend now runs about six times faster
    than the code these defaults were written against. The absence of a flag
    must not mean the absence of a brake.
    """

    def test_no_explicit_cap_resolves_to_a_bounded_default(self) -> None:
        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": ""}):
            resolved = make_search_batch.resolve_max_details(None)
        self.assertIsNotNone(resolved, "an unspecified cap must not mean unlimited")
        self.assertEqual(resolved, make_search_batch.DEFAULT_MAX_DETAILS_CALLS_PER_RUN)

    def test_env_var_overrides_the_default(self) -> None:
        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": "25"}):
            self.assertEqual(make_search_batch.resolve_max_details(None), 25)

    def test_explicit_argument_wins_over_the_env(self) -> None:
        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": "25"}):
            self.assertEqual(make_search_batch.resolve_max_details(5), 5)
            # Including an explicit 0: "spend nothing" is a real instruction,
            # not a missing value.
            self.assertEqual(make_search_batch.resolve_max_details(0), 0)

    def test_a_negative_env_value_is_the_documented_way_back_to_unlimited(self) -> None:
        # The escape hatch has to be the env var and not `--max-details`,
        # because `_validate_optional_cap` rejects a negative flag and 0
        # already means "no details calls at all".
        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": "-1"}):
            self.assertIsNone(make_search_batch.resolve_max_details(None))

    def test_an_unparseable_env_value_falls_back_to_the_bounded_default(self) -> None:
        warned = io.StringIO()
        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": "lots"}):
            with redirect_stderr(warned):
                resolved = make_search_batch.resolve_max_details(None)
        self.assertEqual(resolved, make_search_batch.DEFAULT_MAX_DETAILS_CALLS_PER_RUN)
        self.assertIn("MAX_DETAILS_CALLS_PER_RUN", warned.getvalue())

    def test_collect_raw_rows_enforces_the_env_default_with_no_flag(self) -> None:
        # The regression that matters: the resolved default has to reach the
        # budget object, not merely exist as a constant.
        def fake_details(api_key, place_id):
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": "4"}):
            raw_rows, warnings, errors = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(20)
            )

        self.assertEqual(len(raw_rows), 4, "billed past the env cap with no --max-details")
        self.assertEqual(errors, [])
        self.assertTrue(any("max-details=4" in w for w in warnings), warnings)

    def test_collect_raw_rows_is_bounded_even_with_no_env_at_all(self) -> None:
        def fake_details(api_key, place_id):
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": ""}), patch.object(
            make_search_batch, "DEFAULT_MAX_DETAILS_CALLS_PER_RUN", 3
        ):
            raw_rows, warnings, _ = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(20)
            )

        self.assertEqual(len(raw_rows), 3)
        self.assertTrue(any("max-details=3" in w for w in warnings), warnings)

    def test_the_shipped_default_clears_a_normal_metro_batch(self) -> None:
        # Observed unique places per medium metro config across the 32 batches
        # in data/raw peak at 251; the default has to sit clear of that or it
        # would silently truncate ordinary runs.
        self.assertGreaterEqual(make_search_batch.DEFAULT_MAX_DETAILS_CALLS_PER_RUN, 300)
        # And it has to stay a real brake, not a formality.
        self.assertLessEqual(make_search_batch.DEFAULT_MAX_DETAILS_CALLS_PER_RUN, 1000)

    def test_the_default_matches_the_legacy_cli_cap_of_the_same_name(self) -> None:
        # `MAX_DETAILS_CALLS_PER_RUN` already bounds the legacy places_search
        # CLI. One env var must not mean two different numbers depending on
        # which entry point read it.
        with patch.dict(os.environ, {"MAX_DETAILS_CALLS_PER_RUN": ""}):
            legacy_default = places_search.getenv_int("MAX_DETAILS_CALLS_PER_RUN", 400)
        self.assertEqual(make_search_batch.DEFAULT_MAX_DETAILS_CALLS_PER_RUN, legacy_default)


class DetailsWorkerResolutionTest(unittest.TestCase):
    def test_default_worker_count_is_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLACES_DETAILS_WORKERS", None)
            workers = make_search_batch.resolve_details_workers()
        self.assertEqual(workers, make_search_batch.DEFAULT_DETAILS_WORKERS)
        self.assertGreaterEqual(workers, 4)
        self.assertLessEqual(workers, make_search_batch.MAX_DETAILS_WORKERS)

    def test_env_override_is_clamped_to_the_ceiling(self) -> None:
        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "64"}):
            self.assertEqual(
                make_search_batch.resolve_details_workers(),
                make_search_batch.MAX_DETAILS_WORKERS,
            )

    def test_env_override_floors_at_one(self) -> None:
        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "0"}):
            self.assertEqual(make_search_batch.resolve_details_workers(), 1)

    def test_env_override_is_honoured_between_the_bounds(self) -> None:
        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "3"}):
            self.assertEqual(make_search_batch.resolve_details_workers(), 3)


class ConcurrentDetailsFetchTest(unittest.TestCase):
    def test_details_calls_overlap(self) -> None:
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def fake_details(api_key, place_id):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "4"}):
            raw_rows, _, errors = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(16)
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(raw_rows), 16)
        self.assertGreater(peak, 1, "Place Details calls did not overlap at all")
        self.assertLessEqual(peak, 4, "concurrency exceeded PLACES_DETAILS_WORKERS")

    def test_cap_holds_when_every_worker_is_in_flight(self) -> None:
        calls: list[str] = []
        lock = threading.Lock()

        def fake_details(api_key, place_id):
            with lock:
                calls.append(place_id)
            # Hold the worker so the submit loop keeps running while the pool
            # is saturated -- the window in which a check-then-act reserve
            # over-fetches.
            time.sleep(0.02)
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "8"}):
            raw_rows, warnings, errors = _collect(
                _config(),
                details_fn=fake_details,
                search_fn=_search_returning(60),
                max_details=10,
            )

        self.assertEqual(len(calls), 10, "billed more Place Details calls than the cap")
        self.assertEqual(len(raw_rows), 10)
        self.assertEqual(errors, [])
        self.assertTrue(any("max-details=10" in w for w in warnings), warnings)

    def test_raw_row_order_follows_submit_order_not_completion_order(self) -> None:
        # Invert the latency so completion order is the reverse of submit
        # order. Row order is observable: raw rows go straight to
        # data/raw/<batch>.csv, and dedupe_search_rows is first-wins, so
        # completion-order output would change which duplicate survives.
        total = 12

        def fake_details(api_key, place_id):
            index = int(place_id.rsplit("-", 1)[1])
            time.sleep(0.005 * (total - index))
            return _details(place_id, index)

        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "6"}):
            raw_rows, _, errors = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(total)
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [row["place_id"] for row in raw_rows],
            [f"place-{i:03d}" for i in range(total)],
        )

    def test_one_failed_details_call_does_not_drop_the_others(self) -> None:
        def fake_details(api_key, place_id):
            index = int(place_id.rsplit("-", 1)[1])
            if index == 3:
                raise RuntimeError("boom")
            return _details(place_id, index)

        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "5"}):
            raw_rows, _, errors = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(8)
            )

        self.assertEqual(len(raw_rows), 7)
        self.assertNotIn("place-003", [row["place_id"] for row in raw_rows])
        self.assertEqual(len(errors), 1)
        self.assertIn("place-003", errors[0])

    def test_a_missing_business_name_warns_and_is_dropped(self) -> None:
        def fake_details(api_key, place_id):
            index = int(place_id.rsplit("-", 1)[1])
            payload = _details(place_id, index)
            if index == 2:
                payload["displayName"] = {"text": ""}
            return payload

        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "4"}):
            raw_rows, warnings, errors = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(6)
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(raw_rows), 5)
        self.assertTrue(any("place-002" in w for w in warnings), warnings)

    def test_duplicate_place_ids_are_still_counted_once(self) -> None:
        def fake_search(api_key, search_text, *, page_size, max_pages=1, max_results=None, sleep_seconds=0.0):
            return [{"id": "place-000"}, {"id": "place-001"}, {"id": "place-000"}], 1

        def fake_details(api_key, place_id):
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        with patch("make_search_batch.text_search_paged", side_effect=fake_search), patch(
            "make_search_batch.place_details", side_effect=fake_details
        ), redirect_stdout(io.StringIO()):
            raw_rows, _, duplicate_counts, _ = make_search_batch.collect_raw_rows(
                _config(),
                api_key="test-key",
                sleep_seconds=0.0,
                warnings=[],
                errors=[],
            )

        self.assertEqual(len(raw_rows), 2)
        self.assertEqual(duplicate_counts["duplicate_place_id_early"], 1)

    def test_no_per_call_sleep_between_details_fetches(self) -> None:
        # The 0.15s pacing sleep existed to keep serial calls under the Places
        # QPS ceiling. A bounded pool does that job; paying it per call would
        # just re-serialise the pool.
        def fake_details(api_key, place_id):
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        sleeps: list[float] = []

        with patch("make_search_batch.time.sleep", side_effect=sleeps.append), patch.dict(
            os.environ, {"PLACES_DETAILS_WORKERS": "4"}
        ):
            raw_rows, _, errors = _collect(
                _config(),
                details_fn=fake_details,
                search_fn=_search_returning(10),
                sleep_seconds=0.15,
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(raw_rows), 10)
        # At most the one end-of-search pause, never one per place.
        self.assertLessEqual(len(sleeps), 1, f"per-call sleep survived: {sleeps}")

    def test_single_worker_still_works(self) -> None:
        def fake_details(api_key, place_id):
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        with patch.dict(os.environ, {"PLACES_DETAILS_WORKERS": "1"}):
            raw_rows, _, errors = _collect(
                _config(), details_fn=fake_details, search_fn=_search_returning(5)
            )

        self.assertEqual(errors, [])
        self.assertEqual([row["place_id"] for row in raw_rows], [f"place-{i:03d}" for i in range(5)])


class SuppressionStillGatesEveryLeadTest(unittest.TestCase):
    """Concurrency must not open a path around opt-out suppression.

    `collect_raw_rows` never checked suppression -- the gate lives downstream in
    `enrich_row`, which `run_search_batch` calls with the configured index. This
    pins that every place the concurrent fetch produces still reaches it.
    """

    def test_every_concurrently_fetched_place_reaches_enrichment_with_the_index(self) -> None:
        import json
        import tempfile

        seen_indexes: list[object] = []
        lock = threading.Lock()

        def fake_details(api_key, place_id):
            return _details(place_id, int(place_id.rsplit("-", 1)[1]))

        def fake_enrich(row, screenshots_dir, take_shots, rendered_site_check=False,
                        suppression_index=None, review_snapshot_store=None, per_lead_timeout=None):
            with lock:
                seen_indexes.append(suppression_index)
            return {"google": row, "website": {"site_status": "No Website"}, "derived": {}, "screenshots": {}}

        suppression = frozenset({"suppressed.example"})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(_config().to_dict()), encoding="utf-8")

            with patch("make_search_batch.text_search_paged", side_effect=_search_returning(9)), patch(
                "make_search_batch.place_details", side_effect=fake_details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), patch.dict(
                os.environ, {"PLACES_DETAILS_WORKERS": "6"}
            ), redirect_stdout(io.StringIO()):
                result = make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    no_screenshots=True,
                    sleep_seconds=0,
                    suppression_index=suppression,
                    review_snapshot_db=root / "review.sqlite3",
                )

        self.assertEqual(result.raw_count, 9)
        self.assertEqual(len(seen_indexes), 9)
        self.assertTrue(all(index is suppression for index in seen_indexes))


if __name__ == "__main__":
    unittest.main()


class PlaceDetailsTransientRetryTest(unittest.TestCase):
    """A 429 must not permanently drop a lead that a retry would have kept.

    Text Search has retried transient statuses since `_text_search_page` was
    written; Place Details never did. That asymmetry was survivable while the
    details loop was serial and paid a 0.15s sleep after every call -- the sleep
    was pacing the burst even though nothing said so. Concurrency removed the
    sleep and put six calls in flight, which is a burst well above the old
    ~6.7/s, so the untried 429 became a lead lost at spend: the budget slot is
    reserved before the call, so the money is gone either way.
    """

    def test_place_details_retries_a_transient_status(self) -> None:
        attempts = []

        def flaky(method, url, *, headers=None, json_body=None):
            attempts.append(url)
            if len(attempts) == 1:
                raise places_search.GoogleApiError("rate limited", status_code=429)
            return {"id": "place-1", "displayName": {"text": "Patio Pros"}}

        with patch.object(places_search, "request_json", flaky), \
                patch.object(places_search.time, "sleep", lambda _s: None):
            result = places_search.place_details("key", "place-1")

        self.assertEqual(len(attempts), 2, "a 429 should be retried, not dropped")
        self.assertEqual(result["id"], "place-1")

    def test_place_details_does_not_retry_a_permanent_status(self) -> None:
        attempts = []

        def rejected(method, url, *, headers=None, json_body=None):
            attempts.append(url)
            raise places_search.GoogleApiError("bad request", status_code=400)

        with patch.object(places_search, "request_json", rejected), \
                patch.object(places_search.time, "sleep", lambda _s: None):
            with self.assertRaises(places_search.GoogleApiError):
                places_search.place_details("key", "place-1")

        self.assertEqual(
            len(attempts), 1, "a 400 is deterministic; retrying it just costs latency"
        )
