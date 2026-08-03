# Multi-Provider LLM Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `lead_machine/llm_client.py` score via a provider chain — kimi (Moonshot API) by default, codex CLI (ChatGPT subscription) fallback 1, claude CLI (subscription) fallback 2 — with the existing Anthropic API kept as an optional provider.

**Architecture:** `score_batch(prompt, *, model=None, max_tokens)` becomes a chain driver over a `_PROVIDERS` registry. Each provider is a function `(prompt, *, model, max_tokens) -> {"bundle", "usage", "provider", "model"}` that raises `ProviderUnavailable` when it cannot serve (missing key, auth failure, quota, timeout, transport error) and lets `InvalidLlmResponse` propagate (no fallback on output-quality problems). CLI providers reuse `score_batch_subscription.py` helpers via import, the same way `llm_client.py` already imports `import_llm_score_bundle`.

**Tech Stack:** Python 3.10+ stdlib + `requests` + `python-dotenv` (already used); stdlib `unittest` tests; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md`
(see its `## Revision 2026-08-03` section — it supersedes two statements in the original design)

## Global Constraints

- Tests are stdlib `unittest`; run `.venv/bin/python -m unittest discover -s tests` (bare `python -m unittest` discovers 0 tests here).
- No new heavy deps; HTTP via `requests` only.
- Follow the existing `_getenv_int`/`_getenv_float` + `[WARN]` pattern in `lead_machine/llm_client.py` for env config.
- Fallback only on unavailability; `InvalidLlmResponse` always propagates without trying the next provider.
- CLI subprocesses are never spawned in unit tests — patch `score_batch_subscription.run_runner_attempt` / `preflight_subscription_auth`.
- Default chain (when `LLM_SCORING_PROVIDER_CHAIN` is unset): `kimi,codex,claude`. `anthropic` is a valid but non-default provider name.
- **Line numbers in this plan are from 2026-07-24 and have drifted.** Locate every edit site by symbol
  name, not by line number, and re-read the surrounding code before editing.

## Readiness review (2026-08-03)

This plan was reviewed against the live code before implementation. Everything it reaches for in
`lead_machine/llm_client.py` and `score_batch_subscription.py` exists with the assumed signatures.
Two gaps were found and are now fixed **in this plan**, not left to the implementer:

1. `score_leads.py` resolves `llm_model = os.getenv("LLM_SCORING_MODEL") or "claude-sonnet-5"` and
   passes it as the explicit `model=` into `score_batch`, which would override the kimi model and
   make a live run POST `claude-sonnet-5` to Moonshot. Tests here all pass `model=None`, so the
   suite would be green while the pipeline was dead. → Task 4 + the first-provider-only model rule
   in Task 1.
2. `score_leads.llm_pricing()` raises `UnknownLlmModelError` on unregistered model ids and the
   caller `return 1`s before any scoring call, so a kimi model id aborts the run at cost
   estimation. → Task 4 pricing entries.

Original scope said `score_leads.py` was untouched. That is no longer true: **Task 4 is required for
Kimi to score anything.**

---

### Task 0: Preflight — confirm the Moonshot model id, endpoint, and prices

Gate, not code. Do this first; Tasks 2 and 4 hardcode its answers.

`kimi-k3` and `https://api.moonshot.ai/v1` are assumptions carried from the design doc and have
never been exercised. A wrong model id 400s (and, per Task 1's semantics, silently degrades to a
subscription provider). A guessed price silently miscalibrates `MAX_LLM_USD_PER_RUN` — which is the
exact failure the comment at `score_leads.py:278-285` was written about.

- [ ] **Step 1: Confirm `KIMI_API_KEY` is present in `.env`** (the operator adds it; never commit it).

- [ ] **Step 2: List the real model ids**

```bash
.venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv()
base = (os.getenv("KIMI_BASE_URL") or "https://api.moonshot.ai/v1").rstrip("/")
key = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")
r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=(15, 60))
print(r.status_code)
print(sorted(m.get("id") for m in (r.json().get("data") or [])))
PY
```

