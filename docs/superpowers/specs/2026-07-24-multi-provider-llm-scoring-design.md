# Multi-provider LLM scoring — design

Date: 2026-07-24
Status: approved (design), pre-implementation
Revised: 2026-08-03 — see `## Revision 2026-08-03` at the end; it supersedes two statements below.

## Goal

Make the API-based scoring path (`score_leads.py` → `lead_machine/llm_client.py`)
provider-agnostic with a fallback chain:

1. **kimi** — Moonshot API (`KIMI_API_KEY`), default provider.
2. **codex** — Codex CLI, ChatGPT subscription (fallback 1).
3. **claude** — Claude CLI, subscription (fallback 2).

## Non-goals

- No change to `score_batch_subscription.py`'s CLI contract (it keeps working standalone).
- No change to the scoring prompt, score schema, sendability gate, or repair flow semantics.
- No autonomous sending or other loop work.

## Architecture

All chain logic lives in `lead_machine/llm_client.py`. The public entry point keeps its
current signature:

```python
score_batch(prompt: str, *, model: str | None = None, max_tokens: int) -> Dict[str, Any]
```

It becomes a chain driver: resolve the provider order, try each provider in turn, and
return the first successful result as:

```python
{"bundle": <validated dict>, "usage": {...}, "provider": "kimi", "model": "<model used>"}
```

`provider`/`model` are additive keys; existing callers read only `bundle` and `usage`.

### Providers

- **kimi** — OpenAI-compatible `POST {KIMI_BASE_URL}/chat/completions`,
  `Authorization: Bearer $KIMI_API_KEY`. `MOONSHOT_API_KEY` accepted as an alias for
  `KIMI_API_KEY`. Connect/read timeouts, max retries, and exponential backoff follow the
  existing `getenv_int`/`getenv_float` + `[WARN]` pattern, with `KIMI_*`-prefixed env vars
  and the same defaults as the current Anthropic settings (15s connect, 180s read,
  2 retries, 2s backoff). Transient HTTP statuses: same `{429, 500, 502, 503, 504}` set.
- **codex** — subprocess via `score_batch_subscription.build_codex_command` +
  `run_runner_attempt`, reusing `preflight_subscription_auth` for the ChatGPT-login check.
- **claude** — subprocess via `score_batch_subscription.build_claude_command` +
  `run_runner_attempt`, reusing `preflight_subscription_auth` for the subscription check.

The CLI wrappers import from `score_batch_subscription` (repo-root script) the same way
`llm_client.py` already imports `validate_bundle_scores` from `import_llm_score_bundle`.
No subprocess/parsing logic is duplicated.

### Fallback semantics

Move to the next provider **only on unavailability**:

- kimi: missing API key, timeout, connection error, transient HTTP status after retries,
  any 4xx (auth/quota/billing).
- codex/claude: auth not verified, `quota_exhausted`, `timeout`, `runner_error`.

A successful HTTP response whose body is invalid JSON or fails schema validation
(`InvalidLlmResponse`) does **not** fall through to the next provider. Output-quality
problems stay with the existing repair flow in `score_leads._score_batch_with_debug`.
Provider-hopping is strictly about availability, never about output quality.

If every provider in the chain is unavailable, `score_batch` raises `RuntimeError`
listing each provider and why it was skipped/failed.

### Configuration (`.env`)

```
KIMI_API_KEY=                       # or MOONSHOT_API_KEY
KIMI_BASE_URL=https://api.moonshot.ai/v1
KIMI_SCORING_MODEL=kimi-k3
LLM_SCORING_PROVIDER_CHAIN=kimi,codex,claude
CODEX_SUBSCRIPTION_SCORING_MODEL=   # optional codex CLI model override
CLAUDE_SUBSCRIPTION_SCORING_MODEL=  # optional claude CLI model override
```

- `LLM_SCORING_PROVIDER_CHAIN` is a comma-separated subset/reordering of
  `kimi,codex,claude`; unknown names are warned about and skipped.
- `LLM_SCORING_MODEL` becomes a legacy alias for `KIMI_SCORING_MODEL` (used only if
  `KIMI_SCORING_MODEL` is unset), so existing setups keep working.
