# Multi-Provider LLM Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `lead_machine/llm_client.py` score via a provider chain — kimi (Moonshot API) by default, codex CLI (ChatGPT subscription) fallback 1, claude CLI (subscription) fallback 2 — with the existing Anthropic API kept as an optional provider.

**Architecture:** `score_batch(prompt, *, model=None, max_tokens)` becomes a chain driver over a `_PROVIDERS` registry. Each provider is a function `(prompt, *, model, max_tokens) -> {"bundle", "usage", "provider", "model"}` that raises `ProviderUnavailable` when it cannot serve (missing key, auth failure, quota, timeout, transport error) and lets `InvalidLlmResponse` propagate (no fallback on output-quality problems). CLI providers reuse `score_batch_subscription.py` helpers via import, the same way `llm_client.py` already imports `import_llm_score_bundle`.

**Tech Stack:** Python 3.10+ stdlib + `requests` + `python-dotenv` (already used); stdlib `unittest` tests; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md`

## Global Constraints

- Tests are stdlib `unittest`; run `python -m unittest discover -s tests` (bare `python -m unittest` discovers 0 tests here).
- No new heavy deps; HTTP via `requests` only.
- Follow the existing `_getenv_int`/`_getenv_float` + `[WARN]` pattern in `lead_machine/llm_client.py` for env config.
- Fallback only on unavailability; `InvalidLlmResponse` always propagates without trying the next provider.
- CLI subprocesses are never spawned in unit tests — patch `score_batch_subscription.run_runner_attempt` / `preflight_subscription_auth`.
- Default chain (when `LLM_SCORING_PROVIDER_CHAIN` is unset): `kimi,codex,claude`. `anthropic` is a valid but non-default provider name.

---

### Task 1: Provider registry, chain driver, and anthropic refactor

**Files:**
- Modify: `lead_machine/llm_client.py` (whole file restructure)
- Test: `tests/test_lead_machine_contracts.py` (update existing `test_llm_client_*` tests ~lines 2345-2517; add new tests after them)

**Interfaces:**
- Consumes: existing `validate_bundle_scores` import; existing `InvalidLlmResponse`, `_parse_score_bundle`, `_usage`, `_estimate_prompt_tokens`.
- Produces:
  - `class ProviderUnavailable(RuntimeError)` — provider cannot serve this call.
  - `DEFAULT_PROVIDER_CHAIN = ("kimi", "codex", "claude")`
  - `_PROVIDERS: Dict[str, Callable]` — name → provider function. After this task it contains only `"anthropic"`; later tasks add the rest.
  - `_provider_chain() -> List[str]` — parses `LLM_SCORING_PROVIDER_CHAIN`.
  - `score_batch(prompt: str, *, model: str | None = None, max_tokens: int) -> Dict[str, Any]` — chain driver; same signature as today, return dict gains `"provider"` and `"model"` keys.

- [ ] **Step 1: Update existing tests to pin the anthropic chain**

The six existing tests at `tests/test_lead_machine_contracts.py:2345-2517` (`test_llm_client_posts_anthropic_message_and_parses_score_bundle`, `..._uses_configured_connect_and_read_timeout_tuple`, `..._retries_timeout_up_to_max_retries`, `..._retries_500_and_503_responses`, `..._does_not_retry_deterministic_400_response`, `..._final_retry_error_includes_timeout_settings_and_attempts`) each use `patch.dict(os.environ, {...}, clear=False)`. Add `"LLM_SCORING_PROVIDER_CHAIN": "anthropic"` to every one of those env dicts so they keep exercising the Anthropic provider directly regardless of the new default chain.

- [ ] **Step 2: Write the failing tests**

Add after the existing llm_client tests:

```python
    def test_llm_client_chain_skips_unknown_provider_names(self) -> None:
        from lead_machine import llm_client

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,bogus,claude"}, clear=False):
            chain = llm_client._provider_chain()

        self.assertEqual(chain, ["kimi", "claude"])

    def test_llm_client_chain_defaults_to_kimi_codex_claude(self) -> None:
        from lead_machine import llm_client

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_SCORING_PROVIDER_CHAIN", None)
            chain = llm_client._provider_chain()

        self.assertEqual(chain, ["kimi", "codex", "claude"])

    def test_llm_client_falls_back_to_next_provider_on_unavailable(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}

        def fake_down(prompt, *, model, max_tokens):
            raise llm_client.ProviderUnavailable("no api key")

        def fake_up(prompt, *, model, max_tokens):
            return {"bundle": bundle, "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}, "model": "m"}

        providers = {"kimi": fake_down, "codex": fake_up, "claude": fake_down}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["bundle"]["batch_id"], "batch_001")

    def test_llm_client_does_not_fall_back_on_invalid_llm_response(self) -> None:
        from lead_machine import llm_client

        def fake_bad_json(prompt, *, model, max_tokens):
            raise llm_client.InvalidLlmResponse("bad json", raw_text="{nope")

        def fake_up(prompt, *, model, max_tokens):
            raise AssertionError("next provider must not be tried")

        providers = {"kimi": fake_bad_json, "codex": fake_up}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                with self.assertRaises(llm_client.InvalidLlmResponse):
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

    def test_llm_client_chain_exhaustion_lists_provider_reasons(self) -> None:
        from lead_machine import llm_client

        def fake_down(reason):
            def _fn(prompt, *, model, max_tokens):
                raise llm_client.ProviderUnavailable(reason)
            return _fn

        providers = {"kimi": fake_down("missing KIMI_API_KEY"), "codex": fake_down("quota_exhausted"), "claude": fake_down("auth fail")}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex,claude"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        message = str(caught.exception)
        self.assertIn("missing KIMI_API_KEY", message)
        self.assertIn("quota_exhausted", message)
        self.assertIn("auth fail", message)
