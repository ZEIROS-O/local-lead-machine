from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List

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
DEFAULT_KIMI_BASE_URL = "https://api.moonshot.ai/v1"
DEFAULT_KIMI_SCORING_MODEL = "kimi-k3"
# kimi-k3 always thinks; reasoning_effort only tunes how much. "low" is the
# lightest setting Moonshot exposes — the default "max" burns the whole
# max_tokens budget on reasoning_content before any score JSON is written,
# which is unnecessary for a fixed-rubric scoring task and starves `content`.
DEFAULT_KIMI_REASONING_EFFORT = "low"
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

DEFAULT_PROVIDER_CHAIN = ("kimi", "codex", "claude")

# §4.3 — grades have to be reproducible. Re-scoring 25 overlapping leads with no
# scoring code changed moved 14 grades (three A→B, nine Skip→C, scores swinging
# ±7) because the payload pinned no sampling at all. 0 is the pin.
PINNED_TEMPERATURE = 0

# ...but the pin only applies where the model will take it. Both API providers'
# *current* default models fix sampling server-side, and sending the key anyway
# is not a no-op:
#   * kimi-k3 — Moonshot fixes temperature=1.0 / top_p=0.95 and documents that
#     they must be omitted from requests. Thinking is always on.
#   * claude-sonnet-5 / opus-5 / opus-4-8 / opus-4-7 / fable-5 / mythos-5 —
#     temperature, top_p and top_k were removed from the Messages API and now
#     return a hard 400. Older ids (sonnet-4-5, sonnet-4-6, opus-4-6, haiku-4-5)
#     still accept them.
# A 400 kills the batch mid-run; a missing pin only costs determinism, so an id
# in this set omits the key rather than gambling the run on it. Matching is by
# family prefix so dated variants (claude-sonnet-5-20260214) are covered too.
SAMPLING_FIXED_MODEL_PREFIXES = (
    "kimi-k3",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
)


class ProviderUnavailable(RuntimeError):
    """A scoring provider cannot serve this call (missing key, auth, quota, transport)."""


class InvalidLlmResponse(ValueError):
    def __init__(
        self,
        message: str,
        *,
        raw_text: str,
        parsed_json: Dict[str, Any] | None = None,
        usage: Dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.parsed_json = parsed_json
        # Usage for the failed attempt, so callers can still charge it against
        # per-run token/USD caps even though the response never validated.
        self.usage = usage or {}
        # Which model burned that usage. The chain can fall past a $0
        # subscription runner onto a paid API, so the run-level model is the
        # wrong price for a failed attempt too.
        self.model = ""


def _response_text(data: Dict[str, Any]) -> str:
    parts = []
    for item in data.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise InvalidLlmResponse(
            "Anthropic response did not include text content",
            raw_text=json.dumps(data, ensure_ascii=False, indent=2),
        )
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


def _response_text_openai(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
        if text:
            return text
    raise InvalidLlmResponse(
        "Kimi response did not include message content",
        raw_text=json.dumps(data, ensure_ascii=False, indent=2),
    )


def _usage_openai(data: Dict[str, Any]) -> Dict[str, int]:
    raw_usage = data.get("usage") or {}
    input_tokens = int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0)
    output_tokens = int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0)
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


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def model_accepts_temperature(model: str) -> bool:
    """Whether `model` will take an explicit `temperature` in its request payload.

    False for the families in `SAMPLING_FIXED_MODEL_PREFIXES`, and for an empty
    id — an unresolved model is not something to gamble a billed batch on.
    """
    normalized = (model or "").strip().lower()
    if not normalized:
        return False
    return not normalized.startswith(SAMPLING_FIXED_MODEL_PREFIXES)


