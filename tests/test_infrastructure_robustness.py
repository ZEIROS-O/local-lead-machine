"""Failure handling around the three external services.

Airtable writes raised on the first error and left a half-synced base with no
record of what applied. Places retried errors no retry could fix. And LLM spend
caps were computed from Sonnet 4.6 prices while the configured model had
already moved on, so every cap was measuring a model the run was not using.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import places_search
import upload_airtable
from score_leads import (
    LLM_PRICING_USD_PER_MILLION_TOKENS,
    UnknownLlmModelError,
    estimate_llm_usd,
    llm_pricing,
)


def _response(status_code: int, *, body: str = "{}") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    try:
        response.json.return_value = json.loads(body)
    except json.JSONDecodeError:
        response.json.side_effect = ValueError("not json")
    return response


def _plan(count: int) -> list[dict]:
    return [
        {
            "method": "PATCH",
            "record_id": f"rec{index}",
            "upsert_key": f"phone:216555010{index}",
            "payload": {"fields": {"Status": "New"}},
        }
        for index in range(count)
    ]


class AirtableRetryTest(unittest.TestCase):
    def test_a_rate_limit_is_retried_and_then_succeeds(self) -> None:
        session = MagicMock()
        session.request.side_effect = [_response(429, body='{"error":"rate"}'), _response(200)]

        with patch("upload_airtable.time.sleep"):
            with redirect_stderr(io.StringIO()):
                response = upload_airtable._airtable_write_with_retry(
                    session,
                    method="PATCH",
                    url="https://api.airtable.com/v0/base/table/rec1",
                    headers={},
                    payload={},
                    label="phone:2165550100",
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.request.call_count, 2)

    def test_a_deterministic_error_is_not_retried(self) -> None:
        # A 422 for an unknown field fails identically every time; retrying it
        # only delays the same error.
        session = MagicMock()
        session.request.return_value = _response(422, body='{"error":"UNKNOWN_FIELD_NAME"}')

        with patch("upload_airtable.time.sleep"):
            with self.assertRaises(RuntimeError) as caught:
                upload_airtable._airtable_write_with_retry(
                    session,
                    method="PATCH",
                    url="https://api.airtable.com/v0/base/table/rec1",
                    headers={},
                    payload={},
                    label="phone:2165550100",
                )

        self.assertEqual(session.request.call_count, 1)
        self.assertIn("422", str(caught.exception))


class AirtableReceiptsTest(unittest.TestCase):
    def test_writes_that_applied_before_a_failure_are_recorded(self) -> None:
        """The property that makes a half-synced base recoverable.

        send_airtable_plan used to raise on the first status >= 400 with no
        record of which writes had already landed, so the operator could
        neither trust the base nor safely re-run.
        """
        responses = [_response(200), _response(200), _response(422, body="bad field")]
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp) / "writes.jsonl"
            with patch("upload_airtable.requests.Session") as session_cls, patch(
                "upload_airtable.time.sleep"
            ):
                session_cls.return_value.__enter__.return_value.request.side_effect = responses
                with self.assertRaises(RuntimeError):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        upload_airtable.send_airtable_plan(
                            "base1", "Prospects", "token", _plan(3), receipts_path=receipts
                        )

            written = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(written), 2)
        self.assertEqual(
            [item["upsert_key"] for item in written],
            ["phone:2165550100", "phone:2165550101"],
        )

    def test_a_resumed_run_skips_what_already_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp) / "writes.jsonl"
            receipts.write_text(
                json.dumps(
                    {
                        "upsert_key": "phone:2165550100",
                        "base_id": "base1",
                        "table": "Prospects",
                        "method": "PATCH",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            applied_keys = upload_airtable.load_applied_write_keys(
                receipts, base_id="base1", table="Prospects"
            )

            with patch("upload_airtable.requests.Session") as session_cls, patch(
                "upload_airtable.time.sleep"
            ):
                request = session_cls.return_value.__enter__.return_value.request
                request.return_value = _response(200)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    applied = upload_airtable.send_airtable_plan(
                        "base1",
                        "Prospects",
                        "token",
                        _plan(3),
                        receipts_path=receipts,
                        applied_keys=applied_keys,
                    )

        self.assertEqual(applied_keys, {"phone:2165550100"})
        self.assertEqual(applied, 2)
        self.assertEqual(request.call_count, 2)

    def test_receipts_from_a_different_base_do_not_suppress_writes(self) -> None:
        # Otherwise a receipts file left over from another base would make a run
        # skip writes it never performed.
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp) / "writes.jsonl"
            receipts.write_text(
                json.dumps({"upsert_key": "phone:2165550100", "base_id": "other", "table": "Prospects"})
                + "\n",
                encoding="utf-8",
            )

            applied = upload_airtable.load_applied_write_keys(
                receipts, base_id="base1", table="Prospects"
            )

        self.assertEqual(applied, set())


class PlacesRetryTest(unittest.TestCase):
    def test_a_transient_status_is_retried(self) -> None:
        error = places_search.GoogleApiError("Google API error 503: busy", status_code=503)
        with patch("places_search.request_json", side_effect=[error, {"places": []}]) as request, patch(
            "places_search.time.sleep"
        ):
            result = places_search._text_search_page({}, {})

        self.assertEqual(result, {"places": []})
        self.assertEqual(request.call_count, 2)

    def test_a_deterministic_status_fails_immediately(self) -> None:
        # request_json raises on any status >= 400, and this caught every
        # RuntimeError — so a 400 for a malformed query or a 403 for a disabled
        # key burned three attempts and two backoff sleeps before failing.
        error = places_search.GoogleApiError("Google API error 403: disabled", status_code=403)
        with patch("places_search.request_json", side_effect=error) as request, patch(
            "places_search.time.sleep"
        ) as sleep:
            with self.assertRaises(places_search.GoogleApiError):
                places_search._text_search_page({}, {})

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_google_api_error_is_still_a_runtime_error(self) -> None:
        # Callers up the stack catch RuntimeError; this must not slip past them.
        self.assertTrue(issubclass(places_search.GoogleApiError, RuntimeError))


class DuplicateCollisionTest(unittest.TestCase):
    def test_two_different_names_sharing_a_phone_are_flagged(self) -> None:
        kept = MagicMock(business_name="Cleveland Plumbing Co", place_id="place-1")
        candidate = MagicMock(business_name="Akron Drain Experts", place_id="place-2")

        self.assertTrue(places_search._distinct_business(kept, candidate))

    def test_the_same_business_listed_twice_is_not_flagged(self) -> None:
        kept = MagicMock(business_name="Cleveland Plumbing Co", place_id="place-1")
        same_place = MagicMock(business_name="Cleveland Plumbing", place_id="place-1")
        same_name = MagicMock(business_name="Cleveland Plumbing Co", place_id="place-2")

        self.assertFalse(places_search._distinct_business(kept, same_place))
        self.assertFalse(places_search._distinct_business(kept, same_name))


class LlmPricingTest(unittest.TestCase):
    def test_the_default_scoring_model_is_priced(self) -> None:
        from score_leads import DEFAULT_LLM_SCORING_MODEL

        self.assertIn(DEFAULT_LLM_SCORING_MODEL, LLM_PRICING_USD_PER_MILLION_TOKENS)

    def test_cost_follows_the_model_rather_than_a_fixed_rate(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}

        sonnet = estimate_llm_usd(usage, model="claude-sonnet-5")
        opus = estimate_llm_usd(usage, model="claude-opus-5")

        self.assertEqual(sonnet, 18.0)
        self.assertEqual(opus, 90.0)
        self.assertGreater(opus, sonnet)

    def test_a_dated_snapshot_prices_as_its_base_model(self) -> None:
        self.assertEqual(
            llm_pricing("claude-sonnet-5-20260101"), LLM_PRICING_USD_PER_MILLION_TOKENS["claude-sonnet-5"]
        )

    def test_an_unknown_model_raises_rather_than_being_mispriced(self) -> None:
        # A silently wrong price on a spend cap is worse than a refusal:
        # MAX_LLM_USD_PER_RUN would be enforcing a number that means nothing.
        with self.assertRaises(UnknownLlmModelError) as caught:
            llm_pricing("some-new-model-v9")

        self.assertIn("No pricing registered", str(caught.exception))

    def test_an_empty_model_is_refused(self) -> None:
        with self.assertRaises(UnknownLlmModelError):
            llm_pricing("")


if __name__ == "__main__":
    unittest.main()
