from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from generate_search_config import VALID_SIZES


SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class BatchResolutionError(ValueError):
    pass


def _rel_or_root(root: Path, rel: str) -> Path:
    path = Path(rel)
    return path if root == Path(".") else root / path


@dataclass(frozen=True)
class BatchPaths:
    batch: str
    vertical: str
    market: str
    size: str
    seq: str
    root: Path = Path(".")

    @classmethod
    def for_batch(cls, batch: str, *, root: Path | str = Path(".")) -> "BatchPaths":
        return resolve_batch_paths(batch, root=Path(root))

    @property
    def config_path(self) -> Path:
        return _rel_or_root(self.root, f"search_configs/{self.batch}.json")

    @property
    def raw_csv(self) -> Path:
        return _rel_or_root(self.root, f"data/raw/{self.batch}.csv")

    @property
    def deduped_csv(self) -> Path:
        return _rel_or_root(self.root, f"data/deduped/{self.batch}.csv")

    @property
    def enriched(self) -> Path:
        return _rel_or_root(self.root, f"output/enriched_{self.batch}.jsonl")

    @property
    def scored_csv(self) -> Path:
        return _rel_or_root(self.root, f"output/scored_{self.batch}_api.csv")

    @property
    def manual_scored_csv(self) -> Path:
        return _rel_or_root(self.root, f"output/scored_{self.batch}_manual.csv")

    @property
    def tiered_csv(self) -> Path:
        return _rel_or_root(self.root, f"output/scored_{self.batch}_api_tiered.csv")

    @property
    def packets_dir(self) -> Path:
        return _rel_or_root(self.root, f"packets_{self.batch}")

    @property
    def packet_index(self) -> Path:
        return self.packets_dir / "packet_index.csv"

    @property
    def report_path(self) -> Path:
        return _rel_or_root(self.root, f"reports/search_{self.batch}.md")

    @property
    def review_queue(self) -> Path:
        return _rel_or_root(self.root, f"review_queue_{self.batch}")

    @property
    def scores_dir(self) -> Path:
        return self.review_queue / "scores"

    @property
    def cards_output(self) -> Path:
        return _rel_or_root(self.root, f"output/lead_cards_{self.batch}_api.jsonl")

    @property
    def usage_json(self) -> Path:
        return _rel_or_root(self.root, f"output/llm_usage_{self.batch}.json")

    @property
    def state_json(self) -> Path:
        return _rel_or_root(self.root, f"run_state/{self.batch}.json")

    @property
    def airtable_receipts_dir(self) -> Path:
        return _rel_or_root(self.root, f"run_state/airtable_receipts/{self.batch}")


def _known_stems(root: Path, directory: str) -> set[str]:
    path = _rel_or_root(root, directory)
    if not path.exists():
        return set()
    return {p.stem for p in path.glob("*.json")}


def resolve_batch_paths(batch: str, *, root: Path | str = Path(".")) -> BatchPaths:
    root_path = Path(root)
    raw = (batch or "").strip()
    parts = raw.split("_")
    if len(parts) < 4 or not parts[-1].isdigit() or parts[-2] not in VALID_SIZES:
        raise BatchResolutionError(
            f"Unrecognized batch '{batch}'. Expected <vertical>_<market>_<size>_NNN."
        )

    size = parts[-2]
    seq = parts[-1]
    remainder = "_".join(parts[:-2])
    verticals = _known_stems(root_path, "vertical_profiles")
    markets = _known_stems(root_path, "market_profiles")
    for vertical in sorted(verticals, key=len, reverse=True):
        prefix = f"{vertical}_"
        if remainder.startswith(prefix):
            market = remainder[len(prefix):]
            if market in markets:
                return BatchPaths(
                    batch=raw,
                    vertical=vertical,
                    market=market,
                    size=size,
                    seq=seq,
                    root=root_path,
                )
    if not verticals or not markets:
        return BatchPaths(
            batch=raw,
            vertical=parts[0],
            market="_".join(parts[1:-2]),
            size=size,
            seq=seq,
            root=root_path,
        )
    raise BatchResolutionError(
        f"Could not resolve batch '{batch}' to a known vertical + market."
    )


def nearby_batches(batch: str, *, root: Path | str = Path("."), limit: int = 8) -> list[str]:
    root_path = Path(root)
    names: set[str] = set()
    for pattern in (
        "search_configs/*.json",
        "output/enriched_*.jsonl",
        "output/scored_*_api.csv",
        "packets_*/packet_index.csv",
    ):
        for path in root_path.glob(pattern):
            name = path.stem
            if name.startswith("enriched_"):
                name = name.removeprefix("enriched_")
            if name.startswith("scored_") and name.endswith("_api"):
                name = name.removeprefix("scored_").removesuffix("_api")
            if path.name == "packet_index.csv":
                name = path.parent.name.removeprefix("packets_")
            names.add(name)
    return difflib.get_close_matches(batch, sorted(names), n=limit, cutoff=0.35)
