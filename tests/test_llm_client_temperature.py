"""§4.3 — `temperature: 0` is pinned on every API provider whose model accepts it.

Non-reproducible grades are the defect: against an earlier partial run's 25
overlapping leads, 14 grades moved with no scoring code changed (three A→B, nine
Skip→C, scores swinging ±7). The API payload carried no `temperature`, `top_p`
or `seed`, so nothing pinned sampling.

The pin is model-aware, not provider-aware, because the two API providers'
*default* models both fix sampling server-side and would reject or ignore the
key (see `SAMPLING_FIXED_MODEL_PREFIXES` in llm_client). Sending it anyway is a
hard 400 mid-batch, which is strictly worse than losing determinism, so these
tests pin both directions: sent when the model accepts it, absent when it does
not.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lead_machine import llm_client  # noqa: E402


def _score() -> dict:
    return {
        "total_score": 75,
        "grade": "B",
        "reasons": ["r1"],
        "pain_points": ["p1"],
        "opportunities": ["o1"],
    }


def _bundle() -> dict:
    return {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": _score()}]}


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _anthropic_response() -> _FakeResponse:
    return _FakeResponse(
        {
            "content": [{"type": "text", "text": json.dumps(_bundle())}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def _kimi_response() -> _FakeResponse:
    return _FakeResponse(
        {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(_bundle())}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


class ModelTemperatureSupportTest(unittest.TestCase):
    """Which model ids can carry an explicit `temperature`."""

    def test_models_with_server_fixed_sampling_do_not_accept_temperature(self) -> None:
        for model in (
            "kimi-k3",
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-fable-5",
            "claude-mythos-5",
        ):
            with self.subTest(model=model):
                self.assertFalse(llm_client.model_accepts_temperature(model))

    def test_older_models_still_accept_temperature(self) -> None:
        for model in (
            "kimi-k2",
            "kimi-k2-0905-preview",
            "claude-sonnet-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5",
        ):
            with self.subTest(model=model):
                self.assertTrue(llm_client.model_accepts_temperature(model))

    def test_match_is_by_family_prefix_so_dated_variants_are_covered(self) -> None:
        # A dated or suffixed id is the same family and behaves the same way.
        self.assertFalse(llm_client.model_accepts_temperature("claude-sonnet-5-20260214"))
        self.assertFalse(llm_client.model_accepts_temperature("kimi-k3-turbo"))
        # ...but a shorter, older id must not be swallowed by the prefix match.
        self.assertTrue(llm_client.model_accepts_temperature("claude-sonnet-4-5-20250929"))

    def test_match_is_case_and_whitespace_insensitive(self) -> None:
        self.assertFalse(llm_client.model_accepts_temperature("  Kimi-K3  "))

    def test_unknown_model_is_treated_as_accepting(self) -> None:
        self.assertTrue(llm_client.model_accepts_temperature("some-future-model"))

    def test_empty_model_omits_rather_than_guessing(self) -> None:
        self.assertFalse(llm_client.model_accepts_temperature(""))

    def test_temperature_for_model_returns_the_pin_or_none(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_PIN_TEMPERATURE", None)
            self.assertEqual(llm_client.temperature_for_model("claude-sonnet-4-5"), 0)
            self.assertIsNone(llm_client.temperature_for_model("claude-sonnet-5"))

    def test_kill_switch_omits_temperature_even_on_a_supporting_model(self) -> None:
        # Escape hatch for a new model that also rejects sampling: unblock the
        # run from .env instead of shipping a code change mid-backlog.
        with patch.dict(os.environ, {"LLM_PIN_TEMPERATURE": "0"}, clear=False):
            self.assertIsNone(llm_client.temperature_for_model("claude-sonnet-4-5"))


class _PayloadTestCase(unittest.TestCase):
    """These assert request shape, so the v3 bundle validator is out of scope."""

    def setUp(self) -> None:
        patcher = patch("lead_machine.llm_client.validate_bundle_scores", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)


class AnthropicPayloadTemperatureTest(_PayloadTestCase):
    def test_payload_pins_temperature_zero_on_a_supporting_model(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "LLM_SCORING_PROVIDER_CHAIN": "anthropic",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("LLM_PIN_TEMPERATURE", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch(
                    "lead_machine.llm_client.requests.post", return_value=_anthropic_response()
                ) as post:
                    llm_client.score_batch(
                        "score this batch", model="claude-sonnet-4-5", max_tokens=4096
                    )

        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["temperature"], 0)

    def test_payload_omits_temperature_on_the_default_sonnet_5_model(self) -> None:
        # claude-sonnet-5 removed temperature/top_p/top_k from the Messages API;
        # sending one is a 400, which would kill the batch outright.
        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "LLM_SCORING_PROVIDER_CHAIN": "anthropic",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("LLM_SCORING_MODEL", None)
            os.environ.pop("LLM_PIN_TEMPERATURE", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch(
                    "lead_machine.llm_client.requests.post", return_value=_anthropic_response()
                ) as post:
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], llm_client.DEFAULT_LLM_SCORING_MODEL)
        self.assertNotIn("temperature", kwargs["json"])


class KimiPayloadTemperatureTest(_PayloadTestCase):
    def test_payload_pins_temperature_zero_on_a_supporting_model(self) -> None:
        env = {
            "KIMI_API_KEY": "kimi-key",
            "LLM_SCORING_PROVIDER_CHAIN": "kimi",
            "KIMI_SCORING_MODEL": "kimi-k2-0905-preview",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("LLM_PIN_TEMPERATURE", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch(
                    "lead_machine.llm_client.requests.post", return_value=_kimi_response()
                ) as post:
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["temperature"], 0)
        # The pin must not displace the existing reasoning knob.
        self.assertEqual(kwargs["json"]["reasoning_effort"], "low")

    def test_payload_omits_temperature_on_the_default_k3_model(self) -> None:
        # Moonshot fixes temperature=1.0 server-side on kimi-k3 and documents
        # that it must be omitted from requests.
        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            os.environ.pop("LLM_PIN_TEMPERATURE", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch(
                    "lead_machine.llm_client.requests.post", return_value=_kimi_response()
                ) as post:
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], llm_client.DEFAULT_KIMI_SCORING_MODEL)
        self.assertNotIn("temperature", kwargs["json"])


class CliRunnerTemperatureGapTest(unittest.TestCase):
    """T2.2 cannot reach the codex/claude CLI runners — pin that as a fact."""

    def test_cli_runner_invocation_carries_no_sampling_parameter(self) -> None:
        import score_batch_subscription as subscription_runner

        for runner in ("codex", "claude"):
            with self.subTest(runner=runner):
                cmd = (
                    subscription_runner.build_codex_command(
                        cwd=Path("/tmp"),
                        last_message_path=Path("/tmp/last_message.txt"),
                        model="",
                        use_output_schema=True,
                        reasoning_effort="medium",
                    )
                    if runner == "codex"
                    else subscription_runner.build_claude_command(
                        model="", reasoning_effort="medium"
                    )
                )
                joined = " ".join(cmd).lower()
                self.assertNotIn("temperature", joined)
                self.assertNotIn("top_p", joined)
                self.assertNotIn("seed", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
