from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


ACCEPTABLE_SMS_CONSENT_POSTURES = frozenset(
    {
        "documented opt in",
        "express written consent",
        "prior express written consent",
    }
)
SUPPRESSION_EVENTS = frozenset(
    {"opt_out", "opted_out", "unsubscribe", "unsubscribed", "suppressed", "stop"}
)
MARK_SENT_ALLOWED_SOURCE_STATUSES = frozenset({"new", "mockup needed"})


class ComplianceError(ValueError):
    pass


def normalize_consent_posture(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def has_acceptable_sms_consent(value: Any) -> bool:
    return normalize_consent_posture(value) in ACCEPTABLE_SMS_CONSENT_POSTURES


def normalize_airtable_status(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def is_mark_sent_source_status(value: Any) -> bool:
    return normalize_airtable_status(value) in MARK_SENT_ALLOWED_SOURCE_STATUSES


def suppression_identifier(kind: str, value: Any) -> str:
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


def record_suppression_identifiers(record: dict[str, Any]) -> set[str]:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    assert isinstance(fields, dict)
    values = {
        "record_id": record.get("record_id") or record.get("id") or fields.get("record_id"),
        "phone": fields.get("phone") or fields.get("Phone"),
        "email": fields.get("email") or fields.get("Email"),
    }
    return {
        identifier
        for kind, value in values.items()
        if (identifier := suppression_identifier(kind, value))
    }


def suppression_source_path(value: Path | str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ComplianceError("suppression source is not configured")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ComplianceError(f"suppression source {path} is missing or unreadable")
    return path


def load_suppression_index(value: Path | str) -> set[str]:
    path = suppression_source_path(value)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ComplianceError(f"suppression source {path} is unreadable: {exc}") from exc

    suppressed: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ComplianceError(
                f"suppression source {path}:{line_number} is malformed JSON: {exc.msg}"
            ) from exc
        if not isinstance(row, dict):
            raise ComplianceError(f"suppression source {path}:{line_number} must contain a JSON object")
        event = str(row.get("event") or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if event not in SUPPRESSION_EVENTS:
            continue
        identifiers = {
            identifier
            for key in ("record_id", "phone", "email")
            if (identifier := suppression_identifier(key, row.get(key)))
        }
        if not identifiers:
            raise ComplianceError(
                f"suppression source {path}:{line_number} has a suppression event without an identifier"
            )
        suppressed.update(identifiers)
    return suppressed


def matching_suppression_identifiers(record: dict[str, Any], suppression: Iterable[str]) -> set[str]:
    return record_suppression_identifiers(record) & set(suppression)


def is_suppressed(record: dict[str, Any], suppression: Iterable[str]) -> bool:
    return bool(matching_suppression_identifiers(record, suppression))
