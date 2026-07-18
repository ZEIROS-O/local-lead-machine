#!/usr/bin/env python3
"""Morning send-queue verbs for the Hermes command layer.

Deterministic half of the morning outreach loop: pooled sendable counts,
budget-capped pipeline top-up, immutable morning queue runs and their dated
discovery indexes, and separate sent write-back. Opener drafting and digest prose live agent-side
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
import uuid
import hashlib
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


QUEUE_SCHEMA_VERSION = 2
QUERY_SCRIPT = "query_ready_to_send.py"
MARK_SENT_SCRIPT = "mark_sent.py"
CONFIG_ENV = "MORNING_QUEUE_CONFIG"
CONFIG_FILENAME = "morning_queue.json"
QUERY_TIMEOUT_SECONDS = 120
MARK_SENT_TIMEOUT_SECONDS = 60
TOPUP_TIMEOUT_MINUTES = 60
TOPUP_REPORT_MISSING = (
    "TOP-UP REPORT MISSING — the 06:30 job did not prove execution; existing pool only."
)

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
    ("metro_context", "Metro Context"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("website", "Website URL"),
    ("reviews", "Reviews"),
    ("rating", "Rating"),
    ("lead_priority", "Lead Priority"),
    ("score_total", "Score Total"),
    ("ai_confidence", "AI Confidence"),
    ("money_signal", "Money Signal"),
    ("buyer_intent_signals", "Buyer Intent Signals"),
    ("send_trigger", "Send Trigger"),
    ("best_service_angle", "Best Service Angle"),
    ("offer_fit", "Offer Fit"),
    ("specific_observation", "Specific Observation"),
    ("site_status", "Site Status"),
    ("stored_opener", "Opener"),
    ("safe_outreach_claims", "Safe Outreach Claims"),
    ("evidence_refs", "Evidence Refs"),
    ("evidence_snapshot", "Evidence Snapshot"),
    ("mockup_url", "Mockup URL"),
    ("video_url", "Video URL"),
    ("commercial_fit_state", "Commercial Fit State"),
    ("commercial_materiality", "Commercial Materiality"),
    ("recurring_consequence", "Recurring Consequence"),
    ("root_cause_confidence", "Root Cause Confidence"),
    ("offer_component", "LPM Offer Component"),
    ("packageable_solution", "Packageable Solution"),
    ("value_mechanism", "Value Mechanism"),
    ("beyond_free_fix", "Beyond Free Fix"),
    ("self_fix_risk", "Self Fix Risk"),
    ("intentionality", "Intentionality"),
    ("contact_eligibility", "Contact Eligibility"),
    ("asset_state", "Asset State"),
    ("promise_state", "Promise State"),
    ("asset_id", "Asset ID"),
    ("asset_verifier_verdict", "Asset Verifier Verdict"),
    ("channel", "Outreach Channel"),
    ("consent_posture", "Consent Posture"),
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


def queue_runs_dir(root: Path, date: str) -> Path:
    return root / "run_state" / "morning_queue_runs" / date


def queue_run_path(root: Path, date: str, queue_run_id: str) -> Path:
    return queue_runs_dir(root, date) / f"{queue_run_id}.json"


def queue_approval_path(root: Path, approval_id: str) -> Path:
    return root / "run_state" / "morning_queue_approvals" / f"{approval_id}.json"


def queue_writeback_path(root: Path, queue_run_id: str) -> Path:
    return root / "run_state" / "morning_queue_writebacks" / f"{queue_run_id}.json"


def latest_queue_file(root: Path) -> Path | None:
    """Return the newest dated discovery index for read-only queue status reporting."""
    state_dir = root / "run_state"
    if not state_dir.exists():
        return None
    candidates = sorted(state_dir.glob("morning_queue_*.json"))
    return candidates[-1] if candidates else None


def topup_file_path(root: Path, date: str) -> Path:
    return root / "run_state" / f"morning_topup_{date}.json"


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _queue_content_hash(payload: dict[str, Any]) -> str:
    return _canonical_hash({key: value for key, value in payload.items() if key != "queue_sha256"})


def _new_queue_run_id(queue_date: str) -> str:
    return f"mq-{queue_date.replace('-', '')}-{uuid.uuid4().hex}"


def _write_queue_index(root: Path, queue_date: str, run: dict[str, Any], path: Path) -> None:
    index: dict[str, Any] = {"schema_version": QUEUE_SCHEMA_VERSION, "date": queue_date, "runs": []}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict) and existing.get("schema_version") == QUEUE_SCHEMA_VERSION:
            index = existing
    runs = index.setdefault("runs", [])
    runs.append({
        "queue_run_id": run["queue_run_id"],
        "queue_sha256": run["queue_sha256"],
        "created_at": run["created_at"],
        "path": str(queue_run_path(root, queue_date, run["queue_run_id"]).relative_to(root)),
    })
    index["latest_queue_run_id"] = run["queue_run_id"]
    _atomic_write_json(path, index)


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


def _suppression_identifier(kind: str, value: Any) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    if kind == "phone":
        cleaned = "".join(character for character in cleaned if character.isdigit())
        if len(cleaned) == 11 and cleaned.startswith("1"):
            cleaned = cleaned[1:]
    else:
        cleaned = cleaned.casefold()
    return f"{kind}:{cleaned}" if cleaned else ""


def _load_suppression_index(config: dict[str, Any]) -> tuple[set[str], str]:
    raw_path = os.environ.get("LPM_OUTCOMES_JSONL", "").strip() or str(config.get("outcomes_jsonl") or "").strip()
    if not raw_path:
        return set(), "outcomes_jsonl suppression source is not configured"
    path = _absolute(raw_path)
    if not path.is_file():
        return set(), f"suppression source {path} is missing or unreadable"
    suppressed: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return set(), f"suppression source {path} is unreadable: {exc}"
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            return set(), f"suppression source {path}:{line_number} is malformed JSON: {exc.msg}"
        if not isinstance(row, dict):
            return set(), f"suppression source {path}:{line_number} must contain a JSON object"
        event = str(row.get("event") or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if event not in {"opt_out", "opted_out", "unsubscribe", "unsubscribed", "suppressed", "stop"}:
            continue
        identifiers = {
            identifier
            for key in ("record_id", "phone", "email")
            if (identifier := _suppression_identifier(key, row.get(key)))
        }
        if not identifiers:
            return set(), f"suppression source {path}:{line_number} has a suppression event without an identifier"
        suppressed.update(identifiers)
    return suppressed, ""


def _item_suppression_identifiers(item: dict[str, Any]) -> set[str]:
    values = {
        "record_id": item.get("record_id"),
        "phone": item.get("phone") or item.get("Phone"),
        "email": item.get("email") or item.get("Email"),
    }
    return {
        identifier
        for key in ("record_id", "phone", "email")
        if (identifier := _suppression_identifier(key, values[key]))
    }


def _is_suppressed(item: dict[str, Any], suppression: set[str]) -> bool:
    return bool(_item_suppression_identifiers(item) & suppression)


def _compliance_annotations(item: dict[str, Any], suppression: set[str]) -> dict[str, str]:
    channel = str(item.get("channel") or "").strip()
    channel_key = channel.casefold()
    suppression_status = "suppressed" if _is_suppressed(item, suppression) else "clear"

    if channel_key == "phone":
        consent = "manual-business-call-not-sms"
        opt_out = "honor-do-not-call-and-stop"
    elif channel_key in {"instagram dm", "dm"}:
        consent = "manual-platform-message"
        opt_out = "honor-stop-and-platform-suppression"
    elif channel_key == "email":
        consent = "cold-business-email"
        opt_out = "requires-unsubscribe-and-physical-address"
    elif channel_key == "sms":
        consent = str(item.get("consent_posture") or "unknown").strip()
        opt_out = "honor-stop-and-suppression"
    else:
        consent = "unknown"
        opt_out = "unknown"

    vertical = str(item.get("vertical") or "local service").replace("_", " ")
    return {
        "suppression_status": suppression_status,
        "consent_posture": consent,
        "opt_out_posture": opt_out,
        "person_read": f"{vertical} owner/operator; practical buyer; sophistication unknown",
        "state_read": "unknown; do not infer mood or urgency",
        "status_read": "cold stranger; buyer controls attention; use a level non-needy posture",
        "moment_read": f"first manual {channel or 'unknown-channel'} touch; thin attention; lead with verified evidence",
    }


def _build_queue_items(
    records: list[dict[str, Any]],
    *,
    target: int,
    overflow_cap: int,
    suppression: set[str] | None = None,
) -> list[dict[str, Any]]:
    ab_count = sum(
        1 for record in records if str(record.get("Lead Priority") or "").strip().upper() in {"A", "B"}
    )
    take = min(max(target, ab_count), max(1, overflow_cap), len(records))
    suppression = suppression or set()
    items: list[dict[str, Any]] = []
    for index, record in enumerate(records[:take], start=1):
        item: dict[str, Any] = {
            "n": index,
            "record_id": str(record.get("record_id") or ""),
            "vertical": resolve_vertical_label(str(record.get("Vertical") or "")) or "unknown",
        }
        for key, field in ITEM_FIELD_MAP:
            item[key] = record.get(field, "")
        item.update(_compliance_annotations(item, suppression))
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


def _topup_report(
    telemetry: dict[str, Any] | None,
    *,
    queue_date: str,
    topup_enabled: bool,
) -> dict[str, Any]:
    """Classify today's 06:30 telemetry before the digest agent sees it.

    Missing, malformed, stale, or stage-invalid telemetry intentionally collapse
    to one fail-closed warning. A present report is evidence of execution only;
    it is never permission to replenish.
    """
    stage = "v1b" if topup_enabled else "v1a"
    missing = {
        "status": "missing",
        "stage": stage,
        "produced": False,
        "message": TOPUP_REPORT_MISSING,
    }
    if not isinstance(telemetry, dict):
        return missing
    if telemetry.get("date") != queue_date:
        return missing
    if not isinstance(telemetry.get("generated_at"), str) or not telemetry["generated_at"].strip():
        return missing
    if type(telemetry.get("ran")) is not bool:
        return missing
    if type(telemetry.get("target")) is not int or telemetry["target"] <= 0:
        return missing
    budget = telemetry.get("budget_usd")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0:
        return missing

    if not topup_enabled:
        if telemetry["ran"] is not False or telemetry.get("reason") != "topup disabled in config":
            return missing
        return {
            "status": "present",
            "stage": stage,
            "produced": True,
            "message": "06:30 top-up telemetry present — ran: false; reason: topup disabled in config; spend: $0.00.",
        }

    if telemetry["ran"] is False:
        if telemetry.get("reason") != "target met":
            return missing
        pooled = telemetry.get("pooled_sendable_before")
        if type(pooled) is not int or pooled < telemetry["target"]:
            return missing
        return {
            "status": "present",
            "stage": stage,
            "produced": True,
            "message": "06:30 top-up telemetry present — ran: false; reason: target met; spend: $0.00.",
        }

    required_run_fields = ("outcome", "vertical", "market", "size", "detail", "spend_usd")
    if any(field not in telemetry for field in required_run_fields):
        return missing
    spend = telemetry.get("spend_usd")
    if isinstance(spend, bool) or not isinstance(spend, (int, float)) or spend < 0:
        return missing
    if any(not str(telemetry.get(field) or "").strip() for field in required_run_fields[:-1]):
        return missing
    return {
        "status": "present",
        "stage": stage,
        "produced": True,
        "message": (
            "06:30 top-up telemetry present — "
            f"ran: true; outcome: {telemetry['outcome']}; vertical: {telemetry['vertical']}; "
            f"size: {telemetry['size']}; spend: ${float(spend):.2f}."
        ),
    }


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
    index_path = queue_file_path(root_path, queue_date)

    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /morning-queue: pooled Airtable query -> top max({resolved_target}, all A+B) capped at "
            f"{overflow_cap} -> create a new immutable run under {queue_runs_dir(root_path, queue_date)} "
            f"and append its identity to {index_path}."
        )

    topup = _load_topup_telemetry(root_path, queue_date)
    topup_report = _topup_report(
        topup,
        queue_date=queue_date,
        topup_enabled=bool(config.get("topup_enabled")),
    )
    queue_run_id = _new_queue_run_id(queue_date)
    created_at = _local_now().isoformat()
    payload: dict[str, Any] = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "queue_run_id": queue_run_id,
        "created_at": created_at,
        "date": queue_date,
        "generated_at": created_at,
        "target": resolved_target,
        "pooled_sendable": 0,
        "fetched_count": 0,
        "suppressed_excluded_count": 0,
        "queued_count": 0,
        "per_vertical": {},
        "topup": topup,
        "topup_report": topup_report,
        "error": None,
        "items": [],
    }
    try:
        records = fetch_pooled_records(root=root_path, python_path=python_path)
        payload["pooled_sendable"] = len(records)
        payload["fetched_count"] = len(records)
        suppression, suppression_error = _load_suppression_index(config)
        if suppression_error:
            raise CommandError(suppression_error)
    except CommandError as exc:
        # Degraded, never silent: write the dated file with zero items so
        # mark-sent can never map numbers against a stale day.
        payload["error"] = str(exc)
        payload["queue_sha256"] = _queue_content_hash(payload)
        run_path = queue_run_path(root_path, queue_date, queue_run_id)
        _atomic_write_json(run_path, payload)
        _write_queue_index(root_path, queue_date, payload, index_path)
        return CommandResult(
            f"morning-queue DEGRADED: compliance/query input failed; wrote immutable run {run_path} with 0 items.\n"
            f"queue_run_id: {queue_run_id}\nqueue_sha256: {payload['queue_sha256']}\nError: {exc}"
        )

    payload["per_vertical"] = per_vertical_counts(records)
    clear_records = [record for record in records if not _is_suppressed(record, suppression)]
    payload["suppressed_excluded_count"] = len(records) - len(clear_records)
    payload["items"] = _build_queue_items(
        clear_records,
        target=resolved_target,
        overflow_cap=overflow_cap,
        suppression=suppression,
    )
    payload["queued_count"] = len(payload["items"])
    payload["queue_sha256"] = _queue_content_hash(payload)
    run_path = queue_run_path(root_path, queue_date, queue_run_id)
    _atomic_write_json(run_path, payload)
    _write_queue_index(root_path, queue_date, payload, index_path)
    return CommandResult(
        f"morning-queue immutable run written: {run_path}\n"
        f"queue_run_id: {queue_run_id}\nqueue_sha256: {payload['queue_sha256']}\n"
        f"fetched {payload['fetched_count']}; excluded {payload['suppressed_excluded_count']}; "
        f"queued {payload['queued_count']} "
        f"(target {resolved_target}, cap {overflow_cap}).\n"
        f"top-up report: {topup_report['message']}"
    )


# ------------------------------------------------------------------- mark-sent


def _load_queue_for_marking(root: Path, queue_run_id: str, queue_sha256: str) -> tuple[Path, dict[str, Any]]:
    if not queue_run_id or not queue_sha256:
        raise CommandError("Item numbers require the exact --queue-run-id and --queue-sha256 from the delivered digest.")
    candidates = list((root / "run_state" / "morning_queue_runs").glob(f"*/{queue_run_id}.json"))
    if len(candidates) != 1:
        raise CommandError(f"Queue run {queue_run_id!r} was not found exactly once.")
    path = candidates[0]
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
    if data.get("queue_run_id") != queue_run_id:
        raise CommandError("Queue run ID does not match immutable artifact content.")
    actual_hash = _queue_content_hash(data)
    if data.get("queue_sha256") != actual_hash or queue_sha256 != actual_hash:
        raise CommandError("Queue hash is missing, changed, or does not match the immutable artifact.")
    return path, data


def _load_writeback_state(root: Path, queue_run_id: str) -> dict[str, Any]:
    path = queue_writeback_path(root, queue_run_id)
    if not path.exists():
        return {"schema_version": 1, "queue_run_id": queue_run_id, "sent": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Write-back state {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("queue_run_id") != queue_run_id or not isinstance(data.get("sent"), dict):
        raise CommandError(f"Write-back state {path} is malformed or belongs to another run.")
    return data


def _validate_sha256(label: str, value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise CommandError(f"{label} must be an exact 64-character SHA-256 hash.")
    return cleaned


def _write_dry_run_approval(
    root: Path,
    *,
    queue_run_id: str,
    queue_sha256: str,
    artifact_sha256: str,
    channel: str,
    record_ids: list[str],
) -> dict[str, Any]:
    approval_id = f"msa-{uuid.uuid4().hex}"
    payload = {
        "schema_version": 1,
        "approval_id": approval_id,
        "created_at": _local_now().isoformat(),
        "queue_run_id": queue_run_id,
        "queue_sha256": queue_sha256,
        "artifact_sha256": artifact_sha256,
        "channel": channel,
        "record_ids": record_ids,
    }
    payload["approval_sha256"] = _canonical_hash(payload)
    _atomic_write_json(queue_approval_path(root, approval_id), payload)
    return payload


def _require_matching_approval(
    root: Path,
    approval_id: str,
    *,
    queue_run_id: str,
    queue_sha256: str,
    artifact_sha256: str,
    channel: str,
    record_ids: list[str],
) -> dict[str, Any]:
    if not approval_id:
        raise CommandError("Live write-back requires --approval-id from the clean dry-run of this exact artifact.")
    path = queue_approval_path(root, approval_id)
    if not path.is_file():
        raise CommandError(f"Dry-run approval {approval_id!r} does not exist.")
    try:
        approval = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Dry-run approval {approval_id!r} is malformed: {exc}") from exc
    expected = {
        "queue_run_id": queue_run_id,
        "queue_sha256": queue_sha256,
        "artifact_sha256": artifact_sha256,
        "channel": channel,
        "record_ids": record_ids,
    }
    changed = [key for key, value in expected.items() if approval.get(key) != value]
    stored_hash = approval.get("approval_sha256")
    actual_hash = _canonical_hash({key: value for key, value in approval.items() if key != "approval_sha256"})
    if stored_hash != actual_hash:
        changed.append("approval_sha256")
    if changed:
        raise CommandError("Refusing live write-back: dry-run approval mismatch in " + ", ".join(changed) + ".")
    return approval


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


def _require_current_suppression_clear(
    matched: list[dict[str, Any]],
    *,
    config: dict[str, Any],
) -> None:
    suppression, error = _load_suppression_index(config)
    if error:
        raise CommandError(f"Refusing mark-sent: current suppression state is unknown: {error}.")
    unknown = [str(item.get("record_id") or "") for item in matched if not _item_suppression_identifiers(item)]
    if unknown:
        raise CommandError("Refusing mark-sent: selected queue item has no suppression identity: " + ", ".join(unknown))
    suppressed = [str(item.get("record_id") or item.get("business") or "unknown") for item in matched if _is_suppressed(item, suppression)]
    if suppressed:
        raise CommandError(
            "Refusing mark-sent: selected item is currently suppressed or opted out: " + ", ".join(suppressed)
        )


def _append_outcome_row(config: dict[str, Any], row: dict[str, Any]) -> str:
    # morning_queue.json carries a machine-absolute default; LPM_OUTCOMES_JSONL
    # overrides it so other machines can redirect without editing tracked config.
    raw_path = os.environ.get("LPM_OUTCOMES_JSONL", "").strip() or str(config.get("outcomes_jsonl") or "").strip()
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
    queue_run_id: str = "",
    queue_sha256: str = "",
    artifact_sha256: str = "",
    approval_id: str = "",
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
            f"preview /mark-sent: map {' '.join(tokens)} against an immutable queue run, PATCH Airtable "
            f"Status=Sent via {MARK_SENT_SCRIPT} with Outreach Channel={channel_label}, update separate "
            f"write-back state, and append one idempotent operator-agent outcomes row."
        )

    artifact_sha256 = _validate_sha256("--artifact-sha256", artifact_sha256)
    has_numbers = any(token.isdigit() for token in tokens)
    if has_numbers or queue_run_id or queue_sha256:
        path, data = _load_queue_for_marking(root_path, queue_run_id, queue_sha256)
        items = data["items"]
        matched = _match_items(tokens, items)
        resolved_run_id = str(data["queue_run_id"])
        resolved_queue_sha = str(data["queue_sha256"])
        queue_date = str(data.get("date") or _today())
    else:
        if not all(RECORD_ID_RE.match(token) for token in tokens):
            raise CommandError("Bare item numbers are forbidden; use record IDs or bind numbers to an exact queue run.")
        path = None
        data = {"items": []}
        matched = [{"n": None, "record_id": token, "business": token, "vertical": ""} for token in dict.fromkeys(tokens)]
        resolved_run_id = "record-ids-direct"
        resolved_queue_sha = _canonical_hash([item["record_id"] for item in matched])
        queue_date = _today()

    record_ids = [str(item.get("record_id") or "") for item in matched]
    if any(not record_id for record_id in record_ids):
        raise CommandError("Every selected item must have an Airtable record ID.")
    # Re-read the authority source for both dry-run and live execution. This
    # catches opt-outs written after queue generation or after approval.
    _require_current_suppression_clear(matched, config=config)
    writeback = _load_writeback_state(root_path, resolved_run_id)

    if dry_run:
        approval = _write_dry_run_approval(
            root_path,
            queue_run_id=resolved_run_id,
            queue_sha256=resolved_queue_sha,
            artifact_sha256=artifact_sha256,
            channel=channel_label,
            record_ids=record_ids,
        )
        lines = [
            f"[DRY-RUN] {item.get('n') or 'record'}. {item.get('business') or item.get('record_id')} "
            f"({item.get('record_id')}) -> Sent via {channel_label}"
            for item in matched
        ]
        lines.append(f"approval_id: {approval['approval_id']}")
        lines.append(f"approval_sha256: {approval['approval_sha256']}")
        return CommandResult("\n".join(lines))

    _require_matching_approval(
        root_path,
        approval_id,
        queue_run_id=resolved_run_id,
        queue_sha256=resolved_queue_sha,
        artifact_sha256=artifact_sha256,
        channel=channel_label,
        record_ids=record_ids,
    )

    lines: list[str] = []
    changed = False
    for item in matched:
        label = f"{item.get('n')}. {item.get('business') or item.get('record_id')}"
        record_id = str(item.get("record_id") or "")
        if not record_id:
            lines.append(f"[SKIP] {label}: no record_id in queue file")
            continue
        prior = writeback["sent"].get(record_id)
        if prior:
            lines.append(f"[ALREADY-SENT] {label} at {prior.get('sent_at')} via {prior.get('sent_channel')}")
            continue
        cmd = [
            str(python_path or sys.executable), MARK_SENT_SCRIPT,
            "--record-id", record_id,
            "--channel", channel_label,
            "--date", queue_date,
        ]
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
        writeback["sent"][record_id] = {
            "sent_at": sent_at,
            "sent_channel": channel_label,
            "approval_id": approval_id,
            "artifact_sha256": artifact_sha256,
        }
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
                "date": queue_date,
                "event": "sent",
                "result": None,
                "sent_at": sent_at,
                "source": "hermes mark-sent",
                "queue_run_id": resolved_run_id,
                "queue_sha256": resolved_queue_sha,
                "artifact_sha256": artifact_sha256,
                "approval_id": approval_id,
            },
        )
        lines.append(f"[SENT] {label} ({record_id}) via {channel_label}; {outcome_note}")

    if changed:
        state_path = queue_writeback_path(root_path, resolved_run_id)
        _atomic_write_json(state_path, writeback)
        lines.append(f"Write-back state updated: {state_path}")
    return CommandResult("\n".join(lines))
