#!/usr/bin/env python3
"""Deterministic Hermes command layer for the local lead pipeline.

Hermes is transport only. This module owns command grammar, command construction,
and safety checks so paid scoring and live Airtable writes are never bundled into
one vague confirmation step.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shlex
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from generate_search_config import MARKET_PROFILES_DIR, VALID_SIZES, VERTICAL_PROFILES_DIR
from lead_machine.hermes_batch import BatchPaths, BatchResolutionError, nearby_batches, resolve_batch_paths
from lead_machine.hermes_executor import StreamResult, render_command, run_streaming
from lead_machine.hermes_state import current_sync_hash, load_run_state, update_run_state, utc_now
from lead_machine.sendability import (
    PACKET_TIER_MANUAL,
    PACKET_TIER_NONE,
    PACKET_TIER_SENDABLE,
    PACKET_TIER_WATCHLIST,
)


PIPELINE_SCRIPT = "run_agent_pipeline.py"
TELEGRAM_BUDGET = 3500
DEFAULT_SCORE_LIMIT = 25
DEFAULT_BUDGET = 2.00
DEFAULT_BATCH_SIZE = 3
DEFAULT_MAX_TOKENS = 4096
DEFAULT_SCORE_TIMEOUT_MINUTES = 20
DEFAULT_SCORE_REST_CHUNK = 9
DEFAULT_AUTO_CHUNK_SIZE = 25
DEFAULT_APPROVAL_WINDOW_MINUTES = 30
DRY_RUN_MAX_AGE_MINUTES = 30
DEFAULT_REPO_ROOT = Path("~/local-lead-machine")
PIPELINE_ROOT_MARKERS = ("run_agent_pipeline.py", "score_leads.py", "output")
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PROJECTED_RE = re.compile(r"projected_usd=([0-9]+(?:\.[0-9]+)?)")
BILLING_RE = re.compile(r"(credit|billing|balance|payment|insufficient|quota)", re.IGNORECASE)


class CommandError(Exception):
    """Raised to refuse a command with exit code 2."""


@dataclass
class CommandResult:
    message: str
    exit_code: int = 0


@dataclass(frozen=True)
class TelegramRoute:
    argv: list[str]
    command: list[str]
    cwd: Path
    env: dict[str, str]

    def preview(self) -> str:
        env_text = " ".join(f"{key}={shlex.quote(value)}" for key, value in self.env.items())
        return f"cd {shlex.quote(str(self.cwd))} && {env_text} {render_command(self.command)}"


def _truncate(text: str, budget: int = TELEGRAM_BUDGET) -> str:
    return text if len(text) <= budget else "..." + text[-(budget - 3):]


def _absolute(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _absolute_without_symlink_resolution(path: Path | str) -> Path:
    return Path(path).expanduser().absolute()


def _looks_like_pipeline_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in PIPELINE_ROOT_MARKERS)


def resolve_repo_root(
    cli_root: Path | str | None = None,
    *,
    env: dict[str, str] | os._Environ[str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path:
    env_map = os.environ if env is None else env
    if cli_root:
        return _absolute(cli_root)
    env_root = env_map.get("LOCAL_LEAD_MACHINE_ROOT") if env_map is not None else None
    if env_root:
        return _absolute(env_root)

    base_home = Path.home() if home is None else home
    default_root = (base_home / "local-lead-machine").expanduser().resolve()
    if _looks_like_pipeline_root(default_root):
        return default_root

    cwd_path = Path.cwd() if cwd is None else cwd
    cwd_root = cwd_path.expanduser().resolve()
    if _looks_like_pipeline_root(cwd_root):
        return cwd_root
    return default_root


def resolve_python(
    cli_python: Path | str | None = None,
    *,
    repo_root: Path,
    env: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    env_map = os.environ if env is None else env
    if cli_python:
        return _absolute_without_symlink_resolution(cli_python)
    env_python = env_map.get("LOCAL_LEAD_MACHINE_PYTHON") if env_map is not None else None
    if env_python:
        return _absolute_without_symlink_resolution(env_python)
    repo_python = repo_root / ".venv" / "bin" / "python"
    if repo_python.exists():
        return _absolute_without_symlink_resolution(repo_python)
    return _absolute_without_symlink_resolution(sys.executable)


def _preview(verb: str, cmd: list[str], *, root: Path) -> str:
    return (
        f"repo_root: {root}\n"
        f"preview {verb}: cd {shlex.quote(str(root))} && {render_command(cmd)}"
    )


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise CommandError(
            f"Invalid slug '{slug}'. Use lowercase letters, digits and underscores only."
        )
    return slug


def validate_size(size: str) -> str:
    if size not in VALID_SIZES:
        raise CommandError(f"Invalid size '{size}'. Expected one of {', '.join(VALID_SIZES)}.")
    return size


def split_terms(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(";") if item.strip()]


def _resolve(batch: str, *, root: Path | str = Path(".")) -> BatchPaths:
    try:
        return resolve_batch_paths(batch, root=Path(root))
    except BatchResolutionError as exc:
        matches = nearby_batches(batch, root=root)
        suffix = f" Nearby: {', '.join(matches)}." if matches else ""
        raise CommandError(f"{exc}{suffix}") from exc


def _python(script: str, *, python_path: Path | str | None = None) -> list[str]:
    return [str(python_path or sys.executable), script]


def _fmt_usd(value: float) -> str:
    return f"{float(value):.2f}"


def _score_env(*, budget: float, limit: int, batch_size: int, max_tokens: int) -> dict[str, str]:
    calls = max(1, math.ceil(max(1, limit) / max(1, batch_size)))
    token_cap = max(max_tokens * calls + 50_000, max_tokens)
    return {
        "MAX_LLM_USD_PER_RUN": _fmt_usd(budget),
        "MAX_LLM_CALLS_PER_RUN": str(calls),
        "MAX_LLM_TOKENS_PER_RUN": str(token_cap),
    }


def build_run_command(vertical: str, market: str, size: str, *, python_path: Path | str | None = None) -> list[str]:
    validate_slug(vertical)
    validate_slug(market)
    validate_size(size)
    return [
        *_python(PIPELINE_SCRIPT, python_path=python_path),
        "--vertical", vertical,
        "--market", market,
        "--size", size,
        "--mode", "search-only",
        "--max-llm-usd", _fmt_usd(DEFAULT_BUDGET),
    ]


def build_score_dry_command(
    paths: BatchPaths,
    *,
    python_path: Path | str | None = None,
    limit: int = DEFAULT_SCORE_LIMIT,
    budget: float = DEFAULT_BUDGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[list[str], dict[str, str]]:
    cmd = [
        *_python("score_leads.py", python_path=python_path),
        "--input", str(paths.enriched),
        "--output", str(paths.scored_csv),
        "--vertical", paths.vertical,
        "--mode", "api-llm",
        "--scores-dir", str(paths.scores_dir),
        "--resume-scored",
        "--limit", str(limit),
        "--llm-batch-size", str(batch_size),
        "--llm-max-tokens", str(max_tokens),
        "--dry-run-llm-cost",
    ]
    return cmd, _score_env(budget=budget, limit=limit, batch_size=batch_size, max_tokens=max_tokens)


def build_score_command(
    paths: BatchPaths,
    *,
    python_path: Path | str | None = None,
    limit: int,
    budget: float = DEFAULT_BUDGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[list[str], dict[str, str]]:
    cmd = [
        *_python("score_leads.py", python_path=python_path),
        "--input", str(paths.enriched),
        "--output", str(paths.scored_csv),
        "--vertical", paths.vertical,
        "--mode", "api-llm",
        "--scores-dir", str(paths.scores_dir),
        "--resume-scored",
        "--limit", str(limit),
        "--llm-batch-size", str(batch_size),
        "--llm-max-tokens", str(max_tokens),
        "--confirm-llm-spend",
        "--fresh-scores-dir",
        "--cards-output", str(paths.cards_output),
        "--llm-usage-output", str(paths.usage_json),
    ]
    return cmd, _score_env(budget=budget, limit=limit, batch_size=batch_size, max_tokens=max_tokens)


def build_refresh_command(paths: BatchPaths, *, python_path: Path | str | None = None) -> list[str]:
    return [*_python("refresh_packet_tiers.py", python_path=python_path), str(paths.scored_csv), "--output", str(paths.tiered_csv)]


def build_packets_command(paths: BatchPaths, *, python_path: Path | str | None = None) -> list[str]:
    return [*_python("make_packets.py", python_path=python_path), "--inputs", str(paths.tiered_csv), "--output-dir", str(paths.packets_dir)]


def build_sync_command(
    paths: BatchPaths,
    *,
    include_manual: bool,
    live: bool,
    python_path: Path | str | None = None,
) -> list[str]:
    cmd = [
        *_python("upload_airtable.py", python_path=python_path),
        "--input", str(paths.tiered_csv),
        "--packet-dir", str(paths.packets_dir),
    ]
    cmd.append("--include-manual-review" if include_manual else "--only-sendable")
    cmd.append("--live" if live else "--dry-run")
    return cmd


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())


def _tier_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts = {
        PACKET_TIER_SENDABLE: 0,
        PACKET_TIER_MANUAL: 0,
        PACKET_TIER_WATCHLIST: 0,
        PACKET_TIER_NONE: 0,
    }
    for row in rows:
        tier = (row.get("Packet Tier") or "").strip()
        if tier in counts:
            counts[tier] += 1
    return counts


def _update_counts(paths: BatchPaths, *, next_action: str = "") -> None:
    scored_rows = _read_csv_rows(paths.scored_csv)
    packet_rows = _read_csv_rows(paths.packet_index)
    tiers = _tier_counts(_read_csv_rows(paths.tiered_csv) or packet_rows)
    update_run_state(
        paths,
        enriched_rows=_count_jsonl(paths.enriched),
        scored_rows=len(scored_rows),
        packet_count=len(packet_rows),
        sendable_count=tiers[PACKET_TIER_SENDABLE],
        manual_review_count=tiers[PACKET_TIER_MANUAL],
        next_recommended_action=next_action,
    )


def _ensure_ok(result: StreamResult, *, action: str) -> None:
    if result.returncode != 0:
        tail = "\n".join(result.tail_lines[-20:])
        raise CommandError(f"{action} failed with exit {result.returncode}.\n{tail}")


def _parse_projected(lines: list[str]) -> float | None:
    text = "\n".join(lines)
    matches = PROJECTED_RE.findall(text)
    return float(matches[-1]) if matches else None


def _is_billing_error(text: str) -> bool:
    return bool(BILLING_RE.search(text))


def _approval_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_APPROVAL_WINDOW_MINUTES)).isoformat()


def create_pending_approval(
    paths: BatchPaths,
    *,
    stage: str,
    original_command: str,
    next_command: str,
    include_manual: bool,
    score_budget: float,
    projected_score_cost: float | None = None,
    dry_run_hash: str = "",
    chat_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    approval = {
        "approval_id": uuid.uuid4().hex,
        "batch": paths.batch,
        "stage": stage,
        "original_command": original_command,
        "next_command": next_command,
        "created_at": utc_now(),
        "expires_at": _approval_expiry(),
        "include_manual": bool(include_manual),
        "score_budget": float(score_budget),
        "projected_score_cost": projected_score_cost,
        "dry_run_hash": dry_run_hash,
        "chat_id": str(chat_id or ""),
        "user_id": str(user_id or ""),
    }
    update_run_state(
        paths,
        pending_approval=approval,
        autopilot_original_command=original_command,
        next_recommended_action="Reply yes/no",
    )
    return approval


def clear_pending_approval(paths: BatchPaths) -> None:
    update_run_state(paths, pending_approval=None)


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pending_expired(approval: dict[str, Any]) -> bool:
    expires_at = str(approval.get("expires_at") or "")
    if not expires_at:
        return True
    try:
        return datetime.now(timezone.utc) > _parse_iso_datetime(expires_at)
    except ValueError:
        return True


def _find_pending_approval(
    *,
    root: Path,
    chat_id: str = "",
    user_id: str = "",
) -> tuple[BatchPaths, dict[str, Any]]:
    state_dir = root / "run_state"
    candidates = sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if state_dir.exists() else []
    for state_path in candidates:
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        approval = data.get("pending_approval")
        if not isinstance(approval, dict):
            continue
        if chat_id and approval.get("chat_id") and str(approval.get("chat_id")) != str(chat_id):
            continue
        if user_id and approval.get("user_id") and str(approval.get("user_id")) != str(user_id):
            continue
        batch = str(approval.get("batch") or data.get("batch") or state_path.stem)
        return _resolve(batch, root=root), approval
    raise CommandError("No pending approval found.")


def _original_auto_command(vertical: str, market: str, size: str, *, budget: float, include_manual: bool) -> str:
    cmd = f"/run {vertical} {market} {size} --auto --score-budget {_fmt_usd(budget)}"
    if include_manual:
        cmd += " --include-manual"
    return cmd


def _score_resume_command(batch: str, *, budget: float, include_manual: bool) -> str:
    cmd = f"/score-rest {batch} --budget {_fmt_usd(budget)} --chunk-size {DEFAULT_AUTO_CHUNK_SIZE}"
    if include_manual:
        cmd += f" && /packets {batch} && /sync-dry {batch} --include-manual"
    else:
        cmd += f" && /packets {batch} && /sync-dry {batch}"
    return cmd


def _live_resume_command(batch: str, *, include_manual: bool) -> str:
    return f"/sync-live {batch}" + (" --include-manual" if include_manual else "")


def parse_airtable_plan(lines: list[str]) -> tuple[int, int, int, list[str], list[str]]:
    warnings = [line for line in lines if line.startswith("[WARN]")]
    matches = [line for line in lines if line.startswith("[MATCH]")]
    text = "\n".join(lines)
    start = text.find("[")
    while start != -1:
        try:
            plan = json.loads(text[start:])
            if isinstance(plan, list):
                post = sum(1 for item in plan if item.get("method") == "POST")
                patch = sum(1 for item in plan if item.get("method") == "PATCH")
                skip = sum(1 for item in plan if item.get("method") == "SKIP")
                warnings.extend(str(item.get("warning")) for item in plan if item.get("warning"))
                return post, patch, skip, warnings, matches
        except json.JSONDecodeError:
            start = text.find("[", start + 1)
            continue
        break
    post = sum(1 for line in lines if "[SYNC] POST" in line)
    patch = sum(1 for line in lines if "[SYNC] PATCH" in line)
    skip = sum(1 for line in lines if line.startswith("[SKIP]"))
    return post, patch, skip, warnings, matches


def record_dry_run(
    paths: BatchPaths,
    *,
    include_manual: bool,
    post: int,
    patch: int,
    skip: int,
    warnings: list[str],
    fallback_matches: int = 0,
    root: Path | str = Path("."),
) -> None:
    update_run_state(
        paths,
        last_airtable_dry_run_timestamp=utc_now(),
        dry_run_input_hash=current_sync_hash(paths, include_manual=include_manual),
        dry_run_post_count=post,
        dry_run_patch_count=patch,
        dry_run_skip_count=skip,
        dry_run_warnings=warnings,
        dry_run_fallback_match_count=fallback_matches,
        dry_run_include_manual=include_manual,
        next_recommended_action=f"/sync-live {paths.batch}" + (" --include-manual" if include_manual else ""),
    )


def cmd_confirm(batch: str, **_: Any) -> CommandResult:
    raise CommandError(
        "/confirm is deprecated and refusal-only. Use /score, then /packets, "
        "then /sync-dry, then /sync-live when the dry-run is clean."
    )


def _summary(paths: BatchPaths, *, live_status: str) -> str:
    state = load_run_state(paths)
    enriched = _count_jsonl(paths.enriched)
    scored = len(_read_csv_rows(paths.scored_csv))
    unscored = max(0, enriched - scored)
    packet_rows = _read_csv_rows(paths.packet_index)
    tiers = _tier_counts(_read_csv_rows(paths.tiered_csv) or packet_rows)
    usage = {}
    if paths.usage_json.exists():
        try:
            usage = json.loads(paths.usage_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            usage = {}
    projected = state.get("projected_scoring_cost")
    actual = usage.get("estimated_usd")
    usage_text = "LLM usage unavailable"
    if actual is not None:
        usage_text = f"actual LLM ${float(actual):.2f}"
    elif projected is not None:
        usage_text = f"projected LLM ${float(projected):.2f}"
    warnings = state.get("dry_run_warnings") or []
    return (
        f"Final summary: batch {paths.batch}; enriched {enriched}; scored {scored}; unscored {unscored}; "
        f"{usage_text}; packets {len(packet_rows)}; sendable packets {tiers[PACKET_TIER_SENDABLE]}; "
        f"manual review packets {tiers[PACKET_TIER_MANUAL]}; Airtable POST {state.get('dry_run_post_count', 0)} "
        f"PATCH {state.get('dry_run_patch_count', 0)} SKIP {state.get('dry_run_skip_count', 0)}; "
        f"warnings {len(warnings)}; live sync {live_status}; "
        f"next recommended action {state.get('next_recommended_action') or f'/status {paths.batch}'}."
    )


def _sync_dry_is_clean(state: dict[str, Any]) -> bool:
    return (
        int(state.get("dry_run_skip_count") or 0) == 0
        and not state.get("dry_run_warnings")
        and bool(state.get("dry_run_input_hash"))
    )


def _approval_prompt_score(
    paths: BatchPaths,
    *,
    budget: float,
    selected: int,
    include_manual: bool,
    original_command: str,
    chat_id: str = "",
    user_id: str = "",
) -> CommandResult:
    state = load_run_state(paths)
    projected = state.get("projected_scoring_cost")
    cost_text = f"${float(projected):.2f}" if projected is not None else "unknown"
    if projected is not None and float(projected) > budget:
        clear_pending_approval(paths)
        update_run_state(
            paths,
            last_error=f"Projected scoring cost ${float(projected):.2f} exceeded budget ${budget:.2f}.",
            next_recommended_action=f"/score-dry {paths.batch} --limit {DEFAULT_AUTO_CHUNK_SIZE} --budget {_fmt_usd(budget)}",
        )
        return CommandResult(
            f"Projected cost ${float(projected):.2f} exceeds budget ${budget:.2f}. "
            f"Autopilot stopped before paid scoring. Resume with: /score-dry {paths.batch} --limit {DEFAULT_AUTO_CHUNK_SIZE} --budget {_fmt_usd(budget)}"
        )
    existing = len(_read_csv_rows(paths.scored_csv))
    create_pending_approval(
        paths,
        stage="score",
        original_command=original_command,
        next_command=_score_resume_command(paths.batch, budget=budget, include_manual=include_manual),
        include_manual=include_manual,
        score_budget=budget,
        projected_score_cost=projected,
        chat_id=chat_id,
        user_id=user_id,
    )
    return CommandResult(
        f"Scoring dry-run complete for {paths.batch}. Projected cost: {cost_text}. Budget: ${budget:.2f}. "
        f"Existing scored: {existing}. Selected: {selected}. Continue paid scoring? Reply yes/no."
    )


def _continue_after_scoring(
    paths: BatchPaths,
    *,
    budget: float,
    include_manual: bool,
    yes_live: bool,
    root: Path,
    python_path: Path | str | None,
    original_command: str,
    chat_id: str = "",
    user_id: str = "",
) -> CommandResult:
    clear_pending_approval(paths)
    try:
        score_result = cmd_score_rest(
            paths.batch,
            budget=budget,
            chunk_size=DEFAULT_AUTO_CHUNK_SIZE,
            batch_size=DEFAULT_BATCH_SIZE,
            max_tokens=DEFAULT_MAX_TOKENS,
            timeout_minutes=DEFAULT_SCORE_TIMEOUT_MINUTES,
            execute=True,
            root=root,
            python_path=python_path,
        )
    except CommandError as exc:
        text = str(exc)
        if _is_billing_error(text):
            update_run_state(
                paths,
                last_error=f"Anthropic credit/billing error: {text}",
                next_recommended_action=_score_resume_command(paths.batch, budget=budget, include_manual=include_manual),
            )
            return CommandResult(
                "Anthropic credit/billing error. Autopilot stopped. "
                f"Top up credits, then resume with: {_score_resume_command(paths.batch, budget=budget, include_manual=include_manual)}"
            )
        update_run_state(paths, last_error=text, next_recommended_action=_score_resume_command(paths.batch, budget=budget, include_manual=include_manual))
        raise

    if len(_read_csv_rows(paths.scored_csv)) <= 0:
        update_run_state(paths, next_recommended_action=_score_resume_command(paths.batch, budget=budget, include_manual=include_manual))
        return CommandResult(
            f"{score_result.message}\nAutopilot stopped: no scored rows available for packets. "
            f"Resume with: {_score_resume_command(paths.batch, budget=budget, include_manual=include_manual)}"
        )

    packets_result = cmd_packets(paths.batch, execute=True, root=root, python_path=python_path)
    dry_result = cmd_sync_dry(paths.batch, include_manual=include_manual, execute=True, root=root, python_path=python_path)
    state = load_run_state(paths)
    post = int(state.get("dry_run_post_count") or 0)
    patch = int(state.get("dry_run_patch_count") or 0)
    skip = int(state.get("dry_run_skip_count") or 0)
    warnings = state.get("dry_run_warnings") or []
    fallback_matches = int(state.get("dry_run_fallback_match_count") or 0)
    if not _sync_dry_is_clean(state):
        clear_pending_approval(paths)
        update_run_state(paths, next_recommended_action=f"/sync-dry {paths.batch}" + (" --include-manual" if include_manual else ""))
        return CommandResult(
            f"Scoring complete. Packets built. {dry_result.message}\n"
            f"Live sync refused: dry-run is not clean. POST {post} / PATCH {patch} / SKIP {skip} / "
            f"warnings {len(warnings)} / fallback matches {fallback_matches}. Review the dry-run output before live sync."
        )

    dry_hash = str(state.get("dry_run_input_hash") or "")
    if yes_live:
        live_result = cmd_sync_live(paths.batch, include_manual=include_manual, execute=True, root=root, python_path=python_path)
        clear_pending_approval(paths)
        return CommandResult(f"Live sync complete. {live_result.message}\n{_summary(paths, live_status='completed')}")

    create_pending_approval(
        paths,
        stage="live",
        original_command=original_command,
        next_command=_live_resume_command(paths.batch, include_manual=include_manual),
        include_manual=include_manual,
        score_budget=budget,
        dry_run_hash=dry_hash,
        chat_id=chat_id,
        user_id=user_id,
    )
    return CommandResult(
        f"Scoring complete. Packets built. Airtable dry-run complete for {paths.batch}. "
        f"POST {post} / PATCH {patch} / SKIP {skip} / warnings {len(warnings)}. "
        f"Include manual review: {'yes' if include_manual else 'no'}. Live sync now? Reply yes/no."
    )


def _continue_live_approval(
    paths: BatchPaths,
    approval: dict[str, Any],
    *,
    root: Path,
    python_path: Path | str | None,
) -> CommandResult:
    include_manual = bool(approval.get("include_manual"))
    expected_hash = str(approval.get("dry_run_hash") or "")
    state = load_run_state(paths)
    if state.get("dry_run_input_hash") != expected_hash:
        raise CommandError("Refusing live approval: dry-run hash no longer matches.")
    if current_sync_hash(paths, include_manual=include_manual) != expected_hash:
        raise CommandError("Refusing live approval: dry-run hash no longer matches.")
    live_result = cmd_sync_live(paths.batch, include_manual=include_manual, execute=True, root=root, python_path=python_path)
    clear_pending_approval(paths)
    return CommandResult(f"Live sync complete. {live_result.message}\n{_summary(paths, live_status='completed')}")


def cmd_approval_reply(
    reply: str,
    *,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
    chat_id: str = "",
    user_id: str = "",
) -> CommandResult:
    root_path = _absolute(root)
    paths, approval = _find_pending_approval(root=root_path, chat_id=chat_id, user_id=user_id)
    if _pending_expired(approval):
        clear_pending_approval(paths)
        raise CommandError("Approval expired. Re-run the autopilot command or rerun the dry-run stage to refresh the gate.")
    normalized = reply.strip().lower()
    if normalized == "no":
        next_command = str(approval.get("next_command") or "")
        clear_pending_approval(paths)
        update_run_state(paths, next_recommended_action=next_command)
        return CommandResult(f"Autopilot stopped. Resume with: {next_command}")
    if normalized != "yes":
        raise CommandError("Reply yes/no.")
    stage = str(approval.get("stage") or "")
    if stage == "score":
        return _continue_after_scoring(
            paths,
            budget=float(approval.get("score_budget") or DEFAULT_BUDGET),
            include_manual=bool(approval.get("include_manual")),
            yes_live=False,
            root=root_path,
            python_path=python_path,
            original_command=str(approval.get("original_command") or ""),
        )
    if stage == "live":
        return _continue_live_approval(paths, approval, root=root_path, python_path=python_path)
    raise CommandError(f"Refusing unknown approval stage: {stage}")


def cmd_run(
    vertical: str,
    market: str,
    size: str,
    *,
    auto: bool = False,
    score_budget: float = DEFAULT_BUDGET,
    include_manual: bool = False,
    yes_score: bool = False,
    yes_live: bool = False,
    yes_all: bool = False,
    chat_id: str = "",
    user_id: str = "",
    execute: bool = False,
    timeout_minutes: int = 60,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    if auto:
        original = _original_auto_command(vertical, market, size, budget=score_budget, include_manual=include_manual)
        if not execute:
            return CommandResult(
                f"repo_root: {root_path}\n"
                f"preview /run --auto: {original} --execute\n"
                f"Stages: /run -> /status -> /score-dry -> approval -> /score-rest -> /packets -> /sync-dry -> approval -> /sync-live"
            )
        run_result = cmd_run(
            vertical,
            market,
            size,
            execute=True,
            timeout_minutes=timeout_minutes,
            root=root_path,
            python_path=python_path,
        )
        match = re.search(r"Batch:\s*(\S+)", run_result.message)
        batch = match.group(1) if match else ""
        if not batch or batch == "unknown":
            raise CommandError("Autopilot could not resolve batch from /run output.")
        paths = _resolve(batch, root=root_path)
        status = cmd_status(batch, root=root_path).message
        dry = cmd_score_dry(
            batch,
            limit=DEFAULT_AUTO_CHUNK_SIZE,
            budget=score_budget,
            batch_size=DEFAULT_BATCH_SIZE,
            max_tokens=DEFAULT_MAX_TOKENS,
            execute=True,
            root=root_path,
            python_path=python_path,
        )
        selected = DEFAULT_AUTO_CHUNK_SIZE
        approval = _approval_prompt_score(
            paths,
            budget=score_budget,
            selected=selected,
            include_manual=include_manual,
            original_command=original,
            chat_id=chat_id,
            user_id=user_id,
        )
        if load_run_state(paths).get("pending_approval") is None:
            return CommandResult(f"Search/enrich complete. Status: {status}\n{dry.message}\n{approval.message}")
        if not (yes_score or yes_all):
            return CommandResult(f"Search/enrich complete. Status: {status}\n{approval.message}")
        return _continue_after_scoring(
            paths,
            budget=score_budget,
            include_manual=include_manual,
            yes_live=bool(yes_live or yes_all),
            root=root_path,
            python_path=python_path,
            original_command=original,
            chat_id=chat_id,
            user_id=user_id,
        )

    cmd = build_run_command(vertical, market, size, python_path=python_path)
    if not execute:
        return CommandResult(f"{_preview('/run', cmd, root=root_path)}\nNext: /score-dry <batch>")
    result = run_streaming(cmd, timeout_seconds=timeout_minutes * 60, cwd=root_path)
    _ensure_ok(result, action="/run")
    batch = ""
    for line in result.tail_lines:
        m = re.search(r"\[PIPELINE\] batch=(\S+)", line)
        if m:
            batch = m.group(1)
    if batch:
        paths = _resolve(batch, root=root_path)
        _update_counts(paths, next_action=f"/score-dry {batch}")
    return CommandResult(f"/run complete. Batch: {batch or 'unknown'}\nNext: /score-dry {batch or '<batch>'}")


def cmd_score_dry(
    batch: str,
    *,
    limit: int = DEFAULT_SCORE_LIMIT,
    budget: float = DEFAULT_BUDGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    cmd, env = build_score_dry_command(
        paths,
        python_path=python_path,
        limit=limit,
        budget=budget,
        batch_size=batch_size,
        max_tokens=max_tokens,
    )
    if not execute:
        return CommandResult(_preview("/score-dry", cmd, root=root_path))
    result = run_streaming(cmd, env=env, cwd=root_path, resume_hint=f"/score-dry {batch} --limit {limit} --budget {_fmt_usd(budget)}")
    _ensure_ok(result, action="/score-dry")
    projected = _parse_projected(result.tail_lines)
    update_run_state(
        paths,
        last_score_dry_run_timestamp=utc_now(),
        projected_scoring_cost=projected,
        next_recommended_action=f"/score {batch} --limit {limit}",
    )
    _update_counts(paths, next_action=f"/score {batch} --limit {limit}")
    cost = f"${projected:.2f}" if projected is not None else "unknown"
    return CommandResult(f"score dry-run complete: projected {cost}. Next: /score {batch} --limit {limit}")


def cmd_score(
    batch: str,
    *,
    limit: int | None,
    budget: float = DEFAULT_BUDGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_minutes: int | None = DEFAULT_SCORE_TIMEOUT_MINUTES,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    if limit is None or limit <= 0:
        raise CommandError("/score requires explicit --limit N.")
    if timeout_minutes is None or timeout_minutes <= 0:
        raise CommandError("/score requires --timeout MINUTES.")
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    cmd, env = build_score_command(
        paths,
        python_path=python_path,
        limit=limit,
        budget=budget,
        batch_size=batch_size,
        max_tokens=max_tokens,
    )
    if not execute:
        return CommandResult(_preview("/score", cmd, root=root_path))
    result = run_streaming(
        cmd,
        timeout_seconds=timeout_minutes * 60,
        env=env,
        cwd=root_path,
        resume_hint=f"/score {batch} --limit {limit} --budget {_fmt_usd(budget)}",
    )
    _ensure_ok(result, action="/score")
    update_run_state(paths, last_paid_score_timestamp=utc_now(), next_recommended_action=f"/score-rest {batch}")
    _update_counts(paths, next_action=f"/score-rest {batch}")
    return CommandResult(f"score complete. Next: /score-rest {batch} or /packets {batch}")


def cmd_score_rest(
    batch: str,
    *,
    budget: float = DEFAULT_BUDGET,
    chunk_size: int = DEFAULT_SCORE_REST_CHUNK,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_minutes: int = DEFAULT_SCORE_TIMEOUT_MINUTES,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /score-rest: chunks of {chunk_size}; no packets; no Airtable"
        )
    spent = 0.0
    previous = len(_read_csv_rows(paths.scored_csv))
    enriched = _count_jsonl(paths.enriched)
    max_chunks = max(1, math.ceil(max(1, enriched - previous) / max(1, chunk_size))) if enriched else 1
    chunks = 0
    result = CommandResult(f"score-rest stopped: no chunks run. Next: /packets {batch}")
    while spent < budget and chunks < max_chunks:
        chunks += 1
        result = cmd_score(
            batch,
            limit=chunk_size,
            budget=max(0.01, budget - spent),
            batch_size=batch_size,
            max_tokens=max_tokens,
            timeout_minutes=timeout_minutes,
            execute=True,
            root=root_path,
            python_path=python_path,
        )
        current = len(_read_csv_rows(paths.scored_csv))
        usage = json.loads(paths.usage_json.read_text(encoding="utf-8")) if paths.usage_json.exists() else {}
        spent += float(usage.get("estimated_usd") or 0.0)
        if current <= previous:
            update_run_state(paths, next_recommended_action=f"/packets {batch}")
            return CommandResult(f"score-rest stopped: no new scored rows. Next: /packets {batch}")
        previous = current
        if enriched and current >= enriched:
            update_run_state(paths, next_recommended_action=f"/packets {batch}")
            return CommandResult(f"score-rest complete. Next: /packets {batch}")
    update_run_state(paths, next_recommended_action=f"/score-rest {batch} --budget {_fmt_usd(budget)}")
    return CommandResult(result.message)


def cmd_packets(
    batch: str,
    *,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    if not paths.scored_csv.exists():
        raise CommandError(f"Missing canonical scored CSV: {paths.scored_csv}")
    scored_count = len(_read_csv_rows(paths.scored_csv))
    if scored_count <= 0:
        raise CommandError(f"Canonical scored CSV has 0 rows: {paths.scored_csv}")
    refresh = build_refresh_command(paths, python_path=python_path)
    packets = build_packets_command(paths, python_path=python_path)
    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /packets:\n"
            f"cd {shlex.quote(str(root_path))} && {render_command(refresh)}\n"
            f"cd {shlex.quote(str(root_path))} && {render_command(packets)}"
        )
    result = run_streaming(refresh, cwd=root_path)
    _ensure_ok(result, action="/packets refresh")
    tiered_count = len(_read_csv_rows(paths.tiered_csv))
    if tiered_count != scored_count:
        raise CommandError(
            f"Tier refresh row mismatch: {paths.scored_csv} has {scored_count}, {paths.tiered_csv} has {tiered_count}."
        )
    result = run_streaming(packets, cwd=root_path)
    _ensure_ok(result, action="/packets build")
    update_run_state(paths, last_packet_build_timestamp=utc_now(), next_recommended_action=f"/sync-dry {batch}")
    _update_counts(paths, next_action=f"/sync-dry {batch}")
    return CommandResult(f"packets built in {paths.packets_dir}. Next: /sync-dry {batch}")


def cmd_sync_dry(
    batch: str,
    *,
    include_manual: bool = False,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    if not paths.packet_index.exists():
        raise CommandError(f"Packet index missing: {paths.packet_index}")
    current_sync_hash(paths, include_manual=include_manual)
    cmd = build_sync_command(paths, include_manual=include_manual, live=False, python_path=python_path)
    if not execute:
        return CommandResult(_preview("/sync-dry", cmd, root=root_path))
    result = run_streaming(cmd, cwd=root_path, resume_hint=f"/sync-dry {batch}", tail_limit=5000)
    _ensure_ok(result, action="/sync-dry")
    post, patch, skip, warnings, matches = parse_airtable_plan(result.tail_lines)
    record_dry_run(
        paths,
        include_manual=include_manual,
        post=post,
        patch=patch,
        skip=skip,
        warnings=warnings,
        fallback_matches=len(matches),
        root=root,
    )
    return CommandResult(
        f"dry-run POST {post} PATCH {patch} SKIP {skip}; warnings {len(warnings)}; fallback matches {len(matches)}. "
        f"Next: /sync-live {batch}{' --include-manual' if include_manual else ''}"
    )


def cmd_sync_live(
    batch: str,
    *,
    include_manual: bool = False,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    if not paths.packet_index.exists():
        raise CommandError(f"Packet index missing: {paths.packet_index}")
    state = load_run_state(paths)
    dry_run_timestamp = state.get("last_airtable_dry_run_timestamp")
    if not dry_run_timestamp:
        raise CommandError("Refusing live sync: run /sync-dry first.")
    try:
        dry_run_at = datetime.fromisoformat(str(dry_run_timestamp))
        if dry_run_at.tzinfo is None:
            dry_run_at = dry_run_at.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CommandError("Refusing live sync: last dry-run timestamp is invalid.") from exc
    age_seconds = (datetime.now(timezone.utc) - dry_run_at.astimezone(timezone.utc)).total_seconds()
    if age_seconds > DRY_RUN_MAX_AGE_MINUTES * 60:
        raise CommandError(f"Refusing live sync: last dry-run is older than {DRY_RUN_MAX_AGE_MINUTES} minutes.")
    if bool(state.get("dry_run_include_manual")) != bool(include_manual):
        raise CommandError("Refusing live sync: --include-manual mode differs from last dry-run.")
    if state.get("dry_run_warnings"):
        raise CommandError("Refusing live sync: last dry-run had warnings.")
    current_hash = current_sync_hash(paths, include_manual=include_manual)
    if state.get("dry_run_input_hash") != current_hash:
        raise CommandError("Refusing live sync: packet/scored input changed since last dry-run.")
    cmd = build_sync_command(paths, include_manual=include_manual, live=True, python_path=python_path)
    if not execute:
        return CommandResult(_preview("/sync-live", cmd, root=root_path))
    result = run_streaming(cmd, cwd=root_path, resume_hint=f"/sync-live {batch}", tail_limit=5000)
    _ensure_ok(result, action="/sync-live")
    post, patch, skip, warnings, _matches = parse_airtable_plan(result.tail_lines)
    update_run_state(
        paths,
        last_live_sync_timestamp=utc_now(),
        live_sync_input_hash=current_hash,
        live_post_count=post,
        live_patch_count=patch,
        live_skip_count=skip,
        dry_run_warnings=warnings,
        next_recommended_action=f"/status {batch}",
    )
    return CommandResult(f"live sync POST {post} PATCH {patch} SKIP {skip}. Next: /status {batch}")


def cmd_status(batch: str, *, root: Path | str = Path("."), **_: Any) -> CommandResult:
    root_path = _absolute(root)
    paths = _resolve(batch, root=root_path)
    enriched = _count_jsonl(paths.enriched)
    scored_rows = _read_csv_rows(paths.scored_csv)
    tiered_rows = _read_csv_rows(paths.tiered_csv)
    packet_rows = _read_csv_rows(paths.packet_index)
    tier_source = tiered_rows or packet_rows
    tiers = _tier_counts(tier_source)
    unscored = max(0, enriched - len(scored_rows))
    stale = ""
    if paths.scored_csv.exists() and (
        not paths.tiered_csv.exists()
        or paths.tiered_csv.stat().st_mtime < paths.scored_csv.stat().st_mtime
        or (tiered_rows and len(tiered_rows) != len(scored_rows))
    ):
        stale = " stale-tiered"

    state = load_run_state(paths)
    dry = ""
    if state.get("last_airtable_dry_run_timestamp"):
        dry = (
            f" dry-run POST {state.get('dry_run_post_count', 0)} "
            f"PATCH {state.get('dry_run_patch_count', 0)} SKIP {state.get('dry_run_skip_count', 0)}"
        )
    live = ""
    if state.get("last_live_sync_timestamp"):
        live = (
            f" live POST {state.get('live_post_count', 0)} "
            f"PATCH {state.get('live_patch_count', 0)} SKIP {state.get('live_skip_count', 0)}"
        )
    if not paths.enriched.exists():
        next_action = f"/run {paths.vertical} {paths.market} {paths.size}"
    elif unscored:
        next_action = f"/score-dry {batch}"
    elif stale or not paths.packet_index.exists():
        next_action = f"/packets {batch}"
    elif not state.get("last_airtable_dry_run_timestamp"):
        next_action = f"/sync-dry {batch}"
    else:
        next_action = state.get("next_recommended_action") or f"/status {batch}"
    message = (
        f"{batch}: enriched {enriched} scored {len(scored_rows)} unscored {unscored} "
        f"packets {len(packet_rows)} sendable {tiers[PACKET_TIER_SENDABLE]} "
        f"manual {tiers[PACKET_TIER_MANUAL]} watchlist {tiers[PACKET_TIER_WATCHLIST]} "
        f"no-packet {tiers[PACKET_TIER_NONE]}{stale}{dry}{live}. Next: {next_action}"
    )
    return CommandResult(_truncate(message))


def build_vertical_profile(slug: str, *, display: str = "", airtable: str = "", terms: list[str] | None = None) -> dict[str, Any]:
    return {
        "vertical": slug,
        "display_name": display or slug.replace("_", " ").title(),
        "airtable_vertical": airtable,
        "search_terms": list(terms or []),
        "high_intent_terms": [],
        "exclude_terms": [],
    }


def cmd_create_vertical(
    slug: str,
    *,
    display: str = "",
    airtable: str = "",
    terms: str = "",
    overwrite: bool = False,
    execute: bool = False,
    vertical_dir: Path = VERTICAL_PROFILES_DIR,
    **_: Any,
) -> CommandResult:
    validate_slug(slug)
    term_list = split_terms(terms)
    if not term_list:
        raise CommandError("--terms is required.")
    path = vertical_dir / f"{slug}.json"
    payload = json.dumps(build_vertical_profile(slug, display=display, airtable=airtable, terms=term_list), indent=2)
    if not execute:
        return CommandResult(f"preview create vertical {path}:\n{payload}")
    if path.exists() and not overwrite:
        raise CommandError(f"{path} already exists. Pass --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    return CommandResult(f"{path} written")


def cmd_create_market(
    slug: str,
    *,
    small: str = "",
    medium: str = "",
    full: str = "",
    display: str = "",
    state: str = "",
    overwrite: bool = False,
    execute: bool = False,
    market_dir: Path = MARKET_PROFILES_DIR,
    **_: Any,
) -> CommandResult:
    validate_slug(slug)
    small_list = split_terms(small)
    medium_list = split_terms(medium)
    full_list = split_terms(full)
    geos = full_list or medium_list or small_list
    if not geos:
        raise CommandError("At least one geo list is required.")
    profile = {
        "market": slug,
        "display_name": display or slug.replace("_", " ").title(),
        "state": state,
        "geos": geos,
        "sizes": {"small": small_list, "medium": medium_list},
    }
    path = market_dir / f"{slug}.json"
    payload = json.dumps(profile, indent=2)
    if not execute:
        return CommandResult(f"preview create market {path}:\n{payload}")
    if path.exists() and not overwrite:
        raise CommandError(f"{path} already exists. Pass --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    return CommandResult(f"{path} written")


def cmd_help() -> CommandResult:
    return CommandResult(
        _truncate(
            "Lead-machine commands:\n"
            "/run <vertical> <market> <size> - search/enrich only\n"
            "/run <vertical> <market> <size> --auto [--score-budget USD] [--include-manual] - guided autopilot\n"
            "/score-dry <batch> [--limit N] [--budget USD] - cost estimate only\n"
            "/score <batch> --limit N [--timeout MINUTES] - paid scoring only\n"
            "/score-rest <batch> - chunk remaining scoring\n"
            "/packets <batch> - refresh tiers and build packets\n"
            "/sync-dry <batch> [--include-manual] - Airtable dry-run\n"
            "/sync-live <batch> [--include-manual] - live only after clean dry-run\n"
            "/status <batch> - local status\n"
            "/confirm - deprecated/refusal-only"
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes_commands")
    runtime = argparse.ArgumentParser(add_help=False)
    runtime.add_argument("--repo-root", default=argparse.SUPPRESS)
    runtime.add_argument("--python", default=argparse.SUPPRESS, dest="python_path")
    parser.add_argument("--repo-root", default=argparse.SUPPRESS)
    parser.add_argument("--python", default=argparse.SUPPRESS, dest="python_path")
    sub = parser.add_subparsers(dest="verb", required=True)
    for name in ("run", "pipeline", "auto-run"):
        p = sub.add_parser(name, parents=[runtime])
        p.add_argument("vertical")
        p.add_argument("market")
        p.add_argument("size")
        p.add_argument("--auto", action="store_true", default=(name in {"pipeline", "auto-run"}))
        p.add_argument("--score-budget", type=float, default=DEFAULT_BUDGET)
        p.add_argument("--include-manual", action="store_true")
        p.add_argument("--yes-score", action="store_true")
        p.add_argument("--yes-live", action="store_true")
        p.add_argument("--yes-all", action="store_true")
        p.add_argument("--chat-id", default="")
        p.add_argument("--user-id", default="")
        p.add_argument("--timeout", type=int, default=60)
        p.add_argument("--execute", action="store_true")

    p = sub.add_parser("score-dry", parents=[runtime])
    p.add_argument("batch")
    p.add_argument("--limit", type=int, default=DEFAULT_SCORE_LIMIT)
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("score", parents=[runtime])
    p.add_argument("batch")
    p.add_argument("--limit", type=int, required=True)
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--timeout", type=int, required=True)
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("score-rest", parents=[runtime])
    p.add_argument("batch")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    p.add_argument("--chunk-size", type=int, default=DEFAULT_SCORE_REST_CHUNK)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--timeout", type=int, default=DEFAULT_SCORE_TIMEOUT_MINUTES)
    p.add_argument("--execute", action="store_true")

    for name in ("packets", "sync-dry", "sync-live", "status", "confirm"):
        p = sub.add_parser(name, parents=[runtime])
        p.add_argument("batch")
        if name in {"packets", "sync-dry", "sync-live", "status", "confirm"}:
            p.add_argument("--execute", action="store_true")
        if name in {"sync-dry", "sync-live"}:
            p.add_argument("--include-manual", action="store_true")

    p = sub.add_parser("create-vertical", parents=[runtime])
    p.add_argument("slug")
    p.add_argument("--display", default="")
    p.add_argument("--airtable", default="")
    p.add_argument("--terms", default="")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("create-market", parents=[runtime])
    p.add_argument("slug")
    p.add_argument("--display", default="")
    p.add_argument("--state", default="")
    p.add_argument("--small", default="")
    p.add_argument("--medium", default="")
    p.add_argument("--full", default="")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("help", parents=[runtime])
    p.add_argument("--execute", action="store_true")

    for name in ("yes", "no"):
        p = sub.add_parser(name, parents=[runtime])
        p.add_argument("--chat-id", default="")
        p.add_argument("--user-id", default="")
    return parser


def dispatch(args: argparse.Namespace) -> CommandResult:
    repo_root = resolve_repo_root(getattr(args, "repo_root", None))
    python_path = resolve_python(getattr(args, "python_path", None), repo_root=repo_root)
    if args.verb in {"run", "pipeline", "auto-run"}:
        return cmd_run(
            args.vertical,
            args.market,
            args.size,
            auto=args.auto,
            score_budget=args.score_budget,
            include_manual=args.include_manual,
            yes_score=args.yes_score,
            yes_live=args.yes_live,
            yes_all=args.yes_all,
            chat_id=args.chat_id,
            user_id=args.user_id,
            execute=args.execute,
            timeout_minutes=args.timeout,
            root=repo_root,
            python_path=python_path,
        )
    if args.verb == "score-dry":
        return cmd_score_dry(
            args.batch,
            limit=args.limit,
            budget=args.budget,
            batch_size=args.batch_size,
            max_tokens=args.max_tokens,
            execute=args.execute,
            root=repo_root,
            python_path=python_path,
        )
    if args.verb == "score":
        return cmd_score(
            args.batch,
            limit=args.limit,
            budget=args.budget,
            batch_size=args.batch_size,
            max_tokens=args.max_tokens,
            timeout_minutes=args.timeout,
            execute=args.execute,
            root=repo_root,
            python_path=python_path,
        )
    if args.verb == "score-rest":
        return cmd_score_rest(
            args.batch,
            budget=args.budget,
            chunk_size=args.chunk_size,
            batch_size=args.batch_size,
            max_tokens=args.max_tokens,
            timeout_minutes=args.timeout,
            execute=args.execute,
            root=repo_root,
            python_path=python_path,
        )
    if args.verb == "packets":
        return cmd_packets(args.batch, execute=args.execute, root=repo_root, python_path=python_path)
    if args.verb == "sync-dry":
        return cmd_sync_dry(
            args.batch,
            include_manual=args.include_manual,
            execute=args.execute,
            root=repo_root,
            python_path=python_path,
        )
    if args.verb == "sync-live":
        return cmd_sync_live(
            args.batch,
            include_manual=args.include_manual,
            execute=args.execute,
            root=repo_root,
            python_path=python_path,
        )
    if args.verb == "status":
        return cmd_status(args.batch, root=repo_root)
    if args.verb == "confirm":
        return cmd_confirm(args.batch, execute=args.execute)
    if args.verb == "create-vertical":
        return cmd_create_vertical(args.slug, display=args.display, airtable=args.airtable, terms=args.terms, overwrite=args.overwrite, execute=args.execute)
    if args.verb == "create-market":
        return cmd_create_market(args.slug, display=args.display, state=args.state, small=args.small, medium=args.medium, full=args.full, overwrite=args.overwrite, execute=args.execute)
    if args.verb == "help":
        return cmd_help()
    if args.verb in {"yes", "no"}:
        return cmd_approval_reply(
            args.verb,
            root=repo_root,
            python_path=python_path,
            chat_id=args.chat_id,
            user_id=args.user_id,
        )
    raise CommandError(f"Unknown verb '{args.verb}'.")


def _normalize_telegram_raw(raw: list[str]) -> list[str]:
    normalized = list(raw)
    i = 0
    while i < len(normalized):
        token = normalized[i]
        if token in {"--repo-root", "--python"}:
            i += 2
            continue
        break
    if i < len(normalized) - 1 and normalized[i] == "run" and normalized[i + 1].startswith("/"):
        del normalized[i]
    if i < len(normalized) and normalized[i].startswith("/"):
        normalized[i] = normalized[i][1:]
    return normalized


TELEGRAM_HERMES_COMMANDS = {
    "run",
    "status",
    "score-dry",
    "score",
    "score-rest",
    "packets",
    "sync-dry",
    "sync-live",
    "pipeline",
    "auto-run",
}
TELEGRAM_APPROVAL_REPLIES = {"yes", "no"}
TELEGRAM_HERMES_WORKTREE = Path("~/local-lead-machine-hermes").expanduser()
TELEGRAM_LLM_ROOT = Path("~/local-lead-machine").expanduser()
TELEGRAM_LLM_PYTHON = TELEGRAM_LLM_ROOT / ".venv" / "bin" / "python"


def _append_execute(argv: list[str]) -> list[str]:
    if "--execute" in argv or (argv and argv[0] in TELEGRAM_APPROVAL_REPLIES):
        return argv
    return [*argv, "--execute"]


def route_telegram_message(message: str) -> TelegramRoute | None:
    try:
        tokens = shlex.split(message)
    except ValueError:
        return None
    if not tokens:
        return None

    outer_run = len(tokens) >= 2 and tokens[0].lower() == "run" and tokens[1].startswith("/")
    if outer_run:
        tokens = tokens[1:]

    if len(tokens) == 1 and tokens[0].strip().lower() in TELEGRAM_APPROVAL_REPLIES:
        argv = [tokens[0].strip().lower()]
    else:
        first = tokens[0]
        if not first.startswith("/"):
            return None
        verb = first[1:]
        if verb not in TELEGRAM_HERMES_COMMANDS:
            return None
        argv = [verb, *tokens[1:]]
        if outer_run or first.startswith("/"):
            argv = _append_execute(argv)

    env = {
        "LOCAL_LEAD_MACHINE_ROOT": str(TELEGRAM_LLM_ROOT),
        "LOCAL_LEAD_MACHINE_PYTHON": str(TELEGRAM_LLM_PYTHON),
    }
    command = [str(TELEGRAM_LLM_PYTHON), "-m", "lead_machine.hermes_commands", *argv]
    return TelegramRoute(argv=argv, command=command, cwd=TELEGRAM_HERMES_WORKTREE, env=env)


def main(argv: Optional[list[str]] = None) -> int:
    raw = _normalize_telegram_raw(list(sys.argv[1:] if argv is None else argv))
    try:
        args = build_parser().parse_args(raw)
        result = dispatch(args)
    except CommandError as exc:
        print(f"[REFUSE] {exc}", flush=True)
        return 2
    print(result.message, flush=True)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
