from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


REGISTRY_RELATIVE_PATH = Path("lead_machine/dashboard/legacy_batches.json")


@dataclass(frozen=True)
class LegacyBatch:
    batch: str
    legacy_since: str
    reason: str
    note: str


def load_legacy_batches(root: Path) -> dict[str, LegacyBatch]:
    path = root / REGISTRY_RELATIVE_PATH
    value = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"schema_version": 1, "batches": []}
    )
    if value.get("schema_version") != 1 or not isinstance(
        value.get("batches"), list
    ):
        raise ValueError(
            "legacy batch registry must use schema_version 1 and a batches list"
        )

    result: dict[str, LegacyBatch] = {}
    for raw in value["batches"]:
        if not isinstance(raw, dict):
            raise ValueError("legacy batch registry entries must be objects")
        try:
            item = LegacyBatch(**raw)
        except TypeError as exc:
            raise ValueError(f"invalid legacy batch registry entry: {raw!r}") from exc
        if not all((item.batch, item.legacy_since, item.reason, item.note)):
            raise ValueError(f"incomplete legacy batch registry entry: {item.batch!r}")
        if item.batch in result:
            raise ValueError(f"duplicate legacy batch registry entry: {item.batch}")
        result[item.batch] = item
    return result