One call validates the model id, the base URL, and the key's region together. A
`platform.moonshot.ai` key is **not** valid against `api.moonshot.cn` and vice versa — a 401 here
means the wrong endpoint for that key, not a bad key.

- [ ] **Step 3: Record the answers below before continuing**

```
Confirmed model id:      ____________________   (replaces `kimi-k3` everywhere in Tasks 2 and 4)
Confirmed base URL:      ____________________
Input  USD / 1M tokens:  ____________________   (from the Moonshot console pricing page — do NOT estimate)
Output USD / 1M tokens:  ____________________
```

If the confirmed id is not `kimi-k3`, substitute it in `DEFAULT_KIMI_SCORING_MODEL` (Task 2), the
pricing table (Task 4), `.env.example` and `AGENTS.md` (Task 5). Do not leave `kimi-k3` in place
"as a default" if it isn't real — an unregistered id aborts the run at cost estimation.

---

### Task 1: Provider registry, chain driver, and anthropic refactor

**Files:**
- Modify: `lead_machine/llm_client.py` (whole file restructure)
- Test: `tests/test_lead_machine_contracts.py` (update the existing `test_llm_client_*` tests — they
  were at ~2345-2517 when this plan was written and are at ~2813-2985 as of 2026-08-03; find them by
  name — and add new tests after them)

**Interfaces:**
- Consumes: existing `validate_bundle_scores` import; existing `InvalidLlmResponse`, `_parse_score_bundle`, `_usage`, `_estimate_prompt_tokens`.
- Produces:
  - `class ProviderUnavailable(RuntimeError)` — provider cannot serve this call.
  - `DEFAULT_PROVIDER_CHAIN = ("kimi", "codex", "claude")`
  - `_PROVIDERS: Dict[str, Callable]` — name → provider function. After this task it contains only `"anthropic"`; later tasks add the rest.
  - `_provider_chain() -> List[str]` — parses `LLM_SCORING_PROVIDER_CHAIN`.
  - `score_batch(prompt: str, *, model: str | None = None, max_tokens: int) -> Dict[str, Any]` — chain driver; same signature as today, return dict gains `"provider"` and `"model"` keys.

- [ ] **Step 1: Update existing tests to pin the anthropic chain**

The six existing tests (`test_llm_client_posts_anthropic_message_and_parses_score_bundle`, `..._uses_configured_connect_and_read_timeout_tuple`, `..._retries_timeout_up_to_max_retries`, `..._retries_500_and_503_responses`, `..._does_not_retry_deterministic_400_response`, `..._final_retry_error_includes_timeout_settings_and_attempts`) each use `patch.dict(os.environ, {...}, clear=False)`. Add `"LLM_SCORING_PROVIDER_CHAIN": "anthropic"` to every one of those env dicts so they keep exercising the Anthropic provider directly regardless of the new default chain.

