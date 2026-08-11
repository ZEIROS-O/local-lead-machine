"""Fail-closed structural validation for CSV inputs."""

from __future__ import annotations

import csv
from collections import Counter
from typing import Any, TextIO


def read_validated_dict_rows(
    handle: TextIO, *, source: str
) -> tuple[list[str], list[dict[str, Any]]]:
    reader = csv.DictReader(handle)
    fieldnames = list(reader.fieldnames or [])
    if not fieldnames or any(not str(field).strip() for field in fieldnames):
        raise ValueError(f"{source}: blank header")

    duplicates = [
        field for field, count in Counter(fieldnames).items() if count > 1
    ]
    if duplicates:
        raise ValueError(
            f"{source}: duplicate header: {', '.join(repr(field) for field in duplicates)}"
        )

    rows: list[dict[str, Any]] = []
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(
                f"{source}: row {line_number} has unexpected extra cells"
            )
        rows.append(row)
    return fieldnames, rows
