from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lead_machine.hermes_batch import BatchPaths


SCHEMA_VERSION = 1
TOTAL_UPLOAD_SCOPE = "all_scored_rows"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def sync_manifest(paths: BatchPaths) -> dict[str, Any]:
    files = []
    for path in (paths.tiered_csv, paths.packet_index):
        if not path.exists():
            raise FileNotFoundError(path)
        files.append(
            {
                "path": _rel(path, paths.root),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return {
        "batch": paths.batch,
        "upload_scope": TOTAL_UPLOAD_SCOPE,
        "files": files,
    }


def current_sync_hash(paths: BatchPaths) -> str:
    manifest = sync_manifest(paths)
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def default_state(paths: BatchPaths) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "batch": paths.batch,
        "vertical": paths.vertical,
        "market": paths.market,
        "size": paths.size,
        "enriched_rows": 0,
        "scored_rows": 0,
        "packet_count": 0,
        "sendable_count": 0,
        "manual_review_count": 0,
        "last_score_dry_run_timestamp": "",
        "projected_scoring_cost": None,
        "last_paid_score_timestamp": "",
        "last_packet_build_timestamp": "",
        "last_airtable_dry_run_timestamp": "",
        "dry_run_input_hash": "",
        "dry_run_post_count": 0,
        "dry_run_patch_count": 0,
        "dry_run_skip_count": 0,
        "dry_run_warnings": [],
        "dry_run_fallback_match_count": 0,
        # Empty fails closed for run-state files written before total upload.
        # record_dry_run sets the fixed scope after a fresh total dry-run.
        "dry_run_upload_scope": "",
        "last_live_sync_timestamp": "",
        "live_sync_input_hash": "",
        "live_post_count": 0,
        "live_patch_count": 0,
        "live_skip_count": 0,
        "pending_approval": None,
        "autopilot_original_command": "",
        "last_error": "",
        "next_recommended_action": "",
    }


def load_run_state(paths: BatchPaths) -> dict[str, Any]:
    if not paths.state_json.exists():
        return default_state(paths)
    data = json.loads(paths.state_json.read_text(encoding="utf-8"))
    state = default_state(paths)
    state.update(data)
    return state


def save_run_state(paths: BatchPaths, state: dict[str, Any]) -> None:
    paths.state_json.parent.mkdir(parents=True, exist_ok=True)
    paths.state_json.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_run_state(paths: BatchPaths, **updates: Any) -> dict[str, Any]:
    state = load_run_state(paths)
    state.update(updates)
    save_run_state(paths, state)
    return state