Their `assertIn` message checks (`"Anthropic API error 400"`, `"model=claude-sonnet-5"`, `"attempts=3"`, …) still hold after the refactor: the driver wraps the provider's text into `"All LLM scoring providers unavailable: anthropic: <original text>"`, so the substrings survive. Do not weaken those assertions.

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

    def test_llm_client_forwards_explicit_model_only_to_the_first_provider(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        seen: list = []

        def fake_down(prompt, *, model, max_tokens):
            seen.append(model)
            raise llm_client.ProviderUnavailable("down")

        def fake_up(prompt, *, model, max_tokens):
            seen.append(model)
            return {"bundle": bundle, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model": "m"}

        providers = {"kimi": fake_down, "anthropic": fake_up}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,anthropic"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                llm_client.score_batch("score this batch", model="kimi-k3", max_tokens=4096)

        # The caller's model belongs to the provider it was chosen for. Handing
        # "kimi-k3" to the next API provider would 400 the whole chain.
        self.assertEqual(seen, ["kimi-k3", None])

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

Run: `.venv/bin/python -m unittest tests.test_lead_machine_contracts -v 2>&1 | tail -20`
Expected: the six new tests FAIL (`ProviderUnavailable` / `_provider_chain` do not exist — ImportError/AttributeError); the six updated anthropic tests FAIL or error because `LLM_SCORING_PROVIDER_CHAIN` is not yet honored.

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
    for index, provider in enumerate(_provider_chain()):
        # An explicit model belongs to the provider it was chosen for. Handing a
        # kimi id to a fallback API provider would 400 the rest of the chain, so
        # every provider after the first resolves its own model from env.
        requested_model = model if index == 0 else None
        try:
            result = _PROVIDERS[provider](prompt, model=requested_model, max_tokens=max_tokens)
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

Run: `.venv/bin/python -m unittest tests.test_lead_machine_contracts 2>&1 | tail -5`
Expected: `OK` (all tests, including the 12 touched/added llm_client ones).

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

**Use the model id and base URL confirmed in Task 0** wherever this task writes `kimi-k3` or
`https://api.moonshot.ai/v1` — in `DEFAULT_KIMI_SCORING_MODEL`, `DEFAULT_KIMI_BASE_URL`, and the
test assertions.

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

Run: `.venv/bin/python -m unittest tests.test_lead_machine_contracts -v 2>&1 | tail -20`
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

Run: `.venv/bin/python -m unittest tests.test_lead_machine_contracts 2>&1 | tail -5`
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
  - `resolve_scoring_model() -> str` — public; the model id the chain's **first** provider will use.
    `score_leads.py` consumes it for cost estimation and logging (Task 4). Defined here because it
    needs every provider's env conventions.
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

    def test_resolve_scoring_model_follows_the_first_provider_in_the_chain(self) -> None:
        from lead_machine import llm_client

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            self.assertEqual(llm_client.resolve_scoring_model(), llm_client.DEFAULT_KIMI_SCORING_MODEL)

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,kimi"}, clear=False):
            self.assertEqual(llm_client.resolve_scoring_model(), "subscription:codex")

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "anthropic"}, clear=False):
            os.environ.pop("LLM_SCORING_MODEL", None)
            self.assertEqual(llm_client.resolve_scoring_model(), llm_client.DEFAULT_LLM_SCORING_MODEL)
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_lead_machine_contracts -v 2>&1 | tail -20`
Expected: the four CLI tests FAIL (unknown providers `codex`/`claude` → chain empty → exhaustion `RuntimeError`) and the `resolve_scoring_model` test FAILs with `AttributeError`.

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

Then add the public model resolver the pipeline prices against:

```python
def resolve_scoring_model() -> str:
    """Model id the chain's first provider will use — for cost estimation and logging.

    `score_leads.py` needs one model string up front to price the run against
    `MAX_LLM_USD_PER_RUN`. Reading `LLM_SCORING_MODEL` directly (as it used to)
    names a provider that may not be in the chain at all. Subscription runners
    have no per-token price, so they report a `subscription:<runner>` id that the
    pricing table registers at $0.
    """
    load_dotenv()
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
```

Known ambiguity, accepted: `LLM_SCORING_MODEL` is the legacy alias for *both* the kimi and the
anthropic model, so a chain of `anthropic` plus a kimi-shaped `LLM_SCORING_MODEL` resolves wrong.
Prefer the provider-specific vars (`KIMI_SCORING_MODEL`) in `.env`; Task 5 documents that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_lead_machine_contracts 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add lead_machine/llm_client.py tests/test_lead_machine_contracts.py
git commit -m "feat: codex/claude CLI subscription scoring providers"
```

---

### Task 4: Wire the pipeline to the chain — pricing + model resolution