def temperature_for_model(model: str) -> int | None:
    """`PINNED_TEMPERATURE` when the model supports it, else None (omit the key).

    `LLM_DISABLE_TEMPERATURE_PIN=1` omits it everywhere — the escape hatch for a
    model that rejects sampling but is not yet in `SAMPLING_FIXED_MODEL_PREFIXES`,
    so a run can be unblocked from `.env` instead of a code change mid-backlog.
    Named for the disable rather than the pin on purpose: the pinned value *is*
    zero, so a `LLM_PIN_TEMPERATURE=0` would read equally as "pin it to 0" and
    "do not pin", and those are opposite outcomes.
    """
    if _getenv_bool("LLM_DISABLE_TEMPERATURE_PIN", False):
        return None
    return PINNED_TEMPERATURE if model_accepts_temperature(model) else None


def _temperature_for_log(temperature: int | None) -> str:
    return "unset(model-fixed)" if temperature is None else str(temperature)


def _batch_for_log(batch_id: str) -> str:
    """`batch=<id> ` prefix for a log line, or empty when the caller gave none."""
    cleaned = str(batch_id or "").strip()
    return f"batch={cleaned} " if cleaned else ""


# One pooled HTTP session per thread. The concurrent scoring path runs
# LLM_SCORING_CONCURRENCY workers against one host, and a bare requests.post
# opens and tears down a fresh TCP+TLS connection per call — the handshake is
# paid on every batch. requests.Session is not documented thread-safe, so the
# pool is thread-local rather than shared: each worker reuses its own keep-alive
# connection across its batches, and no two threads touch one session.
_SESSIONS = threading.local()


def _session() -> "requests.Session":
    existing = getattr(_SESSIONS, "session", None)
    if existing is None:
        existing = requests.Session()
        _SESSIONS.session = existing
    return existing


def _post(url: str, **kwargs: Any) -> "requests.Response":
    """The single outbound HTTP seam for this module.

    Every provider POSTs through here rather than calling `requests.post` or a
    session directly. One seam means one thing for tests to patch — when the
    pooled session landed, the suite was still patching `requests.post`, so nine
    "mocked" tests silently made live API calls instead of failing. A contract
    test pins that no other outbound call exists.
    """
    return _session().post(url, **kwargs)


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


_dotenv_lock = threading.Lock()
_dotenv_loaded = False