```

(`self._manual_score` is the existing helper used by the current llm_client tests in the same test class — reuse it.)

- [ ] **Step 3: Run new tests to verify they fail**

Run: `python -m unittest tests.test_lead_machine_contracts -v 2>&1 | tail -20`
Expected: the five new tests FAIL (`ProviderUnavailable` / `_provider_chain` do not exist — ImportError/AttributeError); the six updated anthropic tests FAIL or error because `LLM_SCORING_PROVIDER_CHAIN` is not yet honored.

- [ ] **Step 4: Implement the registry and chain driver**

In `lead_machine/llm_client.py`:

1. Add after the existing constants:

```python
DEFAULT_PROVIDER_CHAIN = ("kimi", "codex", "claude")


class ProviderUnavailable(RuntimeError):
    """A scoring provider cannot serve this call (missing key, auth, quota, transport)."""
```

2. Rename the body of today's `score_batch` to `_score_batch_anthropic(prompt, *, model, max_tokens)`, with two changes: (a) missing key raises `ProviderUnavailable("Missing ANTHROPIC_API_KEY in .env")` instead of `RuntimeError`; (b) the two final `raise RuntimeError(_request_failure_message(...))` and the trailing unreachable `raise RuntimeError(...)` are wrapped so the caller sees unavailability — simplest: keep them raising `RuntimeError` and convert in the wrapper below. Add `"provider": "anthropic"` and `"model": selected_model` to the returned dict.

3. Add the registry, chain parser, and driver at the end of the file:

```python
def _anthropic_provider(prompt: str, *, model: str | None, max_tokens: int) -> Dict[str, Any]:
    try:
        return _score_batch_anthropic(prompt, model=model, max_tokens=max_tokens)
    except ProviderUnavailable:
        raise
    except InvalidLlmResponse:
        raise
    except RuntimeError as exc:
        raise ProviderUnavailable(str(exc)) from exc


_PROVIDERS = {
    "anthropic": _anthropic_provider,
}


def _provider_chain() -> List[str]:
    raw = os.getenv("LLM_SCORING_PROVIDER_CHAIN", "").strip()
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    if not names:
        names = list(DEFAULT_PROVIDER_CHAIN)
    chain: List[str] = []
    for name in names:
        if name not in _PROVIDERS:
            print(f"[WARN] Unknown LLM provider {name!r} in LLM_SCORING_PROVIDER_CHAIN; skipping", file=sys.stderr)
            continue
        if name not in chain:
            chain.append(name)
    return chain


def score_batch(prompt: str, *, model: str | None = None, max_tokens: int) -> Dict[str, Any]:
    load_dotenv()
    failures: List[str] = []
    for provider in _provider_chain():
        try:
            result = _PROVIDERS[provider](prompt, model=model, max_tokens=max_tokens)
        except InvalidLlmResponse:
            raise
        except ProviderUnavailable as exc:
            failures.append(f"{provider}: {exc}")
            print(f"[LLM FALLBACK] provider={provider} reason={exc}", file=sys.stderr)
            continue
        result.setdefault("provider", provider)
        return result
    raise RuntimeError("All LLM scoring providers unavailable: " + "; ".join(failures))