**Required for Kimi to score anything.** Without this task, Tasks 1-3 ship a green test suite and a
dead pipeline: `score_leads.py` overrides the kimi model with `claude-sonnet-5`, and if you set
`LLM_SCORING_MODEL` to a kimi id instead, the run aborts at cost estimation because that id has no
registered price.

**Files:**
- Modify: `score_leads.py`
- Test: `tests/test_infrastructure_robustness.py` (the existing `llm_pricing` / `estimate_llm_usd` tests live here, ~lines 220-255)

**Interfaces:**
- Consumes: `llm_client.resolve_scoring_model()` (Task 3).
- Produces:
  - `LLM_PRICING_USD_PER_MILLION_TOKENS` entries for the confirmed kimi model and for
    `subscription:codex` / `subscription:claude` at `(0.0, 0.0)`.
  - `score_leads.main()` resolving its scoring model from the chain instead of from `LLM_SCORING_MODEL`.
  - A `[LLM PROVIDER]` log line per scored batch.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_infrastructure_robustness.py`, next to the existing pricing tests:

```python
    def test_every_chain_resolvable_model_has_a_registered_price(self) -> None:
        from lead_machine import llm_client

        # The run aborts at cost estimation if the chain resolves a model the
        # pricing table does not know, so every provider's model must price.
        for chain in ("kimi", "codex", "claude", "anthropic"):
            with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": chain}, clear=False):
                os.environ.pop("LLM_SCORING_MODEL", None)
                os.environ.pop("KIMI_SCORING_MODEL", None)
                model = llm_client.resolve_scoring_model()
                llm_pricing(model)  # must not raise UnknownLlmModelError

    def test_subscription_runners_price_at_zero(self) -> None:
        usage = {"input_tokens": 100_000, "output_tokens": 50_000}
        self.assertEqual(estimate_llm_usd(usage, model="subscription:codex"), 0.0)
        self.assertEqual(estimate_llm_usd(usage, model="subscription:claude"), 0.0)
```

(Add `from unittest.mock import patch` / `import os` to the module imports if absent.)

- [ ] **Step 2: Run new tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_infrastructure_robustness -v 2>&1 | tail -20`
Expected: both FAIL with `UnknownLlmModelError` — no kimi or `subscription:*` entry exists yet.

- [ ] **Step 3: Register the prices**

In `score_leads.py`, add to `LLM_PRICING_USD_PER_MILLION_TOKENS` (keep the existing explanatory
comment above it intact — it is the reason this table refuses unknown models):

```python
    # Moonshot list price, confirmed in Task 0 against the Moonshot console.
    "<confirmed kimi model id>": (<input_usd_per_1m>, <output_usd_per_1m>),
    # Subscription runners are $0 marginal; they still count against
    # MAX_LLM_CALLS_PER_RUN and MAX_LLM_TOKENS_PER_RUN.
    "subscription:codex": (0.0, 0.0),
    "subscription:claude": (0.0, 0.0),
```

Use the real numbers from Task 0. Do not estimate — a wrong price here silently miscalibrates
`MAX_LLM_USD_PER_RUN`, `--dry-run-llm-cost` and the spend-confirm threshold, which is exactly the
bug the comment above this table records.

- [ ] **Step 4: Resolve the scoring model from the chain**

In `score_leads.main()`, replace:

```python
        llm_model = (os.getenv("LLM_SCORING_MODEL") or DEFAULT_LLM_SCORING_MODEL).strip()
```

with:

```python
        llm_model = llm_client.resolve_scoring_model()
```

`score_leads.py` already imports `llm_client` (`from lead_machine import llm_client`). `llm_model`
keeps flowing unchanged into `estimate_api_llm_cost(...)`, the `[LLM COST] model=` line and
`score_records_api_run(model=llm_model)` — and from there into `score_batch(model=...)`, where it is
now the *correct* provider's model, which is what makes the kimi request valid.

Leave `DEFAULT_LLM_SCORING_MODEL` in place; `resolve_scoring_model()` still uses it for the
anthropic branch.

