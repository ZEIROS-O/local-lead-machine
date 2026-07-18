from __future__ import annotations

import csv
import io
import json
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
        reads = {name: self._read_batch(name) for name in sorted(names)}
        self._cache_signature = signature
        self._cache_reads = reads
        self._cache_ignored = ignored
        return reads, ignored

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
        tiers = _tier_counts(tiered_rows or scored_rows)
        expected_packets = tiers["sendable"] + tiers["manual_review"]

        scored_mtime = base_scored_path.stat().st_mtime if base_scored_path.exists() else 0.0
        tiered_mtime = tiered_path.stat().st_mtime if tiered_path.exists() else 0.0
        packet_mtime = paths.packet_index.stat().st_mtime if paths.packet_index.exists() else 0.0
        tiered_current = bool(scored_rows) and bool(tiered_rows)
        if base_scored_path != tiered_path:
            tiered_current = (
                tiered_current
                and len(tiered_rows) == raw_scored_count
                and tiered_mtime >= scored_mtime
            )
        packets_current = (
            tiered_current
            and paths.packet_index.exists()
            and len(packet_rows) == expected_packets
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
        drift = [key for key, value in state_counts.items() if state.get(key) not in (None, value)]
        if paths.state_json.exists() and drift:
            issues.append(_issue("state_count_drift", "attention", "Saved run-state counts differ from current artifacts."))

        pending = state.get("pending_approval") if isinstance(state.get("pending_approval"), dict) else None
        pending_active = False
        if pending:
            expires = _parse_iso(pending.get("expires_at"))
            pending_active = bool(expires and expires > _utc_now())
            if not pending_active:
                issues.append(_issue("expired_approval", "attention", "A saved Hermes approval has expired."))

        if str(state.get("last_error") or "").strip():
            issues.append(_issue("last_run_error", "blocked", str(state["last_error"]).strip()))
        if usage.get("stopped_by_cap") and unscored:
            reason = str(usage.get("stop_reason") or "configured LLM cap")
            issues.append(_issue("llm_cap_stop", "attention", f"Scoring stopped at {reason}."))

        current_hash = ""
        if paths.tiered_csv.exists() and paths.packet_index.exists():
            try:
                current_hash = current_sync_hash(paths, include_manual=bool(state.get("dry_run_include_manual")))
            except (FileNotFoundError, OSError):
                current_hash = ""
        dry_hash = str(state.get("dry_run_input_hash") or "")
        live_hash = str(state.get("live_sync_input_hash") or "")
        warnings = [str(value) for value in (state.get("dry_run_warnings") or [])]
        dry_clean = bool(
            packets_current
            and dry_hash
            and current_hash
            and dry_hash == current_hash
            and int(state.get("dry_run_skip_count") or 0) == 0
            and not warnings
        )
        live_current = bool(
            dry_clean
            and state.get("last_live_sync_timestamp")
            and live_hash
            and live_hash == current_hash
            and int(state.get("live_skip_count") or 0) == 0
        )
        if warnings:
            issues.append(_issue("airtable_warnings", "blocked", f"Airtable receipt has {len(warnings)} warning(s)."))
        if int(state.get("dry_run_skip_count") or 0):
            issues.append(_issue("airtable_dry_skips", "blocked", "Airtable dry-run contains skipped rows."))
        if packets_current and state.get("last_airtable_dry_run_timestamp") and not dry_clean:
            issues.append(_issue("stale_airtable_dry_run", "attention", "Airtable dry-run no longer matches current packet inputs."))
        if state.get("last_live_sync_timestamp") and not live_current:
            issues.append(_issue("stale_live_sync", "attention", "Airtable live receipt no longer matches current packet inputs."))

        if live_current and scoring_complete:
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

        if any(item["severity"] == "blocked" for item in issues):
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
        caps = usage.get("caps") if isinstance(usage.get("caps"), dict) else {}
        llm_usage = {
            "available": bool(usage),
            "calls": int(usage.get("calls") or 0),
            "call_cap": int(caps.get("MAX_LLM_CALLS_PER_RUN") or 0),
            "tokens": int(usage.get("total_tokens") or 0),
            "token_cap": int(caps.get("MAX_LLM_TOKENS_PER_RUN") or 0),
            "estimated_usd": float(usage.get("estimated_usd") or 0.0),
            "usd_cap": float(caps.get("MAX_LLM_USD_PER_RUN") or 0.0),
            "stopped_by_cap": bool(usage.get("stopped_by_cap")),
            "stop_reason": str(usage.get("stop_reason") or ""),
            "updated_at": _iso_timestamp(paths.usage_json.stat().st_mtime) if paths.usage_json.exists() else "",
        }

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
                "airtable": {"status": "complete" if live_current else ("ready" if dry_clean else ("in_progress" if state.get("last_airtable_dry_run_timestamp") else "not_started")), "count": int(state.get("live_post_count") or 0) + int(state.get("live_patch_count") or 0)},
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
                    "post": int(state.get("dry_run_post_count") or 0),
                    "patch": int(state.get("dry_run_patch_count") or 0),
                    "skip": int(state.get("dry_run_skip_count") or 0),
                    "warnings": warnings,
                },
                "live": {
                    "status": "current" if live_current else ("stale" if state.get("last_live_sync_timestamp") else "not_run"),
                    "timestamp": str(state.get("last_live_sync_timestamp") or ""),
                    "post": int(state.get("live_post_count") or 0),
                    "patch": int(state.get("live_patch_count") or 0),
                    "skip": int(state.get("live_skip_count") or 0),
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
            return {
                "key": _row_key(row),
                "scoring_state": scoring_state,
                "business_name": str(_first(row, "Business Name", "business_name")),
                "city": str(_first(row, "City", "city")),
                "website_url": str(_first(row, "Website URL", "website_url")),
                "google_maps_url": str(_first(row, "Google Maps URL", "google_maps_url")),
                "score_total": _first(row, "Score Total", "Total Score", "total_score"),
                "priority": str(_first(row, "Lead Priority", "Grade", "grade")),
                "packet_tier": str(row.get("Packet Tier") or ""),
                "sendable": _yes(row.get("Sendable")),
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
