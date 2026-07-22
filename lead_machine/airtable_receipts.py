from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from lead_machine.hermes_batch import BatchPaths
from lead_machine.hermes_state import utc_now


SCHEMA_VERSION = 1


def _clean_identities(values: Iterable[Any]) -> list[str]:
    identities = sorted({str(value or "").strip() for value in values})
    if not identities or any(not value for value in identities):
        raise ValueError("Airtable cohort receipt requires non-empty unique identities")
    return identities


def identity_set_sha256(identities: Iterable[Any]) -> str:
    cleaned = _clean_identities(identities)
    payload = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_cohort(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    if not slug:
        raise ValueError("Airtable cohort receipt requires a cohort label")
    return slug


def write_verified_cohort_receipt(
    paths: BatchPaths,
    *,
    cohort: str,
    identities: Iterable[Any],
    live_post_count: int,
    live_patch_count: int,
    verification_patch_count: int,
    recorded_at: str | None = None,
) -> Path:
    cleaned = _clean_identities(identities)
    record_count = len(cleaned)
    if min(live_post_count, live_patch_count, verification_patch_count) < 0:
        raise ValueError("Airtable cohort receipt counts must be nonnegative")
    if live_post_count + live_patch_count != record_count:
        raise ValueError("Airtable live receipt counts must equal the cohort identity count")
    if verification_patch_count != record_count:
        raise ValueError("Post-live verification must match every cohort identity")

    timestamp = recorded_at or utc_now()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "batch": paths.batch,
        "kind": "verified-custom-cohort",
        "cohort": _safe_cohort(cohort),
        "recorded_at": timestamp,
        "record_count": record_count,
        "identities": cleaned,
        "identity_set_sha256": identity_set_sha256(cleaned),
        "live_post_count": live_post_count,
        "live_patch_count": live_patch_count,
        "live_skip_count": 0,
        "verification_method": "post-live-airtable-dry-run",
        "verification_patch_count": verification_patch_count,
        "verification_skip_count": 0,
        "verification_warnings": [],
    }
    path = paths.airtable_receipts_dir / f"{receipt['cohort']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def validate_verified_cohort_receipt(
    value: dict[str, Any], *, paths: BatchPaths, scored_identities: set[str]
) -> str:
    if value.get("schema_version") != SCHEMA_VERSION:
        return "unsupported schema_version"
    if value.get("batch") != paths.batch:
        return "batch does not match receipt directory"
    if value.get("kind") != "verified-custom-cohort":
        return "kind is not verified-custom-cohort"
    raw_identities = value.get("identities") or []
    if not isinstance(raw_identities, list):
        return "identities must be a list"
    try:
        identities = _clean_identities(raw_identities)
    except ValueError as exc:
        return str(exc)
    if len(identities) != len(raw_identities):
        return "identities must be unique and non-empty"
    if value.get("record_count") != len(identities):
        return "record_count does not match identities"
    if value.get("identity_set_sha256") != identity_set_sha256(identities):
        return "identity_set_sha256 does not match identities"
    unknown = sorted(set(identities) - scored_identities)
    if unknown:
        return f"receipt contains {len(unknown)} identity key(s) absent from scored output"
    count_keys = (
        "live_post_count",
        "live_patch_count",
        "live_skip_count",
        "verification_patch_count",
        "verification_skip_count",
    )
    if not all(
        isinstance(value.get(key), int) and not isinstance(value.get(key), bool) and value[key] >= 0
        for key in count_keys
    ):
        return "receipt counts must be nonnegative integers"
    if value["live_post_count"] + value["live_patch_count"] != len(identities):
        return "live counts do not match identities"
    if value["live_skip_count"] or value["verification_skip_count"]:
        return "verified receipt cannot contain skipped rows"
    if value["verification_patch_count"] != len(identities):
        return "post-live verification does not cover every identity"
    if value.get("verification_warnings") != []:
        return "verified receipt cannot contain warnings"
    if not str(value.get("recorded_at") or "").strip():
        return "recorded_at is required"
    return ""