- [ ] **Step 5: Surface which provider actually served**

Silent fallback is the operational risk of this whole change: a bad kimi model id or an exhausted
quota degrades to the codex/claude CLI and the run still succeeds — spending subscription capacity
while you believe you are spending Kimi credits. Make it visible.

In `score_records_api_run`, after each successful `_score_batch_with_debug(...)` call, print:

```python
            print(f"[LLM PROVIDER] provider={result.get('provider', 'unknown')} model={result.get('model', llm_model_display)}")
```

(name the local however it reads best; `result` already carries `provider`/`model` from Task 1.)

Also make the dry-run line provider-neutral: `"[DRY-RUN] No Anthropic call made."` → `"[DRY-RUN] No LLM API call made."` (no test asserts this string).

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_infrastructure_robustness tests.test_lead_machine_contracts 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add score_leads.py tests/test_infrastructure_robustness.py
git commit -m "feat: price and resolve the scoring model from the provider chain"
```

---

### Task 5: Config, docs, and full verification

**Files:**
- Modify: `.env.example`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md` (one-line addendum: `anthropic` kept as optional non-default provider)

**Interfaces:**
- Consumes: Tasks 1-4 env vars.
- Produces: documented configuration; green full test suite.

- [ ] **Step 1: Update `.env.example`**

Replace the leading `ANTHROPIC_*` block (`ANTHROPIC_API_KEY=` through
`ANTHROPIC_RETRY_BACKOFF_SECONDS=2` — it was lines 1-6 in 2026-07, verify before editing) with:

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

Use the model id and base URL confirmed in Task 0, not literally `kimi-k3` / `api.moonshot.ai`
unless Task 0 confirmed them.

Keep `LLM_SCORING_MODEL=claude-sonnet-5` removed from the example and add a comment that it is a
legacy alias resolved for whichever provider leads the chain — set `KIMI_SCORING_MODEL` instead, so
a stale `LLM_SCORING_MODEL` in a real `.env` can't name a model that belongs to a different
provider. Leave the `MAX_LLM_*` cap lines untouched.

Note for the operator (not a file edit): `.env` reads are blocked by this repo's permission hooks,
so the implementing agent cannot verify `KIMI_API_KEY` by reading it. Confirm via Task 0's probe.

- [ ] **Step 2: Update `AGENTS.md`**

In the "Key in-flight facts" section, replace the bullet "Scoring is being automated off the manual clipboard onto the Anthropic Messages API (Sonnet 4.6, `LLM_SCORING_MODEL` in `.env`, no SDK — uses `requests`)." with:

```
- **Scoring runs through a provider chain** in `lead_machine/llm_client.py`: kimi (Moonshot API,
  `KIMI_API_KEY` / `KIMI_SCORING_MODEL`, default `kimi-k3`) → codex CLI (ChatGPT subscription) →
  claude CLI (subscription). Order via `LLM_SCORING_PROVIDER_CHAIN`; `anthropic` (Messages API,
  `ANTHROPIC_API_KEY`) remains an optional provider. Fallback happens only on provider
  unavailability — invalid output stays with the repair flow in `score_leads.py`. Every model the
  chain can resolve must have a price in `score_leads.LLM_PRICING_USD_PER_MILLION_TOKENS` or the
  run aborts at cost estimation; `[LLM PROVIDER]` in the run log says who actually served.
```

- [ ] **Step 3: Add the spec addendum**

Append to `docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md`:

```markdown

## Addendum (implementation)

`anthropic` (the pre-existing Messages API path) is kept as a valid provider name in
`_PROVIDERS` but is not in the default chain, so existing Anthropic setups and tests can
pin `LLM_SCORING_PROVIDER_CHAIN=anthropic`.
```

