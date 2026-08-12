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
import fcntl
from contextlib import contextmanager
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
from lead_machine.sendability import is_contact_ready
from lead_machine.compliance import (
    ComplianceError,
    configured_suppression_source,
    has_acceptable_sms_consent,
    is_mark_sent_source_status,
    is_suppressed,
    load_suppression_index,
    record_suppression_identifiers,
    suppression_source_path,
)


QUEUE_SCHEMA_VERSION = 2
QUERY_SCRIPT = "query_ready_to_send.py"
MARK_SENT_SCRIPT = "mark_sent.py"
MARK_DECLINED_SCRIPT = "mark_declined.py"
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
    ("buyer_intent_signals", "Buyer Intent Signals"),
    ("send_trigger", "Send Trigger"),
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
    ("status", "Status"),
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


def mark_sent_lock_path(outcomes_path: Path) -> Path:
    return outcomes_path.with_name(f"{outcomes_path.name}.mark-sent.lock")


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


@contextmanager
def _mark_sent_lock(outcomes_path: Path):
    path = mark_sent_lock_path(outcomes_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise CommandError(f"Could not acquire shared mark-sent lock {path}: {exc}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


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


def _configured_outcomes_source(config: dict[str, Any]) -> str:
    """The ledger path this module reads and appends to.

    Deferred to `compliance.configured_suppression_source()` rather than reading
    LPM_OUTCOMES_JSONL directly, because that resolver prefers SUPPRESSION_SOURCE
    and every other write path already goes through it. Reading only
    LPM_OUTCOMES_JSONL meant an environment that set just SUPPRESSION_SOURCE had
    `mark_outcome.py` appending opt-outs to one file while the morning queue
    screened against another — no error on either side, because both parse fine.
    The tracked `config["outcomes_jsonl"]` default stays as the final fallback so
    an environment that names neither variable behaves exactly as before.
    """
    return configured_suppression_source() or str(config.get("outcomes_jsonl") or "").strip()


def _load_suppression_index(config: dict[str, Any]) -> tuple[set[str], str]:
    raw_path = _configured_outcomes_source(config)
    try:
        return load_suppression_index(raw_path), ""
    except ComplianceError as exc:
        return set(), str(exc).replace("suppression source is not configured", "outcomes_jsonl suppression source is not configured")


def _item_suppression_identifiers(item: dict[str, Any]) -> set[str]:
    return record_suppression_identifiers(item)


def _is_suppressed(item: dict[str, Any], suppression: set[str]) -> bool:
    return is_suppressed(item, suppression)


def _has_acceptable_sms_consent(item: dict[str, Any]) -> bool:
    channel = str(item.get("channel") or item.get("Outreach Channel") or "").strip().casefold()
    if channel != "sms":
        return True
    posture = item.get("consent_posture") or item.get("Consent Posture")
    return has_acceptable_sms_consent(posture)


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
            # Carried beside record_id rather than through ITEM_FIELD_MAP: the
            # rank is computed by `query_ready_to_send`, not read from a column,
            # so there is no Airtable field to map it from. It has to survive
            # into the frozen queue because this file is what `mark_sent` reads
            # when the rep types an item number — without it the dial reaches
            # the ledger unranked while hand-logged declines arrive ranked, and
            # a mean-vs-mean across that gap is the exact defect
            # `pre_dial_approval` exists to refuse. `None`, not 0.0: zero is a
            # rank, and the worst one.
            "dial_rank": record.get("dial_rank"),
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
        "sms_consent_excluded_count": 0,
        "readiness_excluded_count": 0,
        "status_excluded_count": 0,
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
    ready_records = [record for record in records if is_contact_ready(record)]
    payload["readiness_excluded_count"] = len(records) - len(ready_records)
    status_ready_records = [record for record in ready_records if is_mark_sent_source_status(record.get("Status"))]
    payload["status_excluded_count"] = len(ready_records) - len(status_ready_records)
    clear_records = [record for record in status_ready_records if not _is_suppressed(record, suppression)]
    payload["suppressed_excluded_count"] = len(status_ready_records) - len(clear_records)
    consented_records = [record for record in clear_records if _has_acceptable_sms_consent(record)]
    payload["sms_consent_excluded_count"] = len(clear_records) - len(consented_records)
    payload["items"] = _build_queue_items(
        consented_records,
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
        f"fetched {payload['fetched_count']}; not ready {payload['readiness_excluded_count']}; "
        f"suppressed {payload['suppressed_excluded_count']}; "
        f"SMS without consent {payload['sms_consent_excluded_count']}; "
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


def _load_writeback_state(root: Path, queue_run_id: str, *, queue_date: str = "") -> dict[str, Any]:
    path = queue_writeback_path(root, queue_run_id)
    if not path.exists():
        return {"schema_version": 2, "queue_run_id": queue_run_id, "operations": {}, "sent": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"Write-back state {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("queue_run_id") != queue_run_id or not isinstance(data.get("sent"), dict):
        raise CommandError(f"Write-back state {path} is malformed or belongs to another run.")
    if data.get("schema_version") == 1:
        migrated_sent: dict[str, Any] = {}
        for record_id, raw_entry in data["sent"].items():
            if not isinstance(raw_entry, dict):
                raise CommandError(f"Write-back state {path} has a malformed v1 Sent entry for {record_id}.")
            entry = dict(raw_entry)
            sent_date = str(entry.get("sent_date") or "").strip()
            if not sent_date and queue_date:
                sent_date = queue_date
            if not sent_date:
                sent_at = str(entry.get("sent_at") or "").strip()
                candidate = sent_at[:10]
                try:
                    sent_date = date_cls.fromisoformat(candidate).isoformat()
                except ValueError as exc:
                    raise CommandError(
                        f"Write-back state {path} cannot derive the v1 Sent date for {record_id}."
                    ) from exc
            entry["sent_date"] = sent_date
            if entry.get("sent_channel"):
                entry["event_id"] = _mark_sent_event_id(
                    record_id=str(record_id), channel=str(entry["sent_channel"]), send_date=sent_date
                )
            migrated_sent[str(record_id)] = entry
        return {
            "schema_version": 2,
            "queue_run_id": queue_run_id,
            "operations": {},
            "sent": migrated_sent,
        }
    if data.get("schema_version") != 2 or not isinstance(data.get("operations"), dict):
        raise CommandError(f"Write-back state {path} uses an unsupported schema.")
    return data


def _mark_sent_operation_id(
    *, queue_run_id: str, record_id: str, channel: str, send_date: str, artifact_sha256: str
) -> str:
    digest = _canonical_hash(
        {
            "queue_run_id": queue_run_id,
            "record_id": record_id,
            "channel": channel,
            "send_date": send_date,
            "artifact_sha256": artifact_sha256,
        }
    )
    return f"mark-sent-{digest}"


def _mark_sent_event_id(*, record_id: str, channel: str, send_date: str) -> str:
    digest = _canonical_hash(
        {
            "record_id": str(record_id or "").strip(),
            "channel": str(channel or "").strip().casefold(),
            "send_date": str(send_date or "").strip(),
        }
    )
    return f"sent-event-{digest}"


def _parse_mark_sent_receipt(result: subprocess.CompletedProcess[str], *, operation_id: str) -> dict[str, Any]:
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise CommandError(" / ".join(tail) or f"{MARK_SENT_SCRIPT} failed")
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise CommandError(f"{MARK_SENT_SCRIPT} returned no receipt for {operation_id}")
    try:
        receipt = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise CommandError(f"{MARK_SENT_SCRIPT} returned a malformed receipt for {operation_id}") from exc
    if not isinstance(receipt, dict) or receipt.get("result") not in {"applied", "already_applied"}:
        raise CommandError(f"{MARK_SENT_SCRIPT} returned an unvalidated receipt for {operation_id}")
    return receipt


def _resolved_suppression_path(config: dict[str, Any]) -> Path:
    raw = _configured_outcomes_source(config)
    try:
        return suppression_source_path(raw)
    except ComplianceError as exc:
        raise CommandError(f"Refusing mark-sent: current suppression state is unknown: {exc}.") from exc


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
    send_date: str,
    record_ids: list[str],
) -> dict[str, Any]:
    approval_id = f"msa-{uuid.uuid4().hex}"
    payload = {
        "schema_version": 2,
        "approval_id": approval_id,
        "created_at": _local_now().isoformat(),
        "queue_run_id": queue_run_id,
        "queue_sha256": queue_sha256,
        "artifact_sha256": artifact_sha256,
        "channel": channel,
        "send_date": send_date,
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
    send_date: str,
    queue_date: str,
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
    schema_version = approval.get("schema_version")
    if schema_version not in {1, 2}:
        changed.append("schema_version")
    elif schema_version == 2 or "send_date" in approval:
        if approval.get("send_date") != send_date:
            changed.append("send_date")
    elif not queue_date or send_date != queue_date:
        changed.append("send_date")
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


def _require_sms_consent(matched: list[dict[str, Any]], *, channel: str) -> None:
    if channel.casefold() != "sms":
        return
    missing = [
        str(item.get("record_id") or item.get("business") or "unknown")
        for item in matched
        if ("consent_posture" in item or "Consent Posture" in item)
        and not _has_acceptable_sms_consent({**item, "channel": "SMS"})
    ]
    if missing:
        raise CommandError(
            "Refusing mark-sent via SMS: documented consent is missing or unacceptable for " + ", ".join(missing)
        )


def _require_queue_status_eligible(matched: list[dict[str, Any]]) -> None:
    invalid = [
        str(item.get("record_id") or item.get("business") or "unknown")
        for item in matched
        if ("status" in item or "Status" in item)
        and not is_mark_sent_source_status(item.get("status") or item.get("Status"))
    ]
    if invalid:
        raise CommandError(
            "Refusing mark-sent: selected queue item is not in New status: "
            + ", ".join(invalid)
        )


def _read_outcome_rows(config: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    raw_path = _configured_outcomes_source(config)
    try:
        path = suppression_source_path(raw_path)
    except ComplianceError as exc:
        raise CommandError(f"Could not write required outcomes row: {exc}") from exc
    try:
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CommandError(f"Could not read required outcomes log {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(existing_lines, start=1):
        if not line.strip():
            continue
        try:
            existing = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Required outcomes log {path}:{line_number} is malformed") from exc
        if not isinstance(existing, dict):
            raise CommandError(f"Required outcomes log {path}:{line_number} must contain an object")
        rows.append(existing)
    return path, rows


def _row_event_id(row: dict[str, Any]) -> str:
    existing = str(row.get("event_id") or "").strip()
    if existing:
        return existing
    if not all(row.get(key) for key in ("record_id", "channel", "date")):
        return ""
    return _mark_sent_event_id(
        record_id=str(row.get("record_id")),
        channel=str(row.get("channel")),
        send_date=str(row.get("date")),
    )


def _matching_sent_outcome(config: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    _, rows = _read_outcome_rows(config)
    for row in rows:
        if str(row.get("event") or "").strip().casefold() == "sent" and _row_event_id(row) == event_id:
            return row
    return None


def _append_outcome_row(config: dict[str, Any], row: dict[str, Any]) -> str:
    # morning_queue.json carries a machine-absolute default; SUPPRESSION_SOURCE
    # (or LPM_OUTCOMES_JSONL) overrides it so other machines can redirect without
    # editing tracked config. See _configured_outcomes_source.
    path, existing_rows = _read_outcome_rows(config)
    operation_id = str(row.get("operation_id") or "").strip()
    event_id = str(row.get("event_id") or "").strip() or _mark_sent_event_id(
        record_id=str(row.get("record_id") or ""),
        channel=str(row.get("channel") or ""),
        send_date=str(row.get("date") or ""),
    )
    row["event_id"] = event_id
    artifact_sha256 = str(row.get("artifact_sha256") or "").strip()
    for existing in existing_rows:
        existing_event_id = _row_event_id(existing)
        if str(existing.get("event") or "sent").strip().casefold() == "sent" and existing_event_id == event_id:
            existing_artifact = str(existing.get("artifact_sha256") or "").strip()
            if artifact_sha256 and existing_artifact and artifact_sha256 != existing_artifact:
                raise CommandError(
                    "Refusing conflicting outcome artifacts for the same record, channel, and date."
                )
            return f"outcomes event already present in {path.name}"
        if operation_id and existing.get("operation_id") == operation_id:
            return f"outcomes row already present in {path.name}"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CommandError(f"Could not append required outcomes row to {path}: {exc}") from exc
    return f"outcomes row appended to {path.name}"


def _resolve_queue_selection(
    tokens: list[str],
    *,
    root_path: Path,
    queue_run_id: str,
    queue_sha256: str,
) -> list[dict[str, Any]]:
    """Map *tokens* to queue items, binding numbers to an immutable queue run.

    An item number means nothing without the run that served it, and binding one
    to the wrong run acts on the wrong business. Retiring the wrong lead is
    quieter than messaging the wrong one, which is exactly why it needs the same
    guard rather than a laxer one.

    Used by `cmd_mark_declined` only. `cmd_mark_sent` enforces the identical
    rule from its own inline copy, because it also needs the resolved run id,
    content hash and queue date that this returns nothing of. The two are
    behaviourally identical today and must stay so — a tightening applied to one
    does not reach the other. Said plainly because an earlier version of this
    docstring claimed they were already shared, which was the intent and not the
    code.

    This binding is load-bearing beyond wrong-lead protection. `cmd_mark_declined`
    has no preview-then-confirm step, and the Telegram route auto-appends
    `--execute`, so from a phone the PATCH is immediate and unreviewed. What
    makes that safe is that the selection cannot mis-resolve — not that a decline
    transmits nothing. Relax the bare-number refusal below and the missing
    preview becomes the only thing between a typo and a retired lead.
    """
    if not tokens:
        raise CommandError("Pass at least one item number or record id.")
    if any(token.isdigit() for token in tokens) or queue_run_id or queue_sha256:
        _, data = _load_queue_for_marking(root_path, queue_run_id, queue_sha256)
        return _match_items(tokens, data["items"])
    if not all(RECORD_ID_RE.match(token) for token in tokens):
        raise CommandError(
            "Bare item numbers are forbidden; use record IDs or bind numbers to an exact queue run."
        )
    return [
        {"n": None, "record_id": token, "business": token, "vertical": "", "dial_rank": None}
        for token in dict.fromkeys(tokens)
    ]


def cmd_mark_declined(
    tokens: list[str],
    *,
    reason: str = "",
    queue_run_id: str = "",
    queue_sha256: str = "",
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    """Record that the rep read a lead and chose not to call it.

    The verb the morning workflow was missing. `mark_sent` was reachable through
    Hermes and `mark_declined` was not, so the only observation that can push
    pre-dial approval *down* required remembering an out-of-band command the tool
    never mentioned — and a KPI whose adverse half depends on the rep's memory
    measures the rep's memory. Worse for the experiment: dials arrived carrying
    the rank and hand-logged declines carried whatever the rep typed, so the two
    sides of the falsifier were never ranked by the same path.

    No approval handshake, unlike mark-sent. That gate does two jobs, and only
    one of them is about sending: Gate 0's human-in-the-loop before a
    transmission, which a decline does not need, and a preview of what the
    tokens resolved to, which it would. The second is genuinely absent here —
    the Telegram route auto-appends `--execute`, so from a phone this PATCHes
    immediately. It is safe because `_resolve_queue_selection` makes the
    selection unable to mis-resolve, not because nothing is transmitted; see its
    docstring. Guarding it like a send anyway would tax the refusal and leave
    approval measuring how tedious refusing is.

    Re-declining is left to `mark_declined.py`, which only accepts `Status=New`
    and refuses a second pass on its own — the same reason there is no writeback
    journal here.
    """
    root_path = _absolute(root)
    why = str(reason or "").strip()
    if not why:
        raise CommandError(
            "Pass --reason. A refusal with no reason records that a lead was refused "
            "but not which signal was wrong, which is the only thing that can retire "
            "or restore a demoted blocker."
        )
    matched = _resolve_queue_selection(
        tokens, root_path=root_path, queue_run_id=queue_run_id, queue_sha256=queue_sha256
    )
    if any(not str(item.get("record_id") or "") for item in matched):
        raise CommandError("Every selected item must have an Airtable record ID.")

    labels = [
        f"{item.get('n') or 'record'}. {item.get('business') or item.get('record_id')}"
        for item in matched
    ]
    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /mark-declined: record a pre-dial refusal for {', '.join(labels)} "
            f"with reason {why!r}, PATCH Airtable Status=Not Fit via {MARK_DECLINED_SCRIPT}, "
            "and append one pre-dial decision row. Nothing is transmitted to anyone."
        )

    lines: list[str] = []
    for item, label in zip(matched, labels):
        record_id = str(item.get("record_id") or "")
        cmd = [
            str(python_path or sys.executable), MARK_DECLINED_SCRIPT,
            "--record-id", record_id,
            "--reason", why,
        ]
        served_rank = item.get("dial_rank")
        if served_rank is not None and str(served_rank).strip() != "":
            cmd += ["--rank", str(served_rank)]
        try:
            result = _run_capture(cmd, cwd=root_path, timeout_seconds=MARK_SENT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            lines.append(f"[FAIL] {label}: {MARK_DECLINED_SCRIPT} timed out")
            continue
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            lines.append(f"[FAIL] {label}: " + " ".join(tail))
            continue
        # `mark_declined` prints `[NOTE]` on every *successful* decline, saying
        # that no suppression was recorded and naming `mark_outcome --event
        # opt_out` as the command that does. It must reach the rep verbatim: the
        # opt-out screen it backs up is literal-substring matching, so a refusal
        # worded "they said never again" is not caught and this note is the only
        # thing standing between that and a business believing it opted out.
        # Never make it conditional.
        #
        # A reason that *does* trip the screen never gets here — `mark_declined`
        # raises before any network call and exits non-zero, so it lands in the
        # `[FAIL]` branch above.
        note = ""
        for line in (result.stderr or "").splitlines():
            if line.startswith("[NOTE]"):
                note = f" {line}"
        lines.append(f"[DECLINED] {label} ({record_id}) -> Airtable Status=Not Fit{note}")
    return CommandResult("\n".join(lines))


def cmd_mark_sent(
    tokens: list[str],
    *,
    channel: str = "sms",
    queue_run_id: str = "",
    queue_sha256: str = "",
    artifact_sha256: str = "",
    approval_id: str = "",
    date: str = "",
    dry_run: bool = False,
    execute: bool = False,
    root: Path | str = Path("."),
    python_path: Path | str | None = None,
) -> CommandResult:
    root_path = _absolute(root)
    config = load_queue_config(root_path)
    channel_label = _resolve_channel(channel)
    send_date = _validate_date(date) if date else _today()
    if not tokens:
        raise CommandError("Pass at least one item number or record id.")

    if not execute:
        return CommandResult(
            f"repo_root: {root_path}\n"
            f"preview /mark-sent: map {' '.join(tokens)} against an immutable queue run, PATCH Airtable "
            f"Status=Follow-Up via {MARK_SENT_SCRIPT} with Outreach Channel={channel_label} and "
            f"Last Contacted={send_date}, update separate "
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
        queue_date = str(data.get("date") or "")
    else:
        if not all(RECORD_ID_RE.match(token) for token in tokens):
            raise CommandError("Bare item numbers are forbidden; use record IDs or bind numbers to an exact queue run.")
        path = None
        data = {"items": []}
        matched = [{"n": None, "record_id": token, "business": token, "vertical": ""} for token in dict.fromkeys(tokens)]
        resolved_run_id = "record-ids-direct"
        resolved_queue_sha = _canonical_hash([item["record_id"] for item in matched])
        queue_date = ""

    record_ids = [str(item.get("record_id") or "") for item in matched]
    if any(not record_id for record_id in record_ids):
        raise CommandError("Every selected item must have an Airtable record ID.")
    suppression_path = _resolved_suppression_path(config)

    if dry_run:
        _require_current_suppression_clear(matched, config=config)
        _require_sms_consent(matched, channel=channel_label)
        _require_queue_status_eligible(matched)
        approval = _write_dry_run_approval(
            root_path,
            queue_run_id=resolved_run_id,
            queue_sha256=resolved_queue_sha,
            artifact_sha256=artifact_sha256,
            channel=channel_label,
            send_date=send_date,
            record_ids=record_ids,
        )
        lines = [
            f"[DRY-RUN] {item.get('n') or 'record'}. {item.get('business') or item.get('record_id')} "
            f"({item.get('record_id')}) -> Sent via {channel_label}; Airtable Status=Follow-Up"
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
        send_date=send_date,
        queue_date=queue_date,
        record_ids=record_ids,
    )

    lines: list[str] = []
    state_path = queue_writeback_path(root_path, resolved_run_id)
    with _mark_sent_lock(suppression_path):
        # Re-read inside the shared outcomes lock so attempts from different
        # immutable queue runs cannot race the external write or outcome log.
        _require_current_suppression_clear(matched, config=config)
        _require_sms_consent(matched, channel=channel_label)
        _require_queue_status_eligible(matched)
        writeback = _load_writeback_state(root_path, resolved_run_id, queue_date=queue_date)
        for item in matched:
            label = f"{item.get('n')}. {item.get('business') or item.get('record_id')}"
            record_id = str(item.get("record_id") or "")
            operation_id = _mark_sent_operation_id(
                queue_run_id=resolved_run_id,
                record_id=record_id,
                channel=channel_label,
                send_date=send_date,
                artifact_sha256=artifact_sha256,
            )
            event_id = _mark_sent_event_id(
                record_id=record_id,
                channel=channel_label,
                send_date=send_date,
            )
            prior = writeback["sent"].get(record_id)
            if prior:
                exact_prior = (
                    prior.get("sent_channel") == channel_label
                    and prior.get("sent_date") == send_date
                    and prior.get("artifact_sha256") == artifact_sha256
                )
                if not exact_prior:
                    raise CommandError(f"Refusing conflicting prior Sent state for {record_id}.")
                lines.append(f"[ALREADY-SENT] {label} at {prior.get('sent_at')} via {prior.get('sent_channel')}")
                continue

            existing_outcome = _matching_sent_outcome(config, event_id)
            if existing_outcome:
                existing_artifact = str(existing_outcome.get("artifact_sha256") or "").strip()
                if existing_artifact and existing_artifact != artifact_sha256:
                    raise CommandError(
                        f"Refusing conflicting outcome artifact for prior Sent event {record_id}."
                    )
                sent_at = str(existing_outcome.get("sent_at") or _local_now().isoformat())
                writeback["operations"][record_id] = {
                    "operation_id": operation_id,
                    "event_id": event_id,
                    "record_id": record_id,
                    "phase": "complete",
                    "channel": channel_label,
                    "send_date": send_date,
                    "approval_id": approval_id,
                    "artifact_sha256": artifact_sha256,
                    "airtable_result": "already_applied_from_outcome",
                    "completed_at": _local_now().isoformat(),
                }
                writeback["sent"][record_id] = {
                    "operation_id": operation_id,
                    "event_id": event_id,
                    "sent_at": sent_at,
                    "sent_date": send_date,
                    "sent_channel": channel_label,
                    "approval_id": approval_id,
                    "artifact_sha256": artifact_sha256,
                }
                _atomic_write_json(state_path, writeback)
                lines.append(f"[ALREADY-SENT] {label} at {sent_at} via {channel_label}; outcomes event already present")
                continue

            operation = writeback["operations"].get(record_id)
            if operation and operation.get("operation_id") != operation_id:
                raise CommandError(f"Refusing conflicting pending mark-sent operation for {record_id}.")
            if not operation:
                operation = {
                    "operation_id": operation_id,
                    "event_id": event_id,
                    "record_id": record_id,
                    "phase": "pending",
                    "channel": channel_label,
                    "send_date": send_date,
                    "approval_id": approval_id,
                    "artifact_sha256": artifact_sha256,
                    "created_at": _local_now().isoformat(),
                }
                writeback["operations"][record_id] = operation
                # The durable pending intent must exist before Airtable can be
                # mutated. Any local write failure aborts the operation here.
                _atomic_write_json(state_path, writeback)

            if operation.get("phase") == "pending":
                cmd = [
                    str(python_path or sys.executable), MARK_SENT_SCRIPT,
                    "--record-id", record_id,
                    "--channel", channel_label,
                    "--date", send_date,
                    "--suppression-source", str(suppression_path),
                ]
                # Only when the frozen queue actually carries one. A queue file
                # written before the rank was projected has none, and passing an
                # empty or zero value would write a fabricated number into the
                # ledger — worse than the gap, which `pre_dial_approval` already
                # reports as missing coverage rather than hiding.
                #
                # `--decisions-source` is deliberately not passed. `mark_sent`
                # falls back to $LPM_DIAL_DECISIONS_JSONL, which every reader and
                # writer resolves the same way; naming a path here instead would
                # let the queue record decisions into one file while the eval
                # computes the rate from another.
                served_rank = item.get("dial_rank")
                if served_rank is not None and str(served_rank).strip() != "":
                    cmd += ["--rank", str(served_rank)]
                try:
                    result = _run_capture(cmd, cwd=root_path, timeout_seconds=MARK_SENT_TIMEOUT_SECONDS)
                    receipt = _parse_mark_sent_receipt(result, operation_id=operation_id)
                except subprocess.TimeoutExpired:
                    lines.append(f"[FAIL] {label}: {MARK_SENT_SCRIPT} timed out; pending intent retained")
                    continue
                except CommandError as exc:
                    lines.append(f"[FAIL] {label}: {exc}; pending intent retained")
                    continue
                operation["phase"] = "airtable_applied"
                operation["airtable_result"] = receipt["result"]
                operation["airtable_confirmed_at"] = _local_now().isoformat()
                _atomic_write_json(state_path, writeback)

            if operation.get("phase") != "airtable_applied":
                raise CommandError(f"Write-back operation {operation_id} has invalid phase {operation.get('phase')!r}.")

            sent_at = str(operation.get("airtable_confirmed_at") or _local_now().isoformat())
            outcome_note = _append_outcome_row(
                config,
                {
                    "operation_id": operation_id,
                    "event_id": event_id,
                    "record_id": record_id,
                    "business": "lpm",
                    "vertical": item.get("vertical") or "",
                    "contact": item.get("business") or "",
                    "channel": channel_label,
                    "date": send_date,
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
            operation["phase"] = "complete"
            operation["completed_at"] = _local_now().isoformat()
            writeback["sent"][record_id] = {
                "operation_id": operation_id,
                "event_id": event_id,
                "sent_at": sent_at,
                "sent_date": send_date,
                "sent_channel": channel_label,
                "approval_id": approval_id,
                "artifact_sha256": artifact_sha256,
            }
            _atomic_write_json(state_path, writeback)
            lines.append(f"[SENT] {label} ({record_id}) via {channel_label}; {outcome_note}")

        if writeback["operations"] or writeback["sent"]:
            lines.append(f"Write-back state updated: {state_path}")
    return CommandResult("\n".join(lines))
