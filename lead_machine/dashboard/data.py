from __future__ import annotations

import csv
import io
import json
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from lead_machine.hermes_batch import BatchPaths, BatchResolutionError, resolve_batch_paths
from lead_machine.hermes_state import current_sync_hash, default_state
from lead_machine.sendability import (
    PACKET_TIER_MANUAL,
    PACKET_TIER_NONE,
    PACKET_TIER_SENDABLE,
    PACKET_TIER_WATCHLIST,
    contact_readiness_failures,
    is_contact_ready,
)


PROGRESS_ORDER = {
    "configured": 0,
    "searched": 1,
    "enriched": 2,
    "partial_scoring": 3,
    "scored": 4,
    "packets_current": 5,
    "airtable_ready": 6,
    "finished": 7,
}
HEALTH_ORDER = {"blocked": 0, "attention": 1, "healthy": 2}
YES_VALUES = {"yes", "true", "1"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_timestamp(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _yes(value: Any) -> bool:
    return str(value or "").strip().casefold() in YES_VALUES


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _stable_text(path: Path) -> tuple[str, str]:
    """Read a file without returning a torn write when a pipeline is updating it."""
    for _ in range(2):
        try:
            before = path.stat()
            text = path.read_text(encoding="utf-8", errors="replace")
            after = path.stat()
        except OSError as exc:
            return "", str(exc)
        if before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size:
            return text, ""
    return "", "file changed while it was being read"


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, ""
    text, error = _stable_text(path)
    if error:
        return {}, error
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON: {exc.msg}"
    if not isinstance(value, dict):
        return {}, "JSON root is not an object"
    return value, ""


def _read_csv(path: Path) -> tuple[list[dict[str, str]], str]:
    if not path.is_file():
        return [], ""
    text, error = _stable_text(path)
    if error:
        return [], error
    try:
        # Enrichment evidence can legitimately exceed Python's conservative
        # 128 KiB default CSV cell limit (for example rendered page text).
        csv.field_size_limit(sys.maxsize)
        return list(csv.DictReader(io.StringIO(text))), ""
    except (csv.Error, UnicodeError) as exc:
        return [], f"invalid CSV: {exc}"


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int, str]:
    if not path.is_file():
        return [], 0, ""
    text, error = _stable_text(path)
    if error:
        return [], 0, error
    rows: list[dict[str, Any]] = []
    invalid = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            invalid += 1
    return rows, invalid, ""


def _row_key(row: dict[str, Any]) -> str:
    value = _first(row, "Place Key", "place_key", "Place ID", "place_id")
    if value:
        return str(value).strip()
    name = str(_first(row, "Business Name", "business_name")).strip().casefold()
    city = str(_first(row, "City", "city")).strip().casefold()
    return f"fallback:{name}|{city}" if name or city else ""


def _fallback_row_key(row: dict[str, Any]) -> str:
    name = str(_first(row, "Business Name", "business_name")).strip().casefold()
    city = str(_first(row, "City", "city")).strip().casefold()
    return f"fallback:{name}|{city}" if name or city else ""


def _enriched_google(row: dict[str, Any]) -> dict[str, Any]:
    google = row.get("google")
    return google if isinstance(google, dict) else row


def _enriched_key(row: dict[str, Any]) -> str:
    return _row_key(_enriched_google(row))


def _identity_counts(keys: Iterable[str]) -> tuple[Counter[str], int]:
    cleaned = [str(key or "").strip() for key in keys]
    return Counter(key for key in cleaned if key), sum(1 for key in cleaned if not key)


def _tier_counts(rows: Iterable[dict[str, str]]) -> dict[str, int]:
    counts = {
        "sendable": 0,
        "manual_review": 0,
        "watchlist": 0,
        "no_packet": 0,
    }
    for row in rows:
        tier = str(row.get("Packet Tier") or "").strip()
        if tier == PACKET_TIER_SENDABLE:
            counts["sendable"] += 1
        elif tier == PACKET_TIER_MANUAL:
            counts["manual_review"] += 1
        elif tier == PACKET_TIER_WATCHLIST:
            counts["watchlist"] += 1
        elif tier == PACKET_TIER_NONE:
            counts["no_packet"] += 1
    return counts


def _typed_int(
    source: dict[str, Any], key: str, issues: list[dict[str, str]], *, scope: str, default: int = 0
) -> int:
    if key not in source or source.get(key) is None:
        return default
    value = source.get(key)
    if isinstance(value, bool):
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be an integer."))
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be an integer."))
        return default
    if isinstance(value, float) and not value.is_integer():
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be an integer."))
        return default
    if parsed < 0:
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be nonnegative."))
        return default
    return parsed


def _typed_float(
    source: dict[str, Any], key: str, issues: list[dict[str, str]], *, scope: str, default: float = 0.0
) -> float:
    if key not in source or source.get(key) is None:
        return default
    value = source.get(key)
    if isinstance(value, bool):
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be numeric."))
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be numeric."))
        return default
    if not math.isfinite(parsed) or parsed < 0:
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be finite and nonnegative."))
        return default
    return parsed


def _typed_bool(
    source: dict[str, Any], key: str, issues: list[dict[str, str]], *, scope: str, default: bool = False
) -> bool:
    if key not in source or source.get(key) is None:
        return default
    value = source.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be boolean."))
    return default


def _typed_string_list(
    source: dict[str, Any], key: str, issues: list[dict[str, str]], *, scope: str
) -> list[str]:
    if key not in source or source.get(key) is None:
        return []
    value = source.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        issues.append(_issue(f"invalid_{scope}_{key.lower()}", "blocked", f"{scope} field {key} must be a string list."))
        return []
    return value


def _path_meta(path: Path, root: Path, *, rows: int | None = None) -> dict[str, Any]:
    exists = path.exists()
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = str(path)
    return {
        "path": relative,
        "exists": exists,
        "rows": rows,
        "size_bytes": path.stat().st_size if exists and path.is_file() else None,
        "updated_at": _iso_timestamp(path.stat().st_mtime) if exists else "",
    }


def _latest_mtime(paths: Iterable[Path]) -> float:
    values: list[float] = []
    for path in paths:
        try:
            values.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(values, default=0.0)


def _issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _activity_sort_value(value: Any) -> float:
    parsed = _parse_iso(value)
    return parsed.timestamp() if parsed else 0.0


@dataclass
class BatchRead:
    summary: dict[str, Any]
    scored_rows: list[dict[str, str]]
    enriched_rows: list[dict[str, Any]]


class DashboardRepository:
    """Build source-backed, read-only dashboard snapshots from pipeline artifacts."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self._cache_signature: tuple[Any, ...] | None = None
        self._cache_reads: dict[str, BatchRead] = {}
        self._cache_ignored = 0

    def _candidate_names(self) -> tuple[set[str], int]:
        names: set[str] = set()
        raw_candidates: set[str] = set()
        patterns = (
            ("search_configs/*.json", "", ""),
            ("run_state/*.json", "", ""),
            ("output/enriched_*.jsonl", "enriched_", ""),
            ("output/scored_*_api.csv", "scored_", "_api"),
            ("output/scored_*_api_tiered.csv", "scored_", "_api_tiered"),
            ("output/scored_*.csv", "scored_", ""),
            ("packets_*/packet_index.csv", "packets_", ""),
        )
        for pattern, prefix, suffix in patterns:
            for path in self.root.glob(pattern):
                if path.name == "packet_index.csv":
                    candidate = path.parent.name.removeprefix(prefix)
                else:
                    candidate = path.stem
                    if prefix and candidate.startswith(prefix):
                        candidate = candidate[len(prefix):]
                    if suffix and candidate.endswith(suffix):
                        candidate = candidate[: -len(suffix)]
                    elif pattern == "output/scored_*.csv" and candidate.endswith(("_api", "_api_tiered")):
                        continue
                raw_candidates.add(candidate)
        for candidate in raw_candidates:
            try:
                resolve_batch_paths(candidate, root=self.root)
            except BatchResolutionError:
                continue
            names.add(candidate)
        return names, len(raw_candidates - names)

    def _signature(self, names: Iterable[str]) -> tuple[Any, ...]:
        signature: list[Any] = []
        for name in sorted(names):
            paths = resolve_batch_paths(name, root=self.root)
            candidates = self._artifact_paths(paths)
            signature.append(name)
            for path in candidates:
                try:
                    stat = path.stat()
                    signature.extend((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    continue
        return tuple(signature)

    def _artifact_paths(self, paths: BatchPaths) -> list[Path]:
        return [
            paths.config_path,
            paths.raw_csv,
            paths.deduped_csv,
            paths.enriched,
            paths.scored_csv,
            paths.tiered_csv,
            self.root / f"output/scored_{paths.batch}.csv",
            paths.packet_index,
            paths.usage_json,
            paths.state_json,
        ]

    def _scan(self, *, force: bool = False) -> tuple[dict[str, BatchRead], int]:
        names, ignored = self._candidate_names()
        signature = self._signature(names)
        if not force and signature == self._cache_signature:
            return self._cache_reads, self._cache_ignored
        reads: dict[str, BatchRead] = {}
        for name in sorted(names):
            try:
                reads[name] = self._read_batch(name)
            except Exception as exc:  # per-batch containment is an API integrity boundary
                reads[name] = self._blocked_batch(name, exc)
        self._cache_signature = signature
        self._cache_reads = reads
        self._cache_ignored = ignored
        return reads, ignored

    def _blocked_batch(self, batch: str, exc: Exception) -> BatchRead:
        paths = resolve_batch_paths(batch, root=self.root)
        issue = _issue("unexpected_batch_read_failure", "blocked", f"Batch artifacts could not be reconciled: {exc}.")
        summary = {
            "batch": batch,
            "vertical": paths.vertical,
            "market": paths.market,
            "size": paths.size,
            "sequence": paths.seq,
            "location": paths.market.replace("_", " ").title(),
            "query_count": 0,
            "progress": "configured",
            "progress_rank": PROGRESS_ORDER["configured"],
            "health": "blocked",
            "health_rank": HEALTH_ORDER["blocked"],
            "counts": {"raw": 0, "deduped": 0, "enriched": 0, "scored": 0, "unscored": 0, "packets": 0, "sendable": 0, "manual_review": 0, "watchlist": 0, "no_packet": 0},
            "stages": {key: {"status": "blocked" if key != "discovery" else "not_started", "count": 0} for key in ("discovery", "enrichment", "scoring", "packets", "airtable")},
            "issues": [issue],
            "latest_activity_at": "",
            "next_action": f"/status {batch}",
            "pending_approval": {"active": False, "stage": "", "expires_at": ""},
            "llm_usage": {"available": False, "calls": 0, "call_cap": 0, "tokens": 0, "token_cap": 0, "estimated_usd": 0.0, "usd_cap": 0.0, "stopped_by_cap": False, "stop_reason": "", "updated_at": ""},
            "airtable": {"dry_run": {"status": "not_run", "timestamp": "", "post": 0, "patch": 0, "skip": 0, "warnings": []}, "live": {"status": "not_run", "timestamp": "", "post": 0, "patch": 0, "skip": 0}, "current_input_hash": ""},
            "artifacts": {},
        }
        return BatchRead(summary=summary, scored_rows=[], enriched_rows=[])

    def _read_batch(self, batch: str) -> BatchRead:
        paths = resolve_batch_paths(batch, root=self.root)
        issues: list[dict[str, str]] = []

        config, config_error = _read_json(paths.config_path)
        if config_error:
            issues.append(_issue("invalid_config", "blocked", f"Search config could not be read: {config_error}."))

        raw_rows, raw_error = _read_csv(paths.raw_csv)
        deduped_rows, deduped_error = _read_csv(paths.deduped_csv)
        enriched_rows, invalid_enriched, enriched_error = _read_jsonl(paths.enriched)
        plain_scored = self.root / f"output/scored_{batch}.csv"
        base_scored_path = paths.scored_csv if paths.scored_csv.exists() else plain_scored
        scored_rows, scored_error = _read_csv(base_scored_path)

        if paths.tiered_csv.exists():
            tiered_path = paths.tiered_csv
        elif base_scored_path == plain_scored and plain_scored.exists():
            tiered_path = plain_scored
        else:
            tiered_path = paths.tiered_csv
        tiered_rows, tiered_error = _read_csv(tiered_path)
        packet_rows, packet_error = _read_csv(paths.packet_index)
        loaded_state, state_error = _read_json(paths.state_json)
        state = default_state(paths)
        if not state_error:
            state.update(loaded_state)
        elif paths.state_json.exists():
            issues.append(_issue("invalid_run_state", "blocked", f"Run state could not be read: {state_error}."))
        usage, usage_error = _read_json(paths.usage_json)

        for code, label, error in (
            ("invalid_raw_csv", "Raw Places CSV", raw_error),
            ("invalid_deduped_csv", "Deduplicated CSV", deduped_error),
            ("invalid_enriched_jsonl", "Enriched JSONL", enriched_error),
            ("invalid_scored_csv", "Scored CSV", scored_error),
            ("invalid_tiered_csv", "Tiered CSV", tiered_error),
            ("invalid_packet_index", "Packet index", packet_error),
            ("invalid_llm_usage", "LLM usage", usage_error),
        ):
            if error:
                issues.append(_issue(code, "blocked", f"{label} could not be read: {error}."))
        if invalid_enriched:
            issues.append(
                _issue(
                    "malformed_enriched_rows",
                    "blocked",
                    f"Enriched output contains {invalid_enriched} malformed row(s).",
                )
            )

        enriched_count = len(enriched_rows)
        raw_scored_count = len(scored_rows)
        enriched_identities, missing_enriched_identities = _identity_counts(
            _enriched_key(row) for row in enriched_rows
        )
        scored_identities, missing_scored_identities = _identity_counts(_row_key(row) for row in scored_rows)
        enriched_keys = set(enriched_identities)
        scored_keys = set(scored_identities)
        matched_keys = enriched_keys & scored_keys
        scored_count = len(matched_keys)
        unscored_keys = enriched_keys - scored_keys
        extra_scored_keys = scored_keys - enriched_keys
        duplicate_enriched = sum(count - 1 for count in enriched_identities.values() if count > 1)
        duplicate_scored = sum(count - 1 for count in scored_identities.values() if count > 1)
        unscored = len(unscored_keys)
        identities_clean = not any(
            (
                unscored_keys,
                extra_scored_keys,
                duplicate_enriched,
                duplicate_scored,
                missing_enriched_identities,
                missing_scored_identities,
            )
        )
        scoring_complete = bool(enriched_count) and identities_clean and scored_count == enriched_count
        effective_tiered_rows = tiered_rows or (scored_rows if tiered_path == base_scored_path else [])
        declared_tiers = _tier_counts(effective_tiered_rows)
        readiness_mismatches = [
            row
            for row in effective_tiered_rows
            if str(row.get("Packet Tier") or "").strip() == PACKET_TIER_SENDABLE
            and not is_contact_ready(row)
        ]
        tiers = dict(declared_tiers)
        tiers["sendable"] = sum(
            1
            for row in effective_tiered_rows
            if str(row.get("Packet Tier") or "").strip() == PACKET_TIER_SENDABLE
            and is_contact_ready(row)
        )
        expected_packet_rows = [
            row
            for row in effective_tiered_rows
            if str(row.get("Packet Tier") or "").strip() in {PACKET_TIER_SENDABLE, PACKET_TIER_MANUAL}
        ]
        expected_packets = len(expected_packet_rows)

        tiered_identities, missing_tiered_identities = _identity_counts(
            _row_key(row) for row in effective_tiered_rows
        )
        expected_packet_identities, _ = _identity_counts(_row_key(row) for row in expected_packet_rows)
        expected_by_fallback: dict[str, list[str]] = {}
        for row in expected_packet_rows:
            expected_by_fallback.setdefault(_fallback_row_key(row), []).append(_row_key(row))

        def packet_identity(row: dict[str, Any]) -> str:
            explicit = _first(row, "Place Key", "place_key", "Place ID", "place_id")
            if explicit:
                return str(explicit).strip()
            fallback = _fallback_row_key(row)
            matches = expected_by_fallback.get(fallback, [])
            return matches[0] if fallback and len(matches) == 1 else fallback

        packet_identities, missing_packet_identities = _identity_counts(packet_identity(row) for row in packet_rows)
        duplicate_tiered = sum(count - 1 for count in tiered_identities.values() if count > 1)
        duplicate_packets = sum(count - 1 for count in packet_identities.values() if count > 1)
        tiered_missing = sorted((scored_identities - tiered_identities).elements())
        tiered_extra = sorted((tiered_identities - scored_identities).elements())
        packet_missing = sorted((expected_packet_identities - packet_identities).elements())
        packet_extra = sorted((packet_identities - expected_packet_identities).elements())

        scored_mtime = base_scored_path.stat().st_mtime if base_scored_path.exists() else 0.0
        tiered_mtime = tiered_path.stat().st_mtime if tiered_path.exists() else 0.0
        packet_mtime = paths.packet_index.stat().st_mtime if paths.packet_index.exists() else 0.0
        tiered_current = bool(scored_rows) and bool(effective_tiered_rows)
        tiered_current = bool(
            tiered_current
            and tiered_identities == scored_identities
            and not missing_tiered_identities
            and not duplicate_tiered
            and (base_scored_path == tiered_path or tiered_mtime >= scored_mtime)
        )
        packets_current = (
            tiered_current
            and paths.packet_index.exists()
            and packet_identities == expected_packet_identities
            and not missing_packet_identities
            and not duplicate_packets
            and packet_mtime >= tiered_mtime
        )

        if unscored:
            issues.append(_issue("unscored_leads", "attention", f"{unscored} enriched lead(s) remain unscored."))
        if extra_scored_keys:
            issues.append(
                _issue(
                    "extra_scored_identities",
                    "blocked",
                    f"Scored output contains {len(extra_scored_keys)} identity key(s) absent from enrichment.",
                )
            )
        if duplicate_enriched:
            issues.append(
                _issue(
                    "duplicate_enriched_identities",
                    "blocked",
                    f"Enriched output contains {duplicate_enriched} duplicate identity row(s).",
                )
            )
        if duplicate_scored:
            issues.append(
                _issue(
                    "duplicate_scored_identities",
                    "blocked",
                    f"Scored output contains {duplicate_scored} duplicate identity row(s).",
                )
            )
        if missing_enriched_identities:
            issues.append(
                _issue(
                    "missing_enriched_identities",
                    "blocked",
                    f"Enriched output contains {missing_enriched_identities} row(s) without a stable identity.",
                )
            )
        if missing_scored_identities:
            issues.append(
                _issue(
                    "missing_scored_identities",
                    "blocked",
                    f"Scored output contains {missing_scored_identities} row(s) without a stable identity.",
                )
            )
        if readiness_mismatches:
            mismatch_details = ", ".join(
                f"{_row_key(row) or '<missing-identity>'} ({'; '.join(contact_readiness_failures(row))})"
                for row in readiness_mismatches
            )
            issues.append(
                _issue(
                    "sendable_readiness_mismatch",
                    "blocked",
                    "Sendable packet tier conflicts with contact readiness: " + mismatch_details,
                )
            )
        if effective_tiered_rows and (tiered_missing or tiered_extra or duplicate_tiered or missing_tiered_identities):
            issues.append(
                _issue(
                    "tiered_identity_drift",
                    "blocked",
                    f"Tiered identities drifted (missing={tiered_missing}, extra={tiered_extra}, "
                    f"duplicates={duplicate_tiered}, unidentified={missing_tiered_identities}).",
                )
            )
        if paths.packet_index.exists() and (packet_missing or packet_extra or duplicate_packets or missing_packet_identities):
            issues.append(
                _issue(
                    "packet_identity_drift",
                    "blocked",
                    f"Packet identities drifted (missing={packet_missing}, extra={packet_extra}, "
                    f"duplicates={duplicate_packets}, unidentified={missing_packet_identities}).",
                )
            )
        if scored_count and not tiered_current:
            issues.append(_issue("stale_tiered", "attention", "Packet tiers are missing, stale, or row counts do not reconcile."))
        elif tiered_current and not packets_current:
            issues.append(_issue("stale_packets", "attention", "Packet index is missing, stale, or does not match packet-bearing tiers."))

        state_counts = {
            "enriched_rows": enriched_count,
            "scored_rows": raw_scored_count,
            "packet_count": len(packet_rows),
            "sendable_count": tiers["sendable"],
            "manual_review_count": tiers["manual_review"],
        }
        stored_state_counts = {
            key: _typed_int(state, key, issues, scope="run_state", default=value)
            for key, value in state_counts.items()
        }
        drift = [
            key
            for key, value in state_counts.items()
            if key in state and state.get(key) is not None and stored_state_counts[key] != value
        ]
        if paths.state_json.exists() and drift:
            issues.append(_issue("state_count_drift", "attention", "Saved run-state counts differ from current artifacts."))

        pending_value = state.get("pending_approval")
        pending = pending_value if isinstance(pending_value, dict) else None
        if pending_value not in (None, {}) and pending is None:
            issues.append(_issue("invalid_run_state_pending_approval", "blocked", "Run-state pending_approval must be an object."))
        pending_active = False
        if pending:
            expires = _parse_iso(pending.get("expires_at"))
            pending_active = bool(expires and expires > _utc_now())
            if not pending_active:
                issues.append(_issue("expired_approval", "attention", "A saved Hermes approval has expired."))

        if str(state.get("last_error") or "").strip():
            issues.append(_issue("last_run_error", "blocked", str(state["last_error"]).strip()))
        stopped_by_cap = _typed_bool(usage, "stopped_by_cap", issues, scope="llm_usage")
        if stopped_by_cap and unscored:
            reason = str(usage.get("stop_reason") or "configured LLM cap")
            issues.append(_issue("llm_cap_stop", "attention", f"Scoring stopped at {reason}."))

        dry_include_manual = _typed_bool(state, "dry_run_include_manual", issues, scope="run_state")
        dry_skip_count = _typed_int(state, "dry_run_skip_count", issues, scope="run_state")
        live_skip_count = _typed_int(state, "live_skip_count", issues, scope="run_state")
        dry_post_count = _typed_int(state, "dry_run_post_count", issues, scope="run_state")
        dry_patch_count = _typed_int(state, "dry_run_patch_count", issues, scope="run_state")
        live_post_count = _typed_int(state, "live_post_count", issues, scope="run_state")
        live_patch_count = _typed_int(state, "live_patch_count", issues, scope="run_state")
        warnings = _typed_string_list(state, "dry_run_warnings", issues, scope="run_state")

        current_hash = ""
        if paths.tiered_csv.exists() and paths.packet_index.exists():
            try:
                current_hash = current_sync_hash(paths, include_manual=dry_include_manual)
            except (FileNotFoundError, OSError):
                current_hash = ""
        dry_hash = str(state.get("dry_run_input_hash") or "")
        live_hash = str(state.get("live_sync_input_hash") or "")
        dry_clean = bool(
            packets_current
            and dry_hash
            and current_hash
            and dry_hash == current_hash
            and dry_skip_count == 0
            and not warnings
        )
        live_current = bool(
            dry_clean
            and state.get("last_live_sync_timestamp")
            and live_hash
            and live_hash == current_hash
            and live_skip_count == 0
        )
        if warnings:
            issues.append(_issue("airtable_warnings", "blocked", f"Airtable receipt has {len(warnings)} warning(s)."))
        if dry_skip_count:
            issues.append(_issue("airtable_dry_skips", "blocked", "Airtable dry-run contains skipped rows."))
        if packets_current and state.get("last_airtable_dry_run_timestamp") and not dry_clean:
            issues.append(_issue("stale_airtable_dry_run", "attention", "Airtable dry-run no longer matches current packet inputs."))
        if state.get("last_live_sync_timestamp") and not live_current:
            issues.append(_issue("stale_live_sync", "attention", "Airtable live receipt no longer matches current packet inputs."))

        has_blocked_issue = any(item["severity"] == "blocked" for item in issues)
        if live_current and scoring_complete and not has_blocked_issue:
            progress = "finished"
        elif dry_clean and scoring_complete:
            progress = "airtable_ready"
        elif packets_current and scoring_complete:
            progress = "packets_current"
        elif scoring_complete:
            progress = "scored"
        elif raw_scored_count:
            progress = "partial_scoring"
        elif enriched_count:
            progress = "enriched"
        elif raw_rows or deduped_rows:
            progress = "searched"
        else:
            progress = "configured"

        if has_blocked_issue:
            health = "blocked"
        elif issues:
            health = "attention"
        else:
            health = "healthy"

        if pending_active:
            next_action = "Reply yes/no in Hermes"
        elif not enriched_count:
            next_action = f"/run {paths.vertical} {paths.market} {paths.size}"
        elif not scoring_complete:
            next_action = f"/score-dry {batch}"
        elif not packets_current:
            next_action = f"/packets {batch}"
        elif not dry_clean:
            next_action = f"/sync-dry {batch}"
        elif not live_current:
            next_action = f"/sync-live {batch}"
        else:
            next_action = f"/status {batch}"

        artifact_paths = self._artifact_paths(paths) + [base_scored_path, tiered_path]
        updated_at = _iso_timestamp(_latest_mtime(artifact_paths))
        caps_value = usage.get("caps")
        caps = caps_value if isinstance(caps_value, dict) else {}
        if caps_value not in (None, {}) and not isinstance(caps_value, dict):
            issues.append(_issue("invalid_llm_usage_caps", "blocked", "LLM usage caps must be an object."))
            health = "blocked"
        llm_usage = {
            "available": bool(usage),
            "calls": _typed_int(usage, "calls", issues, scope="llm_usage"),
            "call_cap": _typed_int(caps, "MAX_LLM_CALLS_PER_RUN", issues, scope="llm_usage_caps"),
            "tokens": _typed_int(usage, "total_tokens", issues, scope="llm_usage"),
            "token_cap": _typed_int(caps, "MAX_LLM_TOKENS_PER_RUN", issues, scope="llm_usage_caps"),
            "estimated_usd": _typed_float(usage, "estimated_usd", issues, scope="llm_usage"),
            "usd_cap": _typed_float(caps, "MAX_LLM_USD_PER_RUN", issues, scope="llm_usage_caps"),
            "stopped_by_cap": stopped_by_cap,
            "stop_reason": str(usage.get("stop_reason") or ""),
            "updated_at": _iso_timestamp(paths.usage_json.stat().st_mtime) if paths.usage_json.exists() else "",
        }
        if any(item["severity"] == "blocked" for item in issues):
            health = "blocked"
            if progress in {"finished", "airtable_ready"}:
                progress = "packets_current" if packets_current and scoring_complete else "scored"
            next_action = f"/status {batch}"

        summary = {
            "batch": batch,
            "vertical": paths.vertical,
            "market": paths.market,
            "size": paths.size,
            "sequence": paths.seq,
            "location": str(config.get("location") or paths.market.replace("_", " ").title()),
            "query_count": len(config.get("queries") or []) if isinstance(config.get("queries"), list) else 0,
            "progress": progress,
            "progress_rank": PROGRESS_ORDER[progress],
            "health": health,
            "health_rank": HEALTH_ORDER[health],
            "counts": {
                "raw": len(raw_rows),
                "deduped": len(deduped_rows),
                "enriched": enriched_count,
                "scored": scored_count,
                "unscored": unscored,
                "packets": len(packet_rows),
                **tiers,
            },
            "stages": {
                "discovery": {"status": "complete" if (raw_rows or deduped_rows) else "not_started", "count": len(deduped_rows or raw_rows)},
                "enrichment": {"status": "complete" if enriched_count else "not_started", "count": enriched_count},
                "scoring": {"status": "complete" if scoring_complete else ("in_progress" if raw_scored_count else "not_started"), "count": scored_count},
                "packets": {"status": "complete" if packets_current else ("stale" if raw_scored_count else "not_started"), "count": len(packet_rows)},
                "airtable": {"status": "complete" if live_current else ("ready" if dry_clean else ("in_progress" if state.get("last_airtable_dry_run_timestamp") else "not_started")), "count": live_post_count + live_patch_count},
            },
            "issues": issues,
            "latest_activity_at": updated_at,
            "next_action": next_action,
            "pending_approval": {
                "active": pending_active,
                "stage": str((pending or {}).get("stage") or ""),
                "expires_at": str((pending or {}).get("expires_at") or ""),
            },
            "llm_usage": llm_usage,
            "airtable": {
                "dry_run": {
                    "status": "clean" if dry_clean else ("stale" if state.get("last_airtable_dry_run_timestamp") else "not_run"),
                    "timestamp": str(state.get("last_airtable_dry_run_timestamp") or ""),
                    "post": dry_post_count,
                    "patch": dry_patch_count,
                    "skip": dry_skip_count,
                    "warnings": warnings,
                },
                "live": {
                    "status": "current" if live_current else ("stale" if state.get("last_live_sync_timestamp") else "not_run"),
                    "timestamp": str(state.get("last_live_sync_timestamp") or ""),
                    "post": live_post_count,
                    "patch": live_patch_count,
                    "skip": live_skip_count,
                },
                "current_input_hash": current_hash,
            },
            "artifacts": {
                "config": _path_meta(paths.config_path, self.root),
                "raw": _path_meta(paths.raw_csv, self.root, rows=len(raw_rows)),
                "deduped": _path_meta(paths.deduped_csv, self.root, rows=len(deduped_rows)),
                "enriched": _path_meta(paths.enriched, self.root, rows=enriched_count),
                "scored": _path_meta(base_scored_path, self.root, rows=raw_scored_count),
                "tiered": _path_meta(tiered_path, self.root, rows=len(tiered_rows)),
                "packets": _path_meta(paths.packet_index, self.root, rows=len(packet_rows)),
                "run_state": _path_meta(paths.state_json, self.root),
                "llm_usage": _path_meta(paths.usage_json, self.root),
            },
        }
        return BatchRead(summary=summary, scored_rows=tiered_rows or scored_rows, enriched_rows=enriched_rows)

    def _filtered(
        self,
        reads: dict[str, BatchRead],
        *,
        vertical: str = "",
        market: str = "",
        progress: str = "",
        health: str = "",
        query: str = "",
        unscored: bool | None = None,
        finished: bool | None = None,
    ) -> list[dict[str, Any]]:
        items = [read.summary for read in reads.values()]
        normalized_query = query.strip().casefold()
        if vertical:
            items = [item for item in items if item["vertical"] == vertical]
        if market:
            items = [item for item in items if item["market"] == market]
        if progress:
            items = [item for item in items if item["progress"] == progress]
        if health:
            if health == "needs_attention":
                items = [item for item in items if item["health"] != "healthy"]
            else:
                items = [item for item in items if item["health"] == health]
        if normalized_query:
            items = [
                item for item in items
                if normalized_query in f"{item['batch']} {item['vertical']} {item['market']}".casefold()
            ]
        if unscored is not None:
            items = [item for item in items if (item["counts"]["unscored"] > 0) is unscored]
        if finished is not None:
            items = [item for item in items if (item["progress"] == "finished") is finished]
        return items

    def overview(self, *, vertical: str = "", market: str = "", force: bool = False) -> dict[str, Any]:
        reads, ignored = self._scan(force=force)
        items = self._filtered(reads, vertical=vertical, market=market)
        totals = {
            "batches": len(items),
            "needs_attention": sum(1 for item in items if item["health"] != "healthy"),
            "unscored": sum(item["counts"]["unscored"] for item in items),
            "sendable": sum(item["counts"]["sendable"] for item in items),
            "airtable_ready": sum(1 for item in items if item["progress"] == "airtable_ready"),
            "finished": sum(1 for item in items if item["progress"] == "finished"),
        }
        pipeline = [
            {"key": "discovery", "label": "Google discovery", "count": sum(item["stages"]["discovery"]["count"] for item in items)},
            {"key": "enrichment", "label": "Enriched", "count": sum(item["counts"]["enriched"] for item in items)},
            {"key": "scoring", "label": "LLM scored", "count": sum(item["counts"]["scored"] for item in items)},
            {"key": "packets", "label": "Verified packets", "count": sum(item["counts"]["packets"] for item in items)},
            {"key": "airtable", "label": "Airtable confirmed", "count": sum(item["stages"]["airtable"]["count"] for item in items)},
        ]
        attention: list[dict[str, Any]] = []
        for item in items:
            for issue in item["issues"]:
                attention.append({
                    "batch": item["batch"],
                    "vertical": item["vertical"],
                    "market": item["market"],
                    "latest_activity_at": item["latest_activity_at"],
                    "next_action": item["next_action"],
                    **issue,
                })
        attention.sort(
            key=lambda item: (
                0 if item["severity"] == "blocked" else 1,
                -_activity_sort_value(item["latest_activity_at"]),
            )
        )
        return {
            "generated_at": _utc_now().isoformat(),
            "repo_root": str(self.root),
            "totals": totals,
            "pipeline": pipeline,
            "attention": attention[:12],
            "filters": {
                "verticals": sorted({item.summary["vertical"] for item in reads.values()}),
                "markets": sorted({item.summary["market"] for item in reads.values()}),
                "progress": list(PROGRESS_ORDER),
                "health": list(HEALTH_ORDER),
            },
            "source_health": {
                "ignored_noncanonical_artifacts": ignored,
                "authority": "Current artifacts and run-state receipts",
            },
        }

    def batches(
        self,
        *,
        vertical: str = "",
        market: str = "",
        progress: str = "",
        health: str = "",
        query: str = "",
        unscored: bool | None = None,
        finished: bool | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        reads, _ = self._scan(force=force)
        items = self._filtered(
            reads,
            vertical=vertical,
            market=market,
            progress=progress,
            health=health,
            query=query,
            unscored=unscored,
            finished=finished,
        )
        items.sort(
            key=lambda item: (
                item["health_rank"],
                -item["counts"]["unscored"],
                -_activity_sort_value(item["latest_activity_at"]),
            )
        )
        return {"generated_at": _utc_now().isoformat(), "total": len(items), "items": items}

    def batch(self, batch: str, *, force: bool = False) -> dict[str, Any] | None:
        reads, _ = self._scan(force=force)
        read = reads.get(batch)
        return read.summary if read else None

    def leads(
        self,
        batch: str,
        *,
        state: str = "all",
        priority: str = "",
        tier: str = "",
        sendable: bool | None = None,
        money_signal: bool | None = None,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
        force: bool = False,
    ) -> dict[str, Any] | None:
        reads, _ = self._scan(force=force)
        read = reads.get(batch)
        if read is None:
            return None
        scored_by_key = {_row_key(row): row for row in read.scored_rows if _row_key(row)}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()

        def clean_scored(row: dict[str, Any], scoring_state: str) -> dict[str, Any]:
            packet_tier = str(row.get("Packet Tier") or "")
            declared_sendable = _yes(row.get("Sendable"))
            effective_sendable = packet_tier == PACKET_TIER_SENDABLE and is_contact_ready(row)
            readiness_mismatch = declared_sendable != effective_sendable or (
                packet_tier == PACKET_TIER_SENDABLE
            ) != effective_sendable
            return {
                "key": _row_key(row),
                "scoring_state": scoring_state,
                "business_name": str(_first(row, "Business Name", "business_name")),
                "city": str(_first(row, "City", "city")),
                "website_url": str(_first(row, "Website URL", "website_url")),
                "google_maps_url": str(_first(row, "Google Maps URL", "google_maps_url")),
                "score_total": _first(row, "Score Total", "Total Score", "total_score"),
                "priority": str(_first(row, "Lead Priority", "Grade", "grade")),
                "packet_tier": packet_tier,
                "sendable": effective_sendable,
                "declared_sendable": declared_sendable,
                "readiness_mismatch": readiness_mismatch,
                "money_signal": _yes(_first(row, "Money Signal", "money_signal")),
                "ai_confidence": str(_first(row, "AI Confidence", "Confidence", "confidence")),
                "blockers": str(_first(row, "Send Blockers", "Sendability Blockers")),
            }

        for enriched in read.enriched_rows:
            google = _enriched_google(enriched)
            key = _enriched_key(enriched)
            scored = scored_by_key.get(key)
            if scored:
                items.append(clean_scored(scored, "scored"))
            else:
                items.append(clean_scored(google, "unscored"))
            if key:
                seen.add(key)
        for key, scored in scored_by_key.items():
            if key not in seen:
                items.append(clean_scored(scored, "scored"))

        normalized_query = query.strip().casefold()
        if state in {"scored", "unscored"}:
            items = [item for item in items if item["scoring_state"] == state]
        if priority:
            items = [item for item in items if item["priority"].casefold() == priority.casefold()]
        if tier:
            items = [item for item in items if item["packet_tier"].casefold() == tier.casefold()]
        if sendable is not None:
            items = [item for item in items if item["sendable"] is sendable]
        if money_signal is not None:
            items = [item for item in items if item["money_signal"] is money_signal]
        if normalized_query:
            items = [item for item in items if normalized_query in f"{item['business_name']} {item['city']}".casefold()]
        items.sort(
            key=lambda item: (
                0 if item["scoring_state"] == "unscored" else 1,
                -float(item["score_total"] or 0) if str(item["score_total"] or "").replace(".", "", 1).isdigit() else 0,
                item["business_name"].casefold(),
            )
        )
        total = len(items)
        start = (page - 1) * page_size
        return {
            "generated_at": _utc_now().isoformat(),
            "batch": batch,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
            "items": items[start : start + page_size],
        }

    def health(self) -> dict[str, Any]:
        names, ignored = self._candidate_names()
        return {
            "ok": self.root.is_dir(),
            "repo_root": str(self.root),
            "canonical_batches": len(names),
            "ignored_noncanonical_artifacts": ignored,
            "generated_at": _utc_now().isoformat(),
        }


def default_repo_root() -> Path:
    configured = os.getenv("LOCAL_LEAD_MACHINE_ROOT", "").strip()
    return Path(configured).expanduser() if configured else Path.cwd()