(The design file's `## Revision 2026-08-03` section already records the `score_leads.py` scope
change and the first-provider-only model rule; do not duplicate them here.)

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m unittest discover -s tests 2>&1 | tail -5`
Expected: `OK` — zero failures/errors across the whole suite (~960 tests, ~22s), including
`tests.test_icp_lanes`, `tests.test_lead_machine_contracts`, `tests.test_infrastructure_robustness`
and `tests.test_score_batch_subscription`.

- [ ] **Step 5: Commit**

```bash
git add .env.example AGENTS.md docs/superpowers/specs/2026-07-24-multi-provider-llm-scoring-design.md
git commit -m "docs: kimi-default LLM scoring chain config + AGENTS.md update"
```

---

### Task 6: One-batch live smoke test before any fleet run

Gate, not code. A green suite does not prove the Moonshot call works — every unit test mocks
`requests.post`. Do not report the chain as working, and do not fire the remaining batches, until
this passes.

- [ ] **Step 1: Score exactly one batch with the chain pinned to kimi**

Pin `LLM_SCORING_PROVIDER_CHAIN=kimi` for this run (env var on the command, not a `.env` edit) so a
misconfigured kimi cannot silently degrade to the codex/claude subscription. Run the smallest
available batch through the normal path (`run-lead-batch` / `score_leads.py`), with
`--dry-run-llm-cost` first to confirm the projected USD is computed off the kimi price.

- [ ] **Step 2: Confirm from the run log, not from the exit code**

- `[LLM COST] model=` shows the confirmed kimi model id.
- `[LLM PROVIDER] provider=kimi` appears for the batch.
- The scored bundle validates and lands (no `_invalid_response_*` debug artifacts).
- Moonshot's console shows the spend.

- [ ] **Step 3: Only then unpin the chain and run the remaining batches.**

---

## Self-Review Notes

- **Spec coverage:** provider chain (Tasks 1-3), kimi API (Task 2), CLI reuse (Task 3), fallback-only-on-unavailability (Tasks 1-3 tests), legacy `LLM_SCORING_MODEL` alias (Task 2), pipeline pricing + model resolution (Task 4), env docs and AGENTS.md (Task 5), full-suite run (Task 5), live smoke (Task 6). Deliberate deviations from the original design, both recorded in the spec's `## Revision 2026-08-03`: `anthropic` kept as an optional provider, and an explicit `model=` is forwarded only to the chain's first provider rather than to whichever provider serves.
- **Type consistency:** provider functions uniformly `(prompt, *, model, max_tokens) -> Dict[str, Any]`; `ProviderUnavailable` and `InvalidLlmResponse` names used identically across tasks; `_PROVIDERS` patchable dict is what the tests patch.
- **Verified against the live code (2026-08-03):** every symbol Tasks 1-3 consume exists with the assumed signature — `llm_client._getenv_int/_getenv_float/_parse_score_bundle/_estimate_prompt_tokens/_seconds_for_log/_transient_request_reason/TRANSIENT_HTTP_STATUSES/DEFAULT_ANTHROPIC_*`, and `score_batch_subscription.sanitized_environment/preflight_subscription_auth/run_runner_attempt/clean_bundle_text/RunnerResult/AuthResult`. No import cycle (`score_batch_subscription` imports only `import_llm_score_bundle`); repo root is already on `sys.path` via `llm_client`'s existing top-level import. Extra `provider`/`model` keys are inert to `_score_batch_with_debug`, which reads only `bundle`/`usage`/`call_count`/`debug_notes`.
- **Out of scope (unchanged):** `score_batch_subscription.py`'s CLI contract; `MAX_LLM_*` cap accounting in `score_leads.py` (usage dict shape preserved); the scoring prompt, score schema, sendability gate, and repair flow. `score_leads.py` is **no longer** out of scope — see Task 4.
- **Known limitation, accepted:** a non-transient kimi 4xx (bad model id, quota, billing) is classified as unavailability and degrades to the next provider, so a misconfiguration reads as success while spending subscription capacity. Mitigated by the `[LLM PROVIDER]` line (Task 4) and the pinned-chain smoke test (Task 6), not by the code.
