#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from import_llm_score_bundle import import_score_bundle, validate_score_file


ROOT = Path(__file__).resolve().parent
DEFAULT_SCHEMA = ROOT / "data" / "manual_score_bundle.schema.json"
DEFAULT_LEDGER = ROOT / "output" / "subscription_scoring_ledger.jsonl"
RECEIPT_SCHEMA_VERSION = 1
AUTH_TIMEOUT_SECONDS = 30
MAX_STDERR_TAIL = 4000
QUOTA_TERMS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "status 429",
    "error 429",
    "quota",
    "plan limit",
    "credit balance",
    "insufficient_quota",
)
SANITIZED_ENV_KEYS = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
}


@dataclass(frozen=True)
class BatchJob:
    batch_id: str
    prompt_path: Path
    expected_files_path: Path
    expected_files: tuple[str, ...]
    response_path: Path
    receipt_path: Path


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    method: str
    detail: str


@dataclass(frozen=True)
class RunnerResult:
    result: str
    raw_text: str
    usage: dict[str, Any] | None
    stderr_tail: str
    duration_seconds: float


class QueueLockedError(RuntimeError):
    pass


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _absolute(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def sanitized_environment(source: dict[str, str] | os._Environ[str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in SANITIZED_ENV_KEYS:
        env.pop(key, None)
    return env


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_ledger(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short ledger write: {written}/{len(line)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def queue_lock(queue_dir: Path) -> Iterator[None]:
    lock_path = queue_dir / ".subscription_scoring.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise QueueLockedError(f"another subscription scoring run holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_process(
    cmd: Sequence[str],
    *,
    input_text: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> ProcessResult:
    started = time.monotonic()
    process = subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
        return ProcessResult(
            returncode=int(process.returncode or 0),
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return ProcessResult(
            returncode=int(process.returncode or -1),
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )


ProcessRunner = Callable[..., ProcessResult]


def _stderr_tail(text: str) -> str:
    return text[-MAX_STDERR_TAIL:]


def _contains_quota_error(*parts: str) -> bool:
    combined = "\n".join(part for part in parts if part).lower()
    return any(term in combined for term in QUOTA_TERMS)


def preflight_subscription_auth(
    runner: str,
    *,
    env: dict[str, str],
    process_runner: ProcessRunner = run_process,
) -> AuthResult:
    if runner == "codex":
        cmd = ["codex", "login", "status", "-c", 'forced_login_method="chatgpt"']
        try:
            result = process_runner(
                cmd,
                input_text="",
                cwd=Path(tempfile.gettempdir()),
                env=env,
                timeout_seconds=AUTH_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            return AuthResult(False, "unverified", str(exc))
        combined = f"{result.stdout}\n{result.stderr}"
        if not result.timed_out and result.returncode == 0 and "logged in using chatgpt" in combined.lower():
            return AuthResult(True, "chatgpt_subscription", "Codex is logged in using ChatGPT")
        return AuthResult(False, "unverified", _stderr_tail(combined.strip()) or "Codex ChatGPT login not verified")

    cmd = ["claude", "auth", "status", "--json"]
    try:
        result = process_runner(
            cmd,
            input_text="",
            cwd=Path(tempfile.gettempdir()),
            env=env,
            timeout_seconds=AUTH_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        return AuthResult(False, "unverified", str(exc))
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return AuthResult(False, "unverified", _stderr_tail(f"{result.stdout}\n{result.stderr}".strip()))
    subscription = str(payload.get("subscriptionType") or "").strip()
    ok = (
        not result.timed_out
        and result.returncode == 0
        and payload.get("loggedIn") is True
        and payload.get("authMethod") == "claude.ai"
        and payload.get("apiProvider") == "firstParty"
        and bool(subscription)
    )
    if ok:
        return AuthResult(True, f"claude_subscription:{subscription}", "Claude first-party subscription verified")
    return AuthResult(False, "unverified", "Claude first-party subscription login not verified")


def discover_batches(queue_dir: Path, batch_ids: Sequence[str], scores_dir: Path) -> list[BatchJob]:
    batches_dir = queue_dir / "batches"
    if not batches_dir.is_dir():
        raise ValueError(f"batch directory not found: {batches_dir}")
    if batch_ids:
        ids = list(dict.fromkeys(batch_ids))
    else:
        ids = [path.stem for path in sorted(batches_dir.glob("batch_*.md"))]
    if not ids:
        raise ValueError(f"no batch_*.md prompts found in {batches_dir}")

    jobs: list[BatchJob] = []
    for batch_id in ids:
        if not re.fullmatch(r"batch_[A-Za-z0-9_-]+", batch_id):
            raise ValueError(f"unsafe batch id: {batch_id}")
        prompt_path = batches_dir / f"{batch_id}.md"
        expected_path = batches_dir / f"{batch_id}_expected_files.txt"
        if not prompt_path.is_file():
            raise ValueError(f"batch prompt not found: {prompt_path}")
        if not expected_path.is_file():
            raise ValueError(f"expected-files manifest not found: {expected_path}")
        expected: list[str] = []
        for raw in expected_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                expected.append(validate_score_file(raw))
        if not expected:
            raise ValueError(f"expected-files manifest is empty: {expected_path}")
        if len(expected) != len(set(expected)):
            raise ValueError(f"expected-files manifest contains duplicates: {expected_path}")
        jobs.append(
            BatchJob(
                batch_id=batch_id,
                prompt_path=prompt_path,
                expected_files_path=expected_path,
                expected_files=tuple(expected),
                response_path=batches_dir / f"{batch_id}_response.json",
                receipt_path=scores_dir / ".subscription_receipts" / f"{batch_id}.json",
            )
        )
    return jobs


CODEX_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def resolve_runner_effort(runner: str) -> str:
    """Reasoning effort for a subscription runner, or "" for the runner default.

    An unrecognized level is dropped rather than passed through: both CLIs reject
    it, and a rejected fallback tier is a silently dead link in the provider
    chain rather than a loud config error.
    """
    if runner == "codex":
        raw = os.getenv("CODEX_REASONING_EFFORT", "").strip().lower()
        levels = CODEX_EFFORT_LEVELS
    else:
        raw = os.getenv("CLAUDE_SUBSCRIPTION_EFFORT", "").strip().lower()
        levels = CLAUDE_EFFORT_LEVELS
    if not raw:
        return ""
    if raw not in levels:
        print(
            f"[WARN] Ignoring unsupported {runner} effort {raw!r}; expected one of {', '.join(levels)}",
            file=sys.stderr,
        )
        return ""
    return raw


def build_codex_command(
    *,
    cwd: Path,
    last_message_path: Path,
    model: str,
    use_output_schema: bool,
    reasoning_effort: str = "",
) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'approval_policy="never"',
        "-c",
        "features.shell_tool=false",
        "-c",
        "features.apps=false",
        "-c",
        "features.multi_agent=false",
        "-c",
        'web_search="disabled"',
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--json",
        "-C",
        str(cwd),
    ]
    if use_output_schema:
        cmd.extend(["--output-schema", str(DEFAULT_SCHEMA.resolve())])
    cmd.extend(["--output-last-message", str(last_message_path.resolve())])
    if model:
        cmd.extend(["--model", model])
    if reasoning_effort:
        # codex exec has no --effort flag; effort is a config key, and
        # --strict-config above accepts it.
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    cmd.append("-")
    return cmd


def build_claude_command(*, model: str, reasoning_effort: str = "") -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--safe-mode",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    if model:
        cmd.extend(["--model", model])
    if reasoning_effort:
        cmd.extend(["--effort", reasoning_effort])
    return cmd


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def parse_codex_usage(stdout: str) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in _walk_dicts(event):
            usage = item.get("usage")
            if isinstance(usage, dict) and any("token" in str(key).lower() for key in usage):
                found = usage
    return found


def _codex_event_error(stdout: str) -> str:
    errors: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "").lower()
        if event_type == "error" or event_type.endswith(".error") or event_type.endswith(".failed"):
            errors.append(json.dumps(event, ensure_ascii=False))
    return "\n".join(errors)


def _codex_message_from_events(stdout: str) -> str:
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in _walk_dicts(event):
            if str(item.get("type") or "") in {"agent_message", "assistant_message"}:
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    messages.append(text)
    return messages[-1] if messages else ""


def _claude_envelope(stdout: str) -> tuple[str, dict[str, Any] | None, bool, str]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return "", None, True, f"invalid Claude JSON envelope: {exc}"
    if not isinstance(payload, dict):
        return "", None, True, "Claude JSON envelope must be an object"
    raw = payload.get("result")
    if isinstance(raw, dict):
        raw_text = json.dumps(raw, ensure_ascii=False)
    else:
        raw_text = str(raw or "")
    usage = payload.get("usage") or payload.get("modelUsage")
    return raw_text, usage if isinstance(usage, dict) else None, bool(payload.get("is_error")), str(payload.get("error") or "")


def run_runner_attempt(
    runner: str,
    *,
    prompt_text: str,
    model: str,
    timeout_seconds: int,
    env: dict[str, str],
    use_output_schema: bool,
    process_runner: ProcessRunner = run_process,
) -> RunnerResult:
    with tempfile.TemporaryDirectory(prefix="subscription-score-runner-") as raw_dir:
        temp_dir = Path(raw_dir)
        last_message_path = temp_dir / "last_message.txt"
        reasoning_effort = resolve_runner_effort(runner)
        cmd = (
            build_codex_command(
                cwd=temp_dir,
                last_message_path=last_message_path,
                model=model,
                use_output_schema=use_output_schema,
                reasoning_effort=reasoning_effort,
            )
            if runner == "codex"
            else build_claude_command(model=model, reasoning_effort=reasoning_effort)
        )
        try:
            process = process_runner(
                cmd,
                input_text=prompt_text,
                cwd=temp_dir,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        except OSError as exc:
            return RunnerResult("runner_error", "", None, str(exc), 0.0)

        if runner == "codex":
            raw_text = last_message_path.read_text(encoding="utf-8") if last_message_path.exists() else _codex_message_from_events(process.stdout)
            usage = parse_codex_usage(process.stdout)
            structured_error = _codex_event_error(process.stdout)
        else:
            raw_text, usage, envelope_error, envelope_detail = _claude_envelope(process.stdout)
            structured_error = (envelope_detail or raw_text or "Claude reported is_error") if envelope_error else ""

        combined_error = f"{process.stderr}\n{structured_error}"
        # Treat quota text as authoritative only when it comes from a runner
        # error channel. A valid score bundle may legitimately mention a site
        # crawl being rate-limited; scanning the model's final JSON would turn
        # that lead evidence into a false subscription hard stop.
        if _contains_quota_error(process.stderr, structured_error):
            result = "quota_exhausted"
        elif process.timed_out:
            result = "timeout"
        elif process.returncode != 0 or structured_error:
            result = "runner_error"
        else:
            result = "ok"
        return RunnerResult(
            result=result,
            raw_text=raw_text,
            usage=usage,
            stderr_tail=_stderr_tail(combined_error.strip()),
            duration_seconds=process.duration_seconds,
        )


def clean_bundle_text(text: str) -> str:
    cleaned = text.lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return fenced.group(1).strip() if fenced else cleaned


def validate_bundle_identity(bundle: Any, job: BatchJob) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be one JSON object")
    if bundle.get("batch_id") != job.batch_id:
        raise ValueError(f"bundle batch_id must be {job.batch_id}")
    scores = bundle.get("scores")
    if not isinstance(scores, list) or not scores:
        raise ValueError("bundle scores must be a non-empty array")
    returned: list[str] = []
    for index, item in enumerate(scores):
        if not isinstance(item, dict):
            raise ValueError(f"scores[{index}] must be an object")
        returned.append(validate_score_file(item.get("score_file")))
    if len(returned) != len(set(returned)):
        raise ValueError("bundle contains duplicate score_file values")
    expected_set = set(job.expected_files)
    returned_set = set(returned)
    if returned_set != expected_set:
        missing = sorted(expected_set - returned_set)
        extra = sorted(returned_set - expected_set)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError("bundle score_file set mismatch: " + " ".join(details))
    if len(returned) != len(job.expected_files):
        raise ValueError("bundle score count does not match expected lead count")
    return bundle


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def receipt_matches(job: BatchJob, scores_dir: Path) -> tuple[bool, str]:
    receipt = _load_receipt(job.receipt_path)
    existing = [name for name in job.expected_files if (scores_dir / name).is_file()]
    if receipt is None:
        return False, "existing scores have no valid subscription receipt" if existing else "no receipt"
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION or receipt.get("batch_id") != job.batch_id:
        return False, "receipt identity/version mismatch"
    if receipt.get("prompt_sha256") != _sha256_file(job.prompt_path):
        return False, "prompt hash changed"
    if receipt.get("expected_files_sha256") != _sha256_file(job.expected_files_path):
        return False, "expected-files hash changed"
    if receipt.get("expected_files") != list(job.expected_files):
        return False, "expected-files list changed"
    score_hashes = receipt.get("score_sha256")
    if not isinstance(score_hashes, dict) or set(score_hashes) != set(job.expected_files):
        return False, "receipt score hash set mismatch"
    for filename in job.expected_files:
        path = scores_dir / filename
        if not path.is_file() or score_hashes.get(filename) != _sha256_file(path):
            return False, f"score hash mismatch: {filename}"
    return True, "receipt and score hashes match"


def _attempt_record(number: int, result: RunnerResult) -> dict[str, Any]:
    return {
        "attempt": number,
        "result": result.result,
        "duration_seconds": round(result.duration_seconds, 3),
        "usage": result.usage,
        "stderr_tail": result.stderr_tail,
    }


def _failure_artifact(job: BatchJob, attempt: int, result: RunnerResult) -> None:
    raw = result.raw_text or result.stderr_tail
    _atomic_write_text(job.prompt_path.parent / f"{job.batch_id}_attempt_{attempt}_raw.txt", raw.rstrip() + "\n")


def _promote_validated_bundle(
    bundle: dict[str, Any],
    *,
    job: BatchJob,
    scores_dir: Path,
    runner: str,
    model: str,
    auth_method: str,
) -> int:
    scores_dir.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    with tempfile.TemporaryDirectory(prefix=f".{job.batch_id}-stage-", dir=str(scores_dir.parent)) as raw_stage:
        stage = Path(raw_stage)
        staged_response = stage / "response.json"
        staged_scores = stage / "scores"
        staged_response.write_text(canonical, encoding="utf-8")
        imported = import_score_bundle(staged_response, staged_scores)
        if imported.scores_imported != len(job.expected_files) or imported.files_written != len(job.expected_files):
            raise ValueError("imported score count does not match expected lead count")
        for filename in job.expected_files:
            if not (staged_scores / filename).is_file():
                raise ValueError(f"importer did not stage expected score: {filename}")

        scores_dir.mkdir(parents=True, exist_ok=True)
        for filename in job.expected_files:
            os.replace(staged_scores / filename, scores_dir / filename)
        _atomic_write_text(job.response_path, canonical)
        score_hashes = {filename: _sha256_file(scores_dir / filename) for filename in job.expected_files}
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "batch_id": job.batch_id,
            "prompt_sha256": _sha256_file(job.prompt_path),
            "expected_files_sha256": _sha256_file(job.expected_files_path),
            "expected_files": list(job.expected_files),
            "runner": runner,
            "model": model or "runner-default",
            "auth_method": auth_method,
            "response_sha256": _sha256_bytes(canonical.encode("utf-8")),
            "score_sha256": score_hashes,
            "imported_at": _timestamp(),
        }
        _atomic_write_json(job.receipt_path, receipt)
    return len(job.expected_files)


def score_one_batch(
    job: BatchJob,
    *,
    runner: str,
    model: str,
    scores_dir: Path,
    timeout_seconds: int,
    force: bool,
    auth_method: str,
    env: dict[str, str],
    use_output_schema: bool,
    process_runner: ProcessRunner = run_process,
) -> dict[str, Any]:
    started = time.monotonic()
    matched, receipt_reason = receipt_matches(job, scores_dir)
    existing = any((scores_dir / filename).exists() for filename in job.expected_files) or job.receipt_path.exists()
    base = {
        "event": "batch_result",
        "timestamp": _timestamp(),
        "queue_dir": str(job.prompt_path.parent.parent),
        "batch_id": job.batch_id,
        "runner": runner,
        "model": model or "runner-default",
        "auth_method": auth_method,
        "lead_count": len(job.expected_files),
        "prompt_sha256": _sha256_file(job.prompt_path),
        "expected_files_sha256": _sha256_file(job.expected_files_path),
        "cost_basis": "subscription_auth",
        "billing_guarantee": "account-setting-dependent",
    }
    if matched and not force:
        return {
            **base,
            "attempts": [],
            "duration_seconds": round(time.monotonic() - started, 3),
            "validation_result": "skipped_receipt_match",
            "scores_imported": 0,
            "receipt_path": str(job.receipt_path),
        }
    if existing and not force:
        return {
            **base,
            "attempts": [],
            "duration_seconds": round(time.monotonic() - started, 3),
            "validation_result": "stale_existing",
            "failure": receipt_reason,
            "scores_imported": 0,
            "receipt_path": str(job.receipt_path),
        }

    attempts: list[dict[str, Any]] = []
    for attempt in (1, 2):
        result = run_runner_attempt(
            runner,
            prompt_text=job.prompt_path.read_text(encoding="utf-8"),
            model=model,
            timeout_seconds=timeout_seconds,
            env=env,
            use_output_schema=use_output_schema,
            process_runner=process_runner,
        )
        attempts.append(_attempt_record(attempt, result))
        if result.result != "ok":
            _failure_artifact(job, attempt, result)
            return {
                **base,
                "attempts": attempts,
                "duration_seconds": round(time.monotonic() - started, 3),
                "validation_result": result.result,
                "scores_imported": 0,
                "receipt_path": str(job.receipt_path),
            }

        cleaned = clean_bundle_text(result.raw_text)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            invalid = RunnerResult("invalid_json", result.raw_text, result.usage, str(exc), result.duration_seconds)
            attempts[-1] = _attempt_record(attempt, invalid)
            _failure_artifact(job, attempt, invalid)
            if attempt == 1:
                continue
            terminal = "invalid_json"
            failure = str(exc)
        else:
            try:
                bundle = validate_bundle_identity(parsed, job)
                imported = _promote_validated_bundle(
                    bundle,
                    job=job,
                    scores_dir=scores_dir,
                    runner=runner,
                    model=model,
                    auth_method=auth_method,
                )
            except ValueError as exc:
                invalid = RunnerResult("validation_failed", result.raw_text, result.usage, str(exc), result.duration_seconds)
                attempts[-1] = _attempt_record(attempt, invalid)
                _failure_artifact(job, attempt, invalid)
                if attempt == 1:
                    continue
                terminal = "validation_failed"
                failure = str(exc)
            except OSError as exc:
                failed = RunnerResult("runner_error", result.raw_text, result.usage, str(exc), result.duration_seconds)
                attempts[-1] = _attempt_record(attempt, failed)
                _failure_artifact(job, attempt, failed)
                return {
                    **base,
                    "attempts": attempts,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "validation_result": "runner_error",
                    "failure": str(exc),
                    "scores_imported": 0,
                    "receipt_path": str(job.receipt_path),
                }
            else:
                return {
                    **base,
                    "attempts": attempts,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "validation_result": "imported",
                    "scores_imported": imported,
                    "receipt_path": str(job.receipt_path),
                }
        return {
            **base,
            "attempts": attempts,
            "duration_seconds": round(time.monotonic() - started, 3),
            "validation_result": terminal,
            "failure": failure,
            "scores_imported": 0,
            "receipt_path": str(job.receipt_path),
        }
    raise AssertionError("unreachable retry state")


def _model_for_runner(args: argparse.Namespace) -> str:
    if args.model:
        return args.model
    env_key = "CODEX_SUBSCRIPTION_SCORING_MODEL" if args.runner == "codex" else "CLAUDE_SUBSCRIPTION_SCORING_MODEL"
    return os.getenv(env_key, "").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score manual LLM bundles using a verified Codex or Claude subscription")
    parser.add_argument("--queue-dir", required=True, help="Review queue containing batches/")
    parser.add_argument("--batch-id", action="append", default=[], help="Specific batch ID; repeatable (default: all)")
    parser.add_argument("--runner", choices=("codex", "claude"), default="codex")
    parser.add_argument("--model", default="", help="Optional runner model override")
    parser.add_argument("--scores-dir", default="", help="Default: <queue-dir>/scores")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--force", action="store_true", help="Replace stale or previously imported scores")
    parser.add_argument("--no-output-schema", action="store_true", help="Codex only: rely on prompt plus importer validation")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Append-only JSONL ledger")
    parser.add_argument("--auth-check-only", action="store_true", help="Verify subscription authentication without model usage")
    return parser


def main(argv: Sequence[str] | None = None, *, process_runner: ProcessRunner = run_process) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    if args.no_output_schema and args.runner != "codex":
        raise SystemExit("--no-output-schema is only valid with --runner codex")

    queue_dir = _absolute(args.queue_dir)
    scores_dir = _absolute(args.scores_dir) if args.scores_dir else queue_dir / "scores"
    ledger_path = _absolute(args.ledger)
    model = _model_for_runner(args)
    env = sanitized_environment()

    try:
        jobs = discover_batches(queue_dir, args.batch_id, scores_dir)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1

    try:
        with queue_lock(queue_dir):
            auth = preflight_subscription_auth(args.runner, env=env, process_runner=process_runner)
            if not auth.ok:
                append_ledger(
                    ledger_path,
                    {
                        "event": "run_auth_failure",
                        "timestamp": _timestamp(),
                        "queue_dir": str(queue_dir),
                        "runner": args.runner,
                        "model": model or "runner-default",
                        "validation_result": "auth_unverified",
                        "detail": auth.detail,
                        "cost_basis": "subscription_auth",
                    },
                )
                print(f"[HARD STOP] Subscription authentication not verified: {auth.detail}")
                return 2
            print(f"[AUTH] {auth.detail}")
            if args.auth_check_only:
                print("[DONE] Authentication check only; no model was invoked.")
                return 0

            exit_code = 0
            for job in jobs:
                result = score_one_batch(
                    job,
                    runner=args.runner,
                    model=model,
                    scores_dir=scores_dir,
                    timeout_seconds=args.timeout_seconds,
                    force=args.force,
                    auth_method=auth.method,
                    env=env,
                    use_output_schema=not args.no_output_schema,
                    process_runner=process_runner,
                )
                append_ledger(ledger_path, result)
                status = result["validation_result"]
                print(f"[{status.upper()}] {job.batch_id}: scores_imported={result['scores_imported']}")
                if status in {"quota_exhausted", "auth_unverified"}:
                    print("[HARD STOP] Remaining batches were not attempted.")
                    return 2
                if status not in {"imported", "skipped_receipt_match"}:
                    exit_code = 1
            return exit_code
    except QueueLockedError as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
