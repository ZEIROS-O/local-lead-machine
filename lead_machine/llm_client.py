from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict

import requests
from dotenv import load_dotenv

from import_llm_score_bundle import validate_bundle_scores


ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_LLM_SCORING_MODEL = "claude-sonnet-5"
DEFAULT_ANTHROPIC_CONNECT_TIMEOUT_SECONDS = 15
DEFAULT_ANTHROPIC_READ_TIMEOUT_SECONDS = 180
DEFAULT_ANTHROPIC_MAX_RETRIES = 2
DEFAULT_ANTHROPIC_RETRY_BACKOFF_SECONDS = 2
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class InvalidLlmResponse(ValueError):
    def __init__(self, message: str, *, raw_text: str, parsed_json: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.parsed_json = parsed_json


def _response_text(data: Dict[str, Any]) -> str:
    parts = []
    for item in data.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise RuntimeError("Anthropic response did not include text content")
    return text


def _parse_score_bundle(text: str) -> Dict[str, Any]:
    try:
        bundle = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidLlmResponse(
            f"Invalid LLM score bundle JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
            raw_text=text,
        ) from exc
    if not isinstance(bundle, dict):
        raise ValueError("LLM score bundle must be one JSON object")
    try:
        validate_bundle_scores(bundle)
    except ValueError as exc:
        raise InvalidLlmResponse(str(exc), raw_text=text, parsed_json=bundle) from exc
    return bundle


def _usage(data: Dict[str, Any]) -> Dict[str, int]:
    raw_usage = data.get("usage") or {}
    input_tokens = int(raw_usage.get("input_tokens") or 0)
    output_tokens = int(raw_usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def _getenv_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[WARN] Invalid {name}={raw!r}; using {default}", file=sys.stderr)
        return default


def _estimate_prompt_tokens(prompt: str) -> int:
    if not prompt:
        return 0
    return max(1, (len(prompt) + 3) // 4)


def _seconds_for_log(seconds: float) -> str:
    if seconds == int(seconds):
        return str(int(seconds))
    return str(seconds)


def _request_failure_message(
    exc: BaseException,
    *,
    model: str,
    connect_timeout: int,
    read_timeout: int,
    attempts: int,
) -> str:
    return (
        "Anthropic API request failed after retries: "
        f"model={model} "
        f"connect_timeout={connect_timeout} "
        f"read_timeout={read_timeout} "
        f"attempts={attempts} "
        f"original_exception={exc}"
    )


def _transient_request_reason(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    return exc.__class__.__name__


def score_batch(prompt: str, *, model: str | None = None, max_tokens: int) -> Dict[str, Any]:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY in .env")

    selected_model = (model or os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip()
    connect_timeout = _getenv_int("ANTHROPIC_CONNECT_TIMEOUT_SECONDS", DEFAULT_ANTHROPIC_CONNECT_TIMEOUT_SECONDS)
    read_timeout = _getenv_int("ANTHROPIC_READ_TIMEOUT_SECONDS", DEFAULT_ANTHROPIC_READ_TIMEOUT_SECONDS)
    max_retries = max(0, _getenv_int("ANTHROPIC_MAX_RETRIES", DEFAULT_ANTHROPIC_MAX_RETRIES))
    backoff_seconds = max(
        0.0,
        _getenv_float("ANTHROPIC_RETRY_BACKOFF_SECONDS", float(DEFAULT_ANTHROPIC_RETRY_BACKOFF_SECONDS)),
    )
    max_attempts = max_retries + 1
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    body = {
        "model": selected_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    prompt_tokens_est = _estimate_prompt_tokens(prompt)

    for attempt in range(1, max_attempts + 1):
        print(
            f"[LLM REQUEST] attempt={attempt} model={selected_model} "
            f"prompt_tokens_est={prompt_tokens_est} max_tokens={max_tokens}"
        )
        try:
            response = requests.post(
                ANTHROPIC_MESSAGES_URL,
                headers=headers,
                json=body,
                timeout=(connect_timeout, read_timeout),
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    _request_failure_message(
                        exc,
                        model=selected_model,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                        attempts=attempt,
                    )
                ) from exc
            sleeping = backoff_seconds * (2 ** (attempt - 1))
            print(
                f"[LLM RETRY] attempt={attempt} "
                f"reason={_transient_request_reason(exc)} sleeping={_seconds_for_log(sleeping)}s"
            )
            time.sleep(sleeping)
            continue

        if response.status_code >= 400:
            error = RuntimeError(f"Anthropic API error {response.status_code}: {response.text[:1000]}")
            if response.status_code not in TRANSIENT_HTTP_STATUSES:
                raise error
            if attempt >= max_attempts:
                raise RuntimeError(
                    _request_failure_message(
                        error,
                        model=selected_model,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                        attempts=attempt,
                    )
                ) from error
            sleeping = backoff_seconds * (2 ** (attempt - 1))
            print(f"[LLM RETRY] attempt={attempt} reason=http_{response.status_code} sleeping={_seconds_for_log(sleeping)}s")
            time.sleep(sleeping)
            continue

        data = response.json()
        text = _response_text(data)
        bundle = _parse_score_bundle(text)
        usage = _usage(data)
        print(
            f"[LLM REQUEST OK] attempt={attempt} "
            f"input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']}"
        )
        return {"bundle": bundle, "usage": usage}

    raise RuntimeError("Anthropic API request failed without making an attempt")