```

Also add `from typing import Callable, List` to the typing import (file already imports `Any, Dict`).

Note: `_score_batch_anthropic` keeps its own `load_dotenv()` call — harmless duplicate, keeps it callable standalone.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest tests.test_lead_machine_contracts 2>&1 | tail -5`
Expected: `OK` (all tests, including the 11 touched/added llm_client ones).

- [ ] **Step 6: Commit**

```bash
git add lead_machine/llm_client.py tests/test_lead_machine_contracts.py
git commit -m "refactor: provider registry + fallback chain driver in llm_client"
```

---

### Task 2: kimi (Moonshot API) provider

**Files:**
- Modify: `lead_machine/llm_client.py`
- Test: `tests/test_lead_machine_contracts.py`

**Interfaces:**
- Consumes: Task 1's `_PROVIDERS`, `ProviderUnavailable`, `_parse_score_bundle`, retry helpers.
- Produces:
  - `_score_batch_kimi(prompt: str, *, model: str | None, max_tokens: int) -> Dict[str, Any]`
  - `_PROVIDERS["kimi"]` registered.
  - Env: `KIMI_API_KEY` (alias `MOONSHOT_API_KEY`), `KIMI_BASE_URL` (default `https://api.moonshot.ai/v1`), `KIMI_SCORING_MODEL` (default `kimi-k3`; legacy alias `LLM_SCORING_MODEL` when `KIMI_SCORING_MODEL` unset), `KIMI_CONNECT_TIMEOUT_SECONDS` / `KIMI_READ_TIMEOUT_SECONDS` / `KIMI_MAX_RETRIES` / `KIMI_RETRY_BACKOFF_SECONDS` (same defaults as the ANTHROPIC_* ones: 15 / 180 / 2 / 2).

- [ ] **Step 1: Write the failing tests**

```python
    def test_kimi_provider_posts_chat_completion_and_parses_bundle(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi"}
        with patch.dict(os.environ, env, clear=False):
            with patch("lead_machine.llm_client.requests.post", return_value=FakeResponse()) as post:
                result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "kimi")
        self.assertEqual(result["model"], "kimi-k3")
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        url, kwargs = post.call_args
        self.assertEqual(url[0], "https://api.moonshot.ai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer kimi-key")
        self.assertEqual(kwargs["json"]["model"], "kimi-k3")
        self.assertEqual(kwargs["json"]["max_tokens"], 4096)
        self.assertEqual(kwargs["json"]["messages"][0]["content"], "score this batch")
        self.assertEqual(kwargs["timeout"], (15, 180))

    def test_kimi_provider_missing_key_is_unavailable_and_falls_back(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}

        def fake_up(prompt, *, model, max_tokens):
            return {"bundle": bundle, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model": "m"}

        env = {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_API_KEY", None)
            os.environ.pop("MOONSHOT_API_KEY", None)
            with patch.dict(llm_client._PROVIDERS, {"codex": fake_up}, clear=False):
                result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")

    def test_kimi_provider_retries_429_then_serves(self) -> None:
        import requests  # noqa: F401  (parity with existing tests)
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        success = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        class FakeResponse:
            def __init__(self, status_code: int, payload: dict | None = None) -> None:
                self.status_code = status_code
                self._payload = payload or {}
                self.text = json.dumps(payload or {"error": "rate limited"})

            def json(self) -> dict:
                return self._payload

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi", "KIMI_RETRY_BACKOFF_SECONDS": "2"}
        with patch.dict(os.environ, env, clear=False):
            with patch("lead_machine.llm_client.requests.post", side_effect=[FakeResponse(429), FakeResponse(200, success)]) as post:
                with patch("time.sleep") as sleep:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(post.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2])

    def test_kimi_provider_persistent_429_falls_back_to_next_provider(self) -> None:
        from lead_machine import llm_client

        class FakeResponse:
            status_code = 429
            text = '{"error": "rate limited"}'

            def json(self) -> dict:
                return {"error": "rate limited"}

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}

        def fake_up(prompt, *, model, max_tokens):
            return {"bundle": bundle, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model": "m"}

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi,codex", "KIMI_MAX_RETRIES": "1"}
        with patch.dict(os.environ, env, clear=False):
            with patch("lead_machine.llm_client.requests.post", return_value=FakeResponse()) as post:
                with patch("time.sleep"):
                    with patch.dict(llm_client._PROVIDERS, {"codex": fake_up}, clear=False):
                        result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")
        self.assertEqual(post.call_count, 2)

    def test_kimi_provider_uses_legacy_llm_scoring_model_alias(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi", "LLM_SCORING_MODEL": "kimi-k2.6"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.requests.post", return_value=FakeResponse()) as post:
                result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["model"], "kimi-k2.6")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], "kimi-k2.6")
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `python -m unittest tests.test_lead_machine_contracts -v 2>&1 | tail -20`
Expected: the five kimi tests FAIL (`_PROVIDERS` has no `"kimi"` → `[WARN] Unknown LLM provider` → chain empty or falls through).

- [ ] **Step 3: Implement the kimi provider**

In `lead_machine/llm_client.py`, add constants near the Anthropic ones:

```python
DEFAULT_KIMI_BASE_URL = "https://api.moonshot.ai/v1"
DEFAULT_KIMI_SCORING_MODEL = "kimi-k3"
```

Add response/usage extractors (OpenAI-style shapes differ from Anthropic):

```python
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
```

Add the provider (mirrors the Anthropic retry loop, but raises `ProviderUnavailable` on unavailability):

```python
def _score_batch_kimi(prompt: str, *, model: str | None, max_tokens: int) -> Dict[str, Any]:
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
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "model": selected_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    url = f"{base_url}/chat/completions"
    prompt_tokens_est = _estimate_prompt_tokens(prompt)

    for attempt in range(1, max_attempts + 1):
        print(
            f"[LLM REQUEST] provider=kimi attempt={attempt} model={selected_model} "
            f"prompt_tokens_est={prompt_tokens_est} max_tokens={max_tokens}"
        )
        try:
            response = requests.post(url, headers=headers, json=body, timeout=(connect_timeout, read_timeout))
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
            raise
        print(
            f"[LLM REQUEST OK] provider=kimi attempt={attempt} "
            f"input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']}"
        )
        return {"bundle": bundle, "usage": usage, "provider": "kimi", "model": selected_model}

    raise ProviderUnavailable("kimi request failed without making an attempt")