- The explicit `model=` argument to `score_batch`, when given, overrides the kimi model
  (the chain's first API provider); CLI providers use their env overrides.

### Cost / cap accounting

- kimi usage counts against `MAX_LLM_CALLS_PER_RUN` / `MAX_LLM_TOKENS_PER_RUN` /
  `MAX_LLM_USD_PER_RUN` exactly as Anthropic usage does today, including usage from
  failed-but-paid attempts (`InvalidLlmResponse.usage`).
- CLI providers are subscription-auth ($0 marginal); their usage dicts are normalized to
  `{input_tokens, output_tokens, total_tokens}` when the CLI reports token counts,
  otherwise zeros — call count still increments.
- Log lines keep the existing `[LLM REQUEST]` / `[LLM RETRY]` / `[LLM REQUEST OK]`
  shape, prefixed with the provider name; a `[LLM FALLBACK] provider=X reason=...` line
  is emitted whenever the chain advances.

## Error handling

- Per-provider retry behavior is unchanged (kimi mirrors the current Anthropic retry
  loop; CLI providers keep the subscription runner's classification).
- Chain exhaustion is a single `RuntimeError` with per-provider reasons, so
  `score_leads.py` logging and `[STOP]` cap behavior stay intact.

## Testing

Extend `tests/test_lead_machine_contracts.py` next to the existing `llm_client` tests:

- kimi request shape (URL, auth header, body) and response parsing.
- fallback on: missing `KIMI_API_KEY`, kimi 429-after-retries, kimi timeout, codex auth
  failure, codex quota.
- no fallback on `InvalidLlmResponse` (invalid JSON from a 200 response).
- CLI providers invoked via an injected `process_runner` (no real subprocesses).
- `LLM_SCORING_PROVIDER_CHAIN` reordering/subsetting honored; unknown names skipped.
- `LLM_SCORING_MODEL` legacy alias still selects the kimi model.

Run `python -m unittest discover -s tests` (must pass in full).

## Docs

- Update `.env.example` with the new variables.
- Update `AGENTS.md`: the "Scoring is being automated ... (Sonnet 4.6,
  `LLM_SCORING_MODEL` ...)" line reflects the kimi-default chain after this change.

## Revision 2026-08-03

Found during a pre-implementation review of this design against the live code. Two statements above
are wrong as written; the plan
(`docs/superpowers/plans/2026-07-24-multi-provider-llm-scoring.md`) implements the corrections.

**1. `score_leads.py` is in scope.** The non-goals implied the pipeline was untouched. It isn't:

- `score_leads.main()` resolves `llm_model = os.getenv("LLM_SCORING_MODEL") or "claude-sonnet-5"` and
  passes it as the explicit `model=` into `score_batch`. Under the rule below ("the explicit `model=`
  argument overrides the kimi model"), a live run would POST `claude-sonnet-5` to Moonshot and 400.
  `main()` must resolve its model from the chain instead — `llm_client.resolve_scoring_model()`.
- `score_leads.llm_pricing()` raises `UnknownLlmModelError` for unregistered ids and the caller
  `return 1`s before any scoring call, so the kimi model must be registered in
  `LLM_PRICING_USD_PER_MILLION_TOKENS`, alongside `subscription:codex` / `subscription:claude` at
  `(0.0, 0.0)` for the CLI providers. Prices come from the Moonshot console, never from an estimate:
  a wrong price silently miscalibrates `MAX_LLM_USD_PER_RUN` and `--dry-run-llm-cost`.

**2. An explicit `model=` is forwarded only to the chain's first provider.** The original rule
("the explicit `model=` argument, when given, overrides the kimi model") breaks the moment the chain
advances between two API providers — the second would receive a model id belonging to the first.
`score_batch` now passes the caller's model to the first provider only; every provider after it
resolves its own model from env.

**Also unverified at design time:** `kimi-k3` and `https://api.moonshot.ai/v1` were assumed, never
exercised. Confirm both against `GET {KIMI_BASE_URL}/models` before implementing (plan Task 0);
a `platform.moonshot.ai` key is not valid against `api.moonshot.cn`.

**Accepted limitation:** a non-transient kimi 4xx (bad model, quota, billing) is classified as
unavailability, so a misconfigured kimi degrades to the subscription CLIs and the run still
succeeds — spending subscription capacity instead of Kimi credits. The mitigations are operational,
not structural: a `[LLM PROVIDER]` line in the run log, and a first live batch run with
`LLM_SCORING_PROVIDER_CHAIN=kimi` pinned.

## Addendum (implementation)

`anthropic` (the pre-existing Messages API path) is kept as a valid provider name in
`_PROVIDERS` but is not in the default chain, so existing Anthropic setups and tests can
pin `LLM_SCORING_PROVIDER_CHAIN=anthropic`.
