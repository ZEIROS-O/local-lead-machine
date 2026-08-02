"""Stable setter assignment for Airtable prospects."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from typing import Any

from .schema import normalize_phone


SETTER_ROSTER_ENV = "LPM_SETTER_ROSTER"


def configured_setter_roster(raw: str | None = None) -> tuple[str, ...]:
    """Return the ordered active roster, rejecting accidental duplicate weight."""
    value = os.getenv(SETTER_ROSTER_ENV, "") if raw is None else raw
    roster = tuple(name.strip() for name in value.split(",") if name.strip())
    seen: set[str] = set()
    for name in roster:
        key = name.casefold()
        if key in seen:
            raise ValueError(f"duplicate setter in {SETTER_ROSTER_ENV}: {name}")
        seen.add(key)
    return roster


def assignment_identity(place_key: Any, phone: Any = "") -> str:
    """The stable string an assignment hashes, or "" when there is none.

    `Place Key` when the record has one, so a place-keyed assignment stays
    exactly `sha256(Place Key)` as the SOP states. Phone-keyed records are the
    other front door the uploader has always accepted — `csv_row_to_record`
    upserts them on `phone:<normalized>` — and hashing that same fallback gives
    them an assignment instead of aborting a whole run on a blank Place Key.
    `output/scored_detailing_columbus_medium_001.csv` is 140 such rows. The
    prefix is what keeps the two identifier namespaces from colliding.
    """
    key = str(place_key or "").strip()
    if key:
        return key
    phone_key = normalize_phone(str(phone or ""))
    return f"phone:{phone_key}" if phone_key else ""


def assign_setter(identity: str, roster: Sequence[str]) -> str:
    """Choose a setter with stable SHA-256 modulo assignment."""
    key = str(identity or "").strip()
    if not key:
        raise ValueError(
            "a Place Key or phone is required for deterministic setter assignment"
        )
    if not roster:
        raise ValueError("setter roster must not be empty")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return str(roster[int.from_bytes(digest, "big") % len(roster)])
