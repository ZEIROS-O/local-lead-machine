from __future__ import annotations

import json
import os
from typing import Any, Dict

import requests
from dotenv import load_dotenv

from import_llm_score_bundle import validate_bundle_scores


ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_LLM_SCORING_MODEL = "claude-sonnet-4-6"


class InvalidLlmResponse(ValueError):
    def __init__(self, message: str, *, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


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
    validate_bundle_scores(bundle)
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


def score_batch(prompt: str, *, model: str | None = None, max_tokens: int) -> Dict[str, Any]:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY in .env")

    selected_model = (model or os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip()
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

    try:
        response = requests.post(ANTHROPIC_MESSAGES_URL, headers=headers, json=body, timeout=60)
    except requests.RequestException as exc:
        raise RuntimeError(f"Anthropic API request failed: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"Anthropic API error {response.status_code}: {response.text[:1000]}")

    data = response.json()
    text = _response_text(data)
    return {"bundle": _parse_score_bundle(text), "usage": _usage(data)}
