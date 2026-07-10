#!/usr/bin/env python3
"""Morning send-queue verbs for the Hermes command layer.

Deterministic half of the morning outreach loop: pooled sendable counts,
budget-capped pipeline top-up, the canonical morning queue file, and the
sent write-back. Opener drafting and digest prose live agent-side
(operator-agent SKILLS/morning-queue); every fact the digest states comes
from the queue JSON this module writes. Draft-only: nothing here ever
contacts a prospect — mark-sent only records sends Lou already made.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from lead_machine.airtable_sync import resolve_vertical_label
except ImportError:
    # hermes-command-layer predates the phase-a-g1-api-llm alias registry; the
    # Airtable write path stores slug-style values, so a plain slugifier is a
    # faithful stand-in until that branch merges in.
    def resolve_vertical_label(value: str) -> str:
        cleaned = " ".join(str(value or "").strip().casefold().replace("_", " ").split())
        return cleaned.replace(" ", "_")


from lead_machine.hermes_commands import (
    CommandError,
    CommandResult,
    cmd_run,
    validate_size,
    validate_slug,
)
from lead_machine.hermes_executor import render_command


QUEUE_SCHEMA_VERSION = 1
QUERY_SCRIPT = "query_ready_to_send.py"
MARK_SENT_SCRIPT = "mark_sent.py"
CONFIG_ENV = "MORNING_QUEUE_CONFIG"
CONFIG_FILENAME = "morning_queue.json"
QUERY_TIMEOUT_SECONDS = 120
MARK_SENT_TIMEOUT_SECONDS = 60
TOPUP_TIMEOUT_MINUTES = 60

DEFAULT_CONFIG: dict[str, Any] = {
    "target_queue_size": 30,
    "morning_budget_usd": 2.00,
    "queue_overflow_cap": 40,
    "topup_enabled": False,
    "topup_verticals": [],
    # Fresh searches hit the Places API for real — spend morning_budget_usd does NOT
    # cover — so the fresh retry always runs at topup_fresh_size, never the vertical's
    # configured size ("small" = 1 page/query over the smallest geo set).
    "topup_fresh_enabled": True,
    "topup_fresh_size": "small",
    "outcomes_jsonl": "",
}

RECORD_ID_RE = re.compile(r"^rec[A-Za-z0-9]{14,17}$")
ACTUAL_SPEND_RE = re.compile(r"actual LLM \$([0-9]+(?:\.[0-9]+)?)")
PROJECTED_SPEND_RE = re.compile(r"projected LLM \$([0-9]+(?:\.[0-9]+)?)")
BATCH_NAME_RE = re.compile(r"batch:?\s+([a-z][a-z0-9_]*_[0-9]+)", re.IGNORECASE)
NO_EXISTING_BATCH_RE = re.compile(r"No usable existing batch", re.IGNORECASE)

CHANNEL_LABELS = {
    "sms": "SMS",
    "text": "SMS",
    "ig": "Instagram DM",
    "igdm": "Instagram DM",
    "ig-dm": "Instagram DM",
    "dm": "Instagram DM",
    "instagram": "Instagram DM",
    "instagram-dm": "Instagram DM",
    "phone": "Phone",
    "call": "Phone",
    "email": "Email",
}

ITEM_FIELD_MAP = [
    ("business", "Business Name"),
    ("city", "City"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("website", "Website URL"),
    ("lead_priority", "Lead Priority"),
    ("score_total", "Score Total"),
    ("best_service_angle", "Best Service Angle"),
    ("offer_fit", "Offer Fit"),
    ("specific_observation", "Specific Observation"),
    ("site_status", "Site Status"),
    ("stored_opener", "Opener"),
    ("safe_outreach_claims", "Safe Outreach Claims"),
    ("mockup_url", "Mockup URL"),
    ("video_url", "Video URL"),
]


def _absolute(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _today() -> str:
    return date_cls.today().isoformat()


def _validate_date(value: str) -> str:
    try:
        return date_cls.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CommandError(f"Invalid --date '{value}'. Use YYYY-MM-DD.") from exc


def load_queue_config(root: Path, *, env: dict[str, str] | os._Environ[str] | None = None) -> dict[str, Any]:
    env_map = os.environ if env is None else env
    override = env_map.get(CONFIG_ENV) if env_map is not None else None
    path = _absolute(override) if override else root / CONFIG_FILENAME
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        return config
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Config {path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CommandError(f"Config {path} must be a JSON object.")
    config.update(loaded)
    return config


def queue_file_path(root: Path, date: str) -> Path:
    return root / "run_state" / f"morning_queue_{date}.json"


def topup_file_path(root: Path, date: str) -> Path:
    return root / "run_state" / f"morning_topup_{date}.json"


def latest_queue_file(root: Path) -> Path | None:
    state_dir = root / "run_state"
    if not state_dir.exists():
        return None
    candidates = sorted(state_dir.glob("morning_queue_*.json"))
    return candidates[-1] if candidates else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _run_capture(cmd: list[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Bounded, capturing subprocess call for quick parse-the-output scripts.

    Unlike run_streaming this does not echo every line: the query JSON is an
    input to the queue file, not operator-facing output.
    """
    return subprocess.run(
        [str(arg) for arg in cmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def fetch_pooled_records(*, root: Path, python_path: Path | str | None = None) -> list[dict[str, Any]]:
    cmd = [str(python_path or sys.executable), QUERY_SCRIPT]
    try:
        result = _run_capture(cmd, cwd=root, timeout_seconds=QUERY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{QUERY_SCRIPT} timed out after {QUERY_TIMEOUT_SECONDS}s.") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise CommandError(f"{QUERY_SCRIPT} failed with exit {result.returncode}.\n" + "\n".join(tail))
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"{QUERY_SCRIPT} output is not valid JSON: {exc}") from exc
    if not isinstance(records, list):
        raise CommandError(f"{QUERY_SCRIPT} output must be a JSON list.")
    return records


def per_vertical_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        slug = resolve_vertical_label(str(record.get("Vertical") or "")) or "unknown"
        counts[slug] = counts.get(slug, 0) + 1
    return counts


def _resolve_targets(
    config: dict[str, Any],
    *,
    target: int | None = None,
    budget: float | None = None,
) -> tuple[int, float]:
    resolved_target = int(target if target is not None else config.get("target_queue_size") or 0)
    resolved_budget = float(budget if budget is not None else config.get("morning_budget_usd") or 0.0)
    if resolved_target <= 0:
        raise CommandError("Target queue size must be a positive integer.")
    if resolved_budget <= 0:
        raise CommandError("Budget must be a positive dollar amount.")
    return resolved_target, resolved_budget


# ---------------------------------------------------------------- queue-status


def cmd_queue_status(
    *,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    config = load_queue_config(root_path)
    target = int(config.get("target_queue_size") or 0)
    query_cmd = [str(python_path or sys.executable), QUERY_SCRIPT]
    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /queue-status: cd {shlex.quote(str(root_path))} && {render_command(query_cmd)} (read-only)"
        )
    records = fetch_pooled_records(root=root_path, python_path=python_path)
    pooled = len(records)
    latest = latest_queue_file(root_path)
    payload = {
        "date": _today(),
        "pooled_sendable": pooled,
        "target": target,
        "deficit": max(0, target - pooled),
        "per_vertical": per_vertical_counts(records),
        "latest_queue_file": str(latest) if latest else None,
    }
    return CommandResult(json.dumps(payload, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------- queue-topup


def _classify_topup_outcome(message: str) -> str:
    if "Live sync complete" in message:
        return "completed"
    if "exceeds budget" in message:
        return "stopped-over-budget"
    if "dry-run is not clean" in message:
        return "dry-run-not-clean"
    return "failed"


def _parse_spend(message: str) -> float:
    actual = ACTUAL_SPEND_RE.search(message)
    if actual:
        return float(actual.group(1))
    projected = PROJECTED_SPEND_RE.search(message)
    if projected:
        return float(projected.group(1))
    return 0.0


def _parse_batch(message: str) -> str:
    match = BATCH_NAME_RE.search(message)
    return match.group(1) if match else ""


def _pick_topup_vertical(config: dict[str, Any], counts: dict[str, int]) -> dict[str, str]:
    entries = config.get("topup_verticals") or []
    if not entries:
        raise CommandError(f"No topup_verticals configured in {CONFIG_FILENAME}.")
    best: dict[str, str] | None = None
    best_count: int | None = None
    for entry in entries:
        vertical = validate_slug(str(entry.get("vertical") or ""))
        market = validate_slug(str(entry.get("market") or ""))
        size = validate_size(str(entry.get("size") or ""))
        count = counts.get(vertical, 0)
        if best_count is None or count < best_count:
            best = {"vertical": vertical, "market": market, "size": size}
            best_count = count
    assert best is not None
    return best


def cmd_queue_topup(
    *,
    budget: float | None = None,
    target: int | None = None,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    config = load_queue_config(root_path)
    resolved_target, resolved_budget = _resolve_targets(config, target=target, budget=budget)
    today = _today()
    outcome_path = topup_file_path(root_path, today)

    if not execute:
        verticals = ", ".join(
            f"{e.get('vertical')} {e.get('market')} {e.get('size')}" for e in config.get("topup_verticals") or []
        )
        if config.get("topup_fresh_enabled", True):
            fresh_note = (
                "no existing batch → ONE fresh search at capped size "
                f"'{validate_size(str(config.get('topup_fresh_size') or 'small'))}'"
            )
        else:
            fresh_note = "no existing batch → stop with outcome no-existing-batch (fresh search disabled)"
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /queue-topup: query pooled sendable; if < {resolved_target}, autopilot ONE vertical "
            f"(lowest sendable of: {verticals or 'none configured'}) with score budget ${resolved_budget:.2f}; "
            f"{fresh_note}; writes {outcome_path}. No prospect contact ever."
        )

    generated_at = _local_now().isoformat()
    base_outcome: dict[str, Any] = {
        "date": today,
        "generated_at": generated_at,
        "target": resolved_target,
        "budget_usd": resolved_budget,
    }

    if not config.get("topup_enabled"):
        payload = {**base_outcome, "ran": False, "reason": "topup disabled in config"}
        _atomic_write_json(outcome_path, payload)
        return CommandResult(f"Top-up skipped: disabled in config. Wrote {outcome_path}.")

    records = fetch_pooled_records(root=root_path, python_path=python_path)
    pooled = len(records)
    counts = per_vertical_counts(records)
    base_outcome["pooled_sendable_before"] = pooled

    if pooled >= resolved_target:
        payload = {**base_outcome, "ran": False, "reason": "target met"}
        _atomic_write_json(outcome_path, payload)
        return CommandResult(
            f"Top-up not needed: pooled sendable {pooled} >= target {resolved_target}. Wrote {outcome_path}."
        )

    pick = _pick_topup_vertical(config, counts)
    run_kwargs: dict[str, Any] = dict(
        auto=True,
        score_budget=resolved_budget,
        include_manual=False,
        yes_score=True,
        yes_live=True,
        execute=True,
        timeout_minutes=TOPUP_TIMEOUT_MINUTES,
        root=root_path,
        python_path=python_path,
    )
    outcome = "failed"
    detail = ""
    size_used = pick["size"]
    try:
        try:
            result: CommandResult | None = cmd_run(
                pick["vertical"], pick["market"], pick["size"], use_existing=True, **run_kwargs
            )
        except CommandError as exc:
            if not NO_EXISTING_BATCH_RE.search(str(exc)):
                raise
            if not config.get("topup_fresh_enabled", True):
                result = None
                detail = str(exc)
                outcome = "no-existing-batch"
            else:
                # Fresh search is the expensive path (real Places calls, not gated by
                # morning_budget_usd) — always run it at the capped fresh size.
                size_used = validate_size(str(config.get("topup_fresh_size") or "small"))
                result = cmd_run(pick["vertical"], pick["market"], size_used, fresh=True, **run_kwargs)
        if result is not None:
            detail = result.message
            outcome = _classify_topup_outcome(result.message)
    except CommandError as exc:
        detail = str(exc)
        outcome = "failed"

    payload = {
        **base_outcome,
        "ran": True,
        "vertical": pick["vertical"],
        "market": pick["market"],
        "size": size_used,
        "batch": _parse_batch(detail),
        "outcome": outcome,
        "detail": detail,
        "spend_usd": _parse_spend(detail),
    }
    _atomic_write_json(outcome_path, payload)
    return CommandResult(
        f"Top-up {outcome}: {pick['vertical']} {pick['market']} {pick['size']} "
        f"(pooled {pooled} < target {resolved_target}, spend ${payload['spend_usd']:.2f}). Wrote {outcome_path}."
    )


# ---------------------------------------------------------------- morning-queue


def _build_queue_items(records: list[dict[str, Any]], *, target: int, overflow_cap: int) -> list[dict[str, Any]]:
    ab_count = sum(
        1 for record in records if str(record.get("Lead Priority") or "").strip().upper() in {"A", "B"}
    )
    take = min(max(target, ab_count), max(1, overflow_cap), len(records))
    items: list[dict[str, Any]] = []
    for index, record in enumerate(records[:take], start=1):
        item: dict[str, Any] = {
            "n": index,
            "record_id": str(record.get("record_id") or ""),
            "vertical": resolve_vertical_label(str(record.get("Vertical") or "")) or "unknown",
        }
        for key, field in ITEM_FIELD_MAP:
            item[key] = record.get(field, "")
        item["sent_at"] = None
        item["sent_channel"] = None
        items.append(item)
    return items


def _load_topup_telemetry(root: Path, date: str) -> dict[str, Any] | None:
    path = topup_file_path(root, date)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"error": f"{path.name} is not valid JSON"}
    return loaded if isinstance(loaded, dict) else None


def cmd_morning_queue(
    *,
    target: int | None = None,
    date: str = "",
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    config = load_queue_config(root_path)
    resolved_target = int(target if target is not None else config.get("target_queue_size") or 0)
    if resolved_target <= 0:
        raise CommandError("Target queue size must be a positive integer.")
    overflow_cap = int(config.get("queue_overflow_cap") or resolved_target)
    queue_date = _validate_date(date) if date else _today()
    path = queue_file_path(root_path, queue_date)

    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /morning-queue: pooled Airtable query -> top max({resolved_target}, all A+B) capped at "
            f"{overflow_cap} -> atomic-write {path} (overwrites any manual file for {queue_date})."
        )

    payload: dict[str, Any] = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "date": queue_date,
        "generated_at": _local_now().isoformat(),
        "target": resolved_target,
        "pooled_sendable": 0,
        "per_vertical": {},
        "topup": _load_topup_telemetry(root_path, queue_date),
        "error": None,
        "items": [],
    }
    try:
        records = fetch_pooled_records(root=root_path, python_path=python_path)
    except CommandError as exc:
        # Degraded, never silent: write the dated file with zero items so
        # mark-sent can never map numbers against a stale day.
        payload["error"] = str(exc)
        _atomic_write_json(path, payload)
        return CommandResult(
            f"morning-queue DEGRADED: pooled query failed; wrote {path} with 0 items.\nError: {exc}"
        )

    payload["pooled_sendable"] = len(records)
    payload["per_vertical"] = per_vertical_counts(records)
    payload["items"] = _build_queue_items(records, target=resolved_target, overflow_cap=overflow_cap)
    _atomic_write_json(path, payload)
    topup = payload["topup"] or {}
    topup_text = topup.get("outcome") or topup.get("reason") or "no top-up telemetry"
    return CommandResult(
        f"morning-queue written: {path}\n"
        f"pooled {payload['pooled_sendable']}; queued {len(payload['items'])} "
        f"(target {resolved_target}, cap {overflow_cap}); top-up: {topup_text}."
    )


# ------------------------------------------------------------------- mark-sent


def _load_queue_for_marking(root: Path, date: str) -> tuple[Path, dict[str, Any]]:
    if date:
        path = queue_file_path(root, _validate_date(date))
        if not path.exists():
            raise CommandError(f"No queue file for {date}: {path}")
    else:
        path = queue_file_path(root, _today())
        if not path.exists():
            latest = latest_queue_file(root)
            if latest is None:
                raise CommandError("No morning_queue_*.json found in run_state/. Run morning-queue --execute first.")
            path = latest
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Queue file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise CommandError(
            f"Queue file {path} does not match the morning-queue schema (schema_version "
            f"{QUEUE_SCHEMA_VERSION}). If it is a hand-built file, regenerate it with morning-queue --execute."
        )
    items = data.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise CommandError(f"Queue file {path} has no valid items list.")
    return path, data


def _resolve_channel(channel: str) -> str:
    cleaned = str(channel or "").strip()
    if not cleaned:
        raise CommandError("--channel is required (e.g. sms, ig-dm, phone, email).")
    return CHANNEL_LABELS.get(cleaned.lower(), cleaned)


def _match_items(tokens: list[str], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_number: dict[int, dict[str, Any]] = {}
    for item in items:
        try:
            by_number[int(item.get("n"))] = item
        except (TypeError, ValueError):
            continue
    by_record = {str(item.get("record_id") or ""): item for item in items}
    valid_range = f"1..{len(items)}" if items else "none (queue is empty)"
    matched: list[dict[str, Any]] = []
    for token in tokens:
        if RECORD_ID_RE.match(token):
            item = by_record.get(token)
            if item is None:
                raise CommandError(f"Record id {token} is not in this queue file.")
        elif token.isdigit():
            item = by_number.get(int(token))
            if item is None:
                raise CommandError(f"Item {token} is not in this queue. Valid numbers: {valid_range}.")
        else:
            raise CommandError(f"'{token}' is neither an item number nor a record id. Valid numbers: {valid_range}.")
        if item not in matched:
            matched.append(item)
    return matched


def _append_outcome_row(config: dict[str, Any], row: dict[str, Any]) -> str:
    raw_path = str(config.get("outcomes_jsonl") or "").strip()
    if not raw_path:
        return "outcomes_jsonl not configured; send not logged to operator-agent STATE"
    path = _absolute(raw_path)
    key = (row.get("record_id"), row.get("channel"), row.get("date"))
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (existing.get("record_id"), existing.get("channel"), existing.get("date")) == key:
                return f"outcomes row already present in {path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return f"outcomes row appended to {path.name}"


def cmd_mark_sent(
    tokens: list[str],
    *,
    channel: str = "sms",
    date: str = "",
    dry_run: bool = False,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    config = load_queue_config(root_path)
    channel_label = _resolve_channel(channel)
    if not tokens:
        raise CommandError("Pass at least one item number or record id.")

    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /mark-sent: map {' '.join(tokens)} against today's queue file, PATCH Airtable "
            f"Status=Sent via {MARK_SENT_SCRIPT} with Outreach Channel={channel_label}, update queue file, "
            f"append operator-agent outcomes row."
        )

    path, data = _load_queue_for_marking(root_path, date)
    items = data["items"]
    matched = _match_items(tokens, items)

    lines: list[str] = []
    changed = False
    for item in matched:
        label = f"{item.get('n')}. {item.get('business') or item.get('record_id')}"
        record_id = str(item.get("record_id") or "")
        if not record_id:
            lines.append(f"[SKIP] {label}: no record_id in queue file")
            continue
        if item.get("sent_at"):
            lines.append(f"[ALREADY-SENT] {label} at {item.get('sent_at')} via {item.get('sent_channel')}")
            continue
        if dry_run:
            lines.append(f"[DRY-RUN] would mark {label} ({record_id}) Sent via {channel_label}")
            continue
        cmd = [str(python_path or sys.executable), MARK_SENT_SCRIPT, "--record-id", record_id, "--channel", channel_label]
        try:
            result = _run_capture(cmd, cwd=root_path, timeout_seconds=MARK_SENT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            lines.append(f"[FAIL] {label}: {MARK_SENT_SCRIPT} timed out")
            continue
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            lines.append(f"[FAIL] {label}: " + " / ".join(tail))
            continue
        sent_at = _local_now().isoformat()
        item["sent_at"] = sent_at
        item["sent_channel"] = channel_label
        changed = True
        # Row shape follows the SKILLS/send-queue log-step contract so
        # outreach Loop D can join feedback on record_id.
        outcome_note = _append_outcome_row(
            config,
            {
                "record_id": record_id,
                "business": "lpm",
                "vertical": item.get("vertical") or "",
                "contact": item.get("business") or "",
                "channel": channel_label,
                "date": str(data.get("date") or _today()),
                "event": "sent",
                "result": None,
                "sent_at": sent_at,
                "source": "hermes mark-sent",
            },
        )
        lines.append(f"[SENT] {label} ({record_id}) via {channel_label}; {outcome_note}")

    if changed:
        _atomic_write_json(path, data)
        lines.append(f"Queue file updated: {path}")
    return CommandResult("\n".join(lines))