```

Register it in `_PROVIDERS` (add `"kimi": _score_batch_kimi` above `"anthropic"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_lead_machine_contracts 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add lead_machine/llm_client.py tests/test_lead_machine_contracts.py
git commit -m "feat: kimi (Moonshot API) scoring provider"
```

---

### Task 3: codex + claude CLI providers

**Files:**
- Modify: `lead_machine/llm_client.py`
- Test: `tests/test_lead_machine_contracts.py`

**Interfaces:**
- Consumes: Task 1 registry/driver; `score_batch_subscription` module (repo-root script) — specifically `sanitized_environment()`, `preflight_subscription_auth(runner, *, env)`, `run_runner_attempt(runner, *, prompt_text, model, timeout_seconds, env, use_output_schema)`, `clean_bundle_text(text)`, and its `RunnerResult` fields (`result`, `raw_text`, `usage`, `stderr_tail`).
- Produces:
  - `_normalize_cli_usage(raw: dict | None) -> Dict[str, int]`
  - `_score_batch_cli(runner: str, prompt: str, *, model: str | None, max_tokens: int) -> Dict[str, Any]`
  - `_PROVIDERS["codex"]` and `_PROVIDERS["claude"]` registered (via `functools.partial`-free lambdas or thin wrappers).
  - Env: `CODEX_SUBSCRIPTION_SCORING_MODEL`, `CLAUDE_SUBSCRIPTION_SCORING_MODEL` (already used by `score_batch_subscription.py`), `LLM_CLI_TIMEOUT_SECONDS` (default 900).

- [ ] **Step 1: Write the failing tests**

```python
    def test_codex_cli_provider_serves_bundle(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        runner_result = subs.RunnerResult(
            result="ok",
            raw_text=json.dumps(bundle),
            usage={"input_tokens": 10, "output_tokens": 5},
            stderr_tail="",
            duration_seconds=1.0,
        )

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", return_value=subs.AuthResult(True, "chatgpt_subscription", "ok")) as auth:
                with patch("score_batch_subscription.run_runner_attempt", return_value=runner_result) as attempt:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(auth.call_args.args[0], "codex")
        _, kwargs = attempt.call_args
        self.assertEqual(kwargs["prompt_text"], "score this batch")
        self.assertTrue(kwargs["use_output_schema"])

    def test_codex_cli_auth_failure_falls_back_to_claude(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        runner_result = subs.RunnerResult(result="ok", raw_text=json.dumps(bundle), usage=None, stderr_tail="", duration_seconds=1.0)

        def fake_auth(runner, *, env, process_runner=subs.run_process):
            if runner == "codex":
                return subs.AuthResult(False, "unverified", "not logged in")
            return subs.AuthResult(True, "claude_subscription:max", "ok")

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", side_effect=fake_auth):
                with patch("score_batch_subscription.run_runner_attempt", return_value=runner_result) as attempt:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "claude")
        self.assertEqual(result["usage"], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        self.assertEqual(attempt.call_args.args[0], "claude")

    def test_cli_runner_quota_result_falls_back(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        quota = subs.RunnerResult(result="quota_exhausted", raw_text="", usage=None, stderr_tail="usage limit", duration_seconds=0.5)
        ok = subs.RunnerResult(result="ok", raw_text=json.dumps(bundle), usage=None, stderr_tail="", duration_seconds=1.0)

        def fake_attempt(runner, **kwargs):
            return quota if runner == "codex" else ok

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", return_value=subs.AuthResult(True, "sub", "ok")):
                with patch("score_batch_subscription.run_runner_attempt", side_effect=fake_attempt):
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "claude")

    def test_cli_provider_invalid_json_does_not_fall_back(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bad = subs.RunnerResult(result="ok", raw_text="{not json", usage=None, stderr_tail="", duration_seconds=1.0)

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", return_value=subs.AuthResult(True, "sub", "ok")):
                with patch("score_batch_subscription.run_runner_attempt", return_value=bad):
                    with self.assertRaises(llm_client.InvalidLlmResponse):
                        llm_client.score_batch("score this batch", model=None, max_tokens=4096)
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `python -m unittest tests.test_lead_machine_contracts -v 2>&1 | tail -20`
Expected: four CLI tests FAIL (unknown providers `codex`/`claude` → chain empty → exhaustion `RuntimeError`).

- [ ] **Step 3: Implement the CLI providers**

In `lead_machine/llm_client.py`, add:

```python
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

    input_tokens = _pick("input_tokens", "prompt_tokens")
    output_tokens = _pick("output_tokens", "completion_tokens")
    total = _pick("total_tokens") or input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}


def _score_batch_cli(runner: str, prompt: str, *, model: str | None, max_tokens: int) -> Dict[str, Any]:
    import score_batch_subscription as subscription_runner

    env = subscription_runner.sanitized_environment()
    auth = subscription_runner.preflight_subscription_auth(runner, env=env)
    if not auth.ok:
        raise ProviderUnavailable(f"{runner} subscription auth not verified: {auth.detail}")

    env_key = "CODEX_SUBSCRIPTION_SCORING_MODEL" if runner == "codex" else "CLAUDE_SUBSCRIPTION_SCORING_MODEL"
    selected_model = (os.getenv(env_key) or "").strip()
    timeout_seconds = max(1, _getenv_int("LLM_CLI_TIMEOUT_SECONDS", DEFAULT_CLI_TIMEOUT_SECONDS))
    print(f"[LLM REQUEST] provider={runner} model={selected_model or 'runner-default'} via CLI subscription")
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
    return {"bundle": bundle, "usage": usage, "provider": runner, "model": selected_model or "runner-default"}
```

Register both in `_PROVIDERS`:

```python
_PROVIDERS = {
    "kimi": _score_batch_kimi,
    "codex": lambda prompt, *, model, max_tokens: _score_batch_cli("codex", prompt, model=model, max_tokens=max_tokens),
    "claude": lambda prompt, *, model, max_tokens: _score_batch_cli("claude", prompt, model=model, max_tokens=max_tokens),
    "anthropic": _anthropic_provider,
}
```

(The explicit `model=` argument is intentionally ignored for CLI providers — their model comes from `CODEX_SUBSCRIPTION_SCORING_MODEL` / `CLAUDE_SUBSCRIPTION_SCORING_MODEL`, per the spec.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_lead_machine_contracts 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add lead_machine/llm_client.py tests/test_lead_machine_contracts.py
git commit -m "feat: codex/claude CLI subscription scoring providers"
```

---

### Task 4: Config, docs, and full verification

**Files:**
- Modify: `.env.example`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md` (one-line addendum: `anthropic` kept as optional non-default provider)

**Interfaces:**
- Consumes: Tasks 1-3 env vars.
- Produces: documented configuration; green full test suite.

- [ ] **Step 1: Update `.env.example`**

Replace lines 1-6 (`ANTHROPIC_API_KEY=` through `ANTHROPIC_RETRY_BACKOFF_SECONDS=2`) with:

```
# LLM scoring provider chain: comma-separated subset/reordering of kimi,codex,claude,anthropic
LLM_SCORING_PROVIDER_CHAIN=kimi,codex,claude

# Provider 1 (default): kimi via Moonshot API
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.ai/v1
KIMI_SCORING_MODEL=kimi-k3
KIMI_CONNECT_TIMEOUT_SECONDS=15
KIMI_READ_TIMEOUT_SECONDS=180
KIMI_MAX_RETRIES=2
KIMI_RETRY_BACKOFF_SECONDS=2

# Fallback 1: codex CLI (ChatGPT subscription). Fallback 2: claude CLI (subscription).
CODEX_SUBSCRIPTION_SCORING_MODEL=
CLAUDE_SUBSCRIPTION_SCORING_MODEL=
LLM_CLI_TIMEOUT_SECONDS=900

# Optional provider: anthropic via API (add to LLM_SCORING_PROVIDER_CHAIN to enable)
ANTHROPIC_API_KEY=
ANTHROPIC_CONNECT_TIMEOUT_SECONDS=15
ANTHROPIC_READ_TIMEOUT_SECONDS=180
ANTHROPIC_MAX_RETRIES=2
ANTHROPIC_RETRY_BACKOFF_SECONDS=2
```

Keep `LLM_SCORING_MODEL=claude-sonnet-5` removed from the example (it survives only as a legacy alias) and leave the `MAX_LLM_*` cap lines untouched.

- [ ] **Step 2: Update `AGENTS.md`**

In the "Key in-flight facts" section, replace the bullet "Scoring is being automated off the manual clipboard onto the Anthropic Messages API (Sonnet 4.6, `LLM_SCORING_MODEL` in `.env`, no SDK — uses `requests`)." with:

```
- **Scoring runs through a provider chain** in `lead_machine/llm_client.py`: kimi (Moonshot API,
  `KIMI_API_KEY` / `KIMI_SCORING_MODEL`, default `kimi-k3`) → codex CLI (ChatGPT subscription) →
  claude CLI (subscription). Order via `LLM_SCORING_PROVIDER_CHAIN`; `anthropic` (Messages API,
  `ANTHROPIC_API_KEY`) remains an optional provider. Fallback happens only on provider
  unavailability — invalid output stays with the repair flow in `score_leads.py`.
```

- [ ] **Step 3: Add the spec addendum**

Append to `docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md`:

```markdown

## Addendum (implementation)

`anthropic` (the pre-existing Messages API path) is kept as a valid provider name in
`_PROVIDERS` but is not in the default chain, so existing Anthropic setups and tests can
pin `LLM_SCORING_PROVIDER_CHAIN=anthropic`.
```

- [ ] **Step 4: Run the full test suite**

Run: `python -m unittest discover -s tests 2>&1 | tail -5`
Expected: `OK` — zero failures/errors across the whole suite (including `tests.test_icp_lanes`, `tests.test_lead_machine_contracts`, and `tests.test_score_batch_subscription`, which must be untouched and still green).

- [ ] **Step 5: Commit**

```bash
git add .env.example AGENTS.md docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md
git commit -m "docs: kimi-default LLM scoring chain config + AGENTS.md update"
```

---

## Self-Review Notes

- **Spec coverage:** provider chain (Tasks 1-3), kimi API (Task 2), CLI reuse (Task 3), fallback-only-on-unavailability (Tasks 1-3 tests), legacy `LLM_SCORING_MODEL` alias (Task 2), env docs (Task 4), AGENTS.md (Task 4), full-suite run (Task 4). One deliberate deviation from the spec: `anthropic` kept as an optional provider so existing Anthropic tests/setups keep working — recorded in the Task 4 spec addendum.
- **Type consistency:** provider functions uniformly `(prompt, *, model, max_tokens) -> Dict[str, Any]`; `ProviderUnavailable` and `InvalidLlmResponse` names used identically across tasks; `_PROVIDERS` patchable dict is what the tests patch.
- **Out of scope (unchanged):** `score_leads.py` call sites (signature preserved), `score_batch_subscription.py` CLI contract, `MAX_LLM_*` cap accounting in `score_leads.py` (usage dict shape preserved).