def _ensure_dotenv_loaded() -> None:
    """Load .env exactly once per process.

    `load_dotenv()` mutates os.environ. Calling it per request was harmless while
    scoring was serial; with batches in flight concurrently it is N threads
    writing a shared dict. Double-checked locking keeps the fast path lock-free.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    with _dotenv_lock:
        if _dotenv_loaded:
            return
        load_dotenv()
        _dotenv_loaded = True


def _reset_dotenv_loaded_for_tests() -> None:
    global _dotenv_loaded
    with _dotenv_lock:
        _dotenv_loaded = False


def _score_batch_anthropic(
    prompt: str, *, model: str | None = None, max_tokens: int, batch_id: str = ""
) -> Dict[str, Any]:
    _ensure_dotenv_loaded()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ProviderUnavailable("Missing ANTHROPIC_API_KEY in .env")

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
    temperature = temperature_for_model(selected_model)
    if temperature is not None:
        body["temperature"] = temperature
    prompt_tokens_est = _estimate_prompt_tokens(prompt)

    for attempt in range(1, max_attempts + 1):
        print(
            f"[LLM REQUEST] {_batch_for_log(batch_id)}attempt={attempt} model={selected_model} "
            f"prompt_tokens_est={prompt_tokens_est} max_tokens={max_tokens} "
            f"temperature={_temperature_for_log(temperature)}"
        )
        try:
            response = _post(
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
        usage = _usage(data)
        try:
            text = _response_text(data)
            bundle = _parse_score_bundle(text)
        except InvalidLlmResponse as exc:
            # Preserve the paid usage from this attempt so token/USD caps and
            # cost dashboards see it even when the response is unusable.
            exc.usage = usage
            exc.model = selected_model
            raise
        print(
            f"[LLM REQUEST OK] attempt={attempt} "
            f"input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']}"
        )
        return {"bundle": bundle, "usage": usage, "provider": "anthropic", "model": selected_model}

    raise RuntimeError("Anthropic API request failed without making an attempt")


def _score_batch_kimi(
    prompt: str, *, model: str | None, max_tokens: int, batch_id: str = ""
) -> Dict[str, Any]:
    api_key = (os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or "").strip()
    if not api_key:
        raise ProviderUnavailable("Missing KIMI_API_KEY (or MOONSHOT_API_KEY) in .env")

    base_url = (os.getenv("KIMI_BASE_URL") or DEFAULT_KIMI_BASE_URL).strip().rstrip("/")
    selected_model = (
        model
        or os.getenv("KIMI_SCORING_MODEL")
        or os.getenv("LLM_SCORING_MODEL")  # legacy alias
        or DEFAULT_KIMI_SCORING_MODEL
    ).strip()
    connect_timeout = _getenv_int("KIMI_CONNECT_TIMEOUT_SECONDS", DEFAULT_ANTHROPIC_CONNECT_TIMEOUT_SECONDS)
    read_timeout = _getenv_int("KIMI_READ_TIMEOUT_SECONDS", DEFAULT_ANTHROPIC_READ_TIMEOUT_SECONDS)
    max_retries = max(0, _getenv_int("KIMI_MAX_RETRIES", DEFAULT_ANTHROPIC_MAX_RETRIES))
    backoff_seconds = max(
        0.0,
        _getenv_float("KIMI_RETRY_BACKOFF_SECONDS", float(DEFAULT_ANTHROPIC_RETRY_BACKOFF_SECONDS)),
    )
    max_attempts = max_retries + 1
    reasoning_effort = (os.getenv("KIMI_REASONING_EFFORT") or DEFAULT_KIMI_REASONING_EFFORT).strip()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "model": selected_model,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "messages": [{"role": "user", "content": prompt}],
    }
    temperature = temperature_for_model(selected_model)
    if temperature is not None:
        body["temperature"] = temperature
    url = f"{base_url}/chat/completions"
    prompt_tokens_est = _estimate_prompt_tokens(prompt)

    for attempt in range(1, max_attempts + 1):
        print(
            f"[LLM REQUEST] {_batch_for_log(batch_id)}provider=kimi attempt={attempt} model={selected_model} "
            f"prompt_tokens_est={prompt_tokens_est} max_tokens={max_tokens} "
            f"reasoning_effort={reasoning_effort} "
            f"temperature={_temperature_for_log(temperature)}"
        )
        try:
            response = _post(url, headers=headers, json=body, timeout=(connect_timeout, read_timeout))
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= max_attempts:
                raise ProviderUnavailable(f"kimi request failed after {attempt} attempts: {exc}") from exc
            sleeping = backoff_seconds * (2 ** (attempt - 1))
            print(f"[LLM RETRY] provider=kimi attempt={attempt} reason={_transient_request_reason(exc)} sleeping={_seconds_for_log(sleeping)}s")
            time.sleep(sleeping)
            continue

        if response.status_code >= 400:
            error = ProviderUnavailable(f"kimi API error {response.status_code}: {response.text[:1000]}")
            if response.status_code not in TRANSIENT_HTTP_STATUSES:
                raise error
            if attempt >= max_attempts:
                raise error
            sleeping = backoff_seconds * (2 ** (attempt - 1))
            print(f"[LLM RETRY] provider=kimi attempt={attempt} reason=http_{response.status_code} sleeping={_seconds_for_log(sleeping)}s")
            time.sleep(sleeping)
            continue

        data = response.json()
        usage = _usage_openai(data)
        try:
            text = _response_text_openai(data)
            bundle = _parse_score_bundle(text)
        except InvalidLlmResponse as exc:
            exc.usage = usage
            exc.model = selected_model
            raise
        print(
            f"[LLM REQUEST OK] provider=kimi attempt={attempt} "
            f"input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']}"
        )
        return {"bundle": bundle, "usage": usage, "provider": "kimi", "model": selected_model}

    raise ProviderUnavailable("kimi request failed without making an attempt")


def _anthropic_provider(
    prompt: str, *, model: str | None, max_tokens: int, batch_id: str = ""
) -> Dict[str, Any]:
    try:
        return _score_batch_anthropic(
            prompt, model=model, max_tokens=max_tokens, batch_id=batch_id
        )
    except ProviderUnavailable:
        raise
    except InvalidLlmResponse:
        raise
    except RuntimeError as exc:
        raise ProviderUnavailable(str(exc)) from exc


DEFAULT_CLI_TIMEOUT_SECONDS = 900


def _normalize_cli_usage(raw: Dict[str, Any] | None) -> Dict[str, int]:
    raw = raw or {}

    def _pick(*keys: str) -> int:
        for key in keys:
            try:
                value = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value:
                return value
        return 0

    def _int(key: str) -> int:
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _pick("input_tokens", "prompt_tokens")
    # The Claude CLI envelope reports nearly all prompt tokens under the cache
    # counters — a 3.7k-token scoring prompt comes back as input_tokens=1 with
    # the rest in cache_creation_input_tokens / cache_read_input_tokens. Dropping
    # them under-counts input by ~99%, so MAX_LLM_TOKENS_PER_RUN stops bounding
    # subscription runs.
    input_tokens += _int("cache_creation_input_tokens") + _int("cache_read_input_tokens")
    output_tokens = _pick("output_tokens", "completion_tokens")
    total = max(_pick("total_tokens"), input_tokens + output_tokens)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}


def _score_batch_cli(
    runner: str, prompt: str, *, model: str | None, max_tokens: int, batch_id: str = ""
) -> Dict[str, Any]:
    import score_batch_subscription as subscription_runner

    env = subscription_runner.sanitized_environment()
    auth = subscription_runner.preflight_subscription_auth(runner, env=env)
    if not auth.ok:
        raise ProviderUnavailable(f"{runner} subscription auth not verified: {auth.detail}")

    env_key = "CODEX_SUBSCRIPTION_SCORING_MODEL" if runner == "codex" else "CLAUDE_SUBSCRIPTION_SCORING_MODEL"
    selected_model = (os.getenv(env_key) or "").strip()
    timeout_seconds = max(1, _getenv_int("LLM_CLI_TIMEOUT_SECONDS", DEFAULT_CLI_TIMEOUT_SECONDS))
    effort = subscription_runner.resolve_runner_effort(runner)
    print(
        f"[LLM REQUEST] {_batch_for_log(batch_id)}provider={runner} model={selected_model or 'runner-default'} "
        f"effort={effort or 'runner-default'} via CLI subscription"
    )
    result = subscription_runner.run_runner_attempt(
        runner,
        prompt_text=prompt,
        model=selected_model,
        timeout_seconds=timeout_seconds,
        env=env,
        use_output_schema=(runner == "codex"),
    )
    if result.result != "ok":
        raise ProviderUnavailable(f"{runner} CLI runner result={result.result}: {result.stderr_tail}")

    text = subscription_runner.clean_bundle_text(result.raw_text)
    bundle = _parse_score_bundle(text)  # InvalidLlmResponse propagates: no fallback on bad output
    usage = _normalize_cli_usage(result.usage)
    print(
        f"[LLM REQUEST OK] provider={runner} "
        f"input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']}"
    )
    # `model` is what the caller prices the batch with, so it must be the $0
    # subscription id — billing a sub-served batch at the runner model's API
    # rate would charge MAX_LLM_USD_PER_RUN for tokens that cost nothing.
    # The real runner model stays available for logging as `runner_model`.
    return {
        "bundle": bundle,
        "usage": usage,
        "provider": runner,
        "model": f"subscription:{runner}",
        "runner_model": selected_model or "runner-default",
    }


_PROVIDERS: Dict[str, Callable] = {
    "kimi": _score_batch_kimi,
    "codex": lambda prompt, *, model, max_tokens, batch_id="": _score_batch_cli(
        "codex", prompt, model=model, max_tokens=max_tokens, batch_id=batch_id
    ),
    "claude": lambda prompt, *, model, max_tokens, batch_id="": _score_batch_cli(
        "claude", prompt, model=model, max_tokens=max_tokens, batch_id=batch_id
    ),
    "anthropic": _anthropic_provider,
}


def _provider_chain() -> List[str]:
    raw = os.getenv("LLM_SCORING_PROVIDER_CHAIN", "").strip()
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    if not names:
        names = list(DEFAULT_PROVIDER_CHAIN)
    # A name is "recognized" if it's a documented chain member or a currently
    # registered provider, even if this build hasn't wired its implementation
    # in yet (e.g. kimi/codex land in later tasks). Only genuinely unknown
    # names (typos, retired providers) get skipped here.
    known_names = set(DEFAULT_PROVIDER_CHAIN) | set(_PROVIDERS)
    chain: List[str] = []
    for name in names:
        if name not in known_names:
            print(f"[WARN] Unknown LLM provider {name!r} in LLM_SCORING_PROVIDER_CHAIN; skipping", file=sys.stderr)
            continue
        if name not in chain:
            chain.append(name)
    return chain


def score_batch(
    prompt: str, *, model: str | None = None, max_tokens: int, batch_id: str = ""
) -> Dict[str, Any]:
    """Score one batch through the provider chain.

    `batch_id` is logging-only. At LLM_SCORING_CONCURRENCY>1 several batches are
    in flight at once and their `[LLM REQUEST]` lines interleave on one stdout,
    so without it a retry or a fallback cannot be attributed to the batch that
    caused it — which is the line an operator reads when reconciling spend.
    """
    _ensure_dotenv_loaded()
    failures: List[str] = []
    for index, provider in enumerate(_provider_chain()):
        # An explicit model belongs to the provider it was chosen for. Handing a
        # kimi id to a fallback API provider would 400 the rest of the chain, so
        # every provider after the first resolves its own model from env.
        requested_model = model if index == 0 else None
        provider_fn = _PROVIDERS.get(provider)
        if provider_fn is None:
            # Recognized chain member (e.g. kimi/codex before their provider
            # lands) but not registered yet in this build. Treat like any
            # other unavailability rather than crashing the whole chain.
            reason = "provider not registered"
            failures.append(f"{provider}: {reason}")
            print(
                f"[LLM FALLBACK] {_batch_for_log(batch_id)}provider={provider} reason={reason}",
                file=sys.stderr,
            )
            continue
        try:
            result = provider_fn(
                prompt, model=requested_model, max_tokens=max_tokens, batch_id=batch_id
            )
        except InvalidLlmResponse:
            raise
        except ProviderUnavailable as exc:
            failures.append(f"{provider}: {exc}")
            print(
                f"[LLM FALLBACK] {_batch_for_log(batch_id)}provider={provider} reason={exc}",
                file=sys.stderr,
            )
            continue
        result.setdefault("provider", provider)
        return result
    raise RuntimeError("All LLM scoring providers unavailable: " + "; ".join(failures))


def provider_chain() -> List[str]:
    """The resolved fallback chain, in order. Public for cost projection."""
    _ensure_dotenv_loaded()
    return _provider_chain()


def model_for_provider(provider: str) -> str:
    """Model id a given provider in the chain would use."""
    if provider == "kimi":
        return (
            os.getenv("KIMI_SCORING_MODEL")
            or os.getenv("LLM_SCORING_MODEL")  # legacy alias
            or DEFAULT_KIMI_SCORING_MODEL
        ).strip()
    if provider in {"codex", "claude"}:
        return f"subscription:{provider}"
    return (os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip()


def resolve_scoring_model() -> str:
    """Model id the chain's first provider will use — for logging and reporting.

    NOTE: this is the *expected* model, not a spend guard. A pre-spend
    projection must use the priciest model the chain could reach — see
    `score_leads.resolve_projection_model` — because a $0 subscription runner
    first in the chain would otherwise project $0 for a run that falls through
    to a paid API and spends real money.
    """
    _ensure_dotenv_loaded()
    chain = _provider_chain()
    first = chain[0] if chain else "anthropic"
    if first == "kimi":
        return (
            os.getenv("KIMI_SCORING_MODEL")
            or os.getenv("LLM_SCORING_MODEL")  # legacy alias
            or DEFAULT_KIMI_SCORING_MODEL
        ).strip()
    if first in {"codex", "claude"}:
        return f"subscription:{first}"
    return (os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip()
