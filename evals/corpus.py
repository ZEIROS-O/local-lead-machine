"""Which scored corpora the evals replay, and why it is not "all of them".

All three corpus evals defaulted to globbing `scored_*.csv` over `output/`, which
on this machine is 49 files reaching back to 2026-05-31 — including
`scored_debug_1.csv` and every superseded recovery/claimfix run. They had no
concept of a *current* corpus, so completeness and reproducibility replayed
every corpus ever scored and could never go green: a lead re-scored across three
recovery passes contributes three rows, two of them from a gate revision nobody
is operating against any more.

The fix is a checked-in manifest naming the active corpora. It is data, not code,
so adding a batch is a one-line edit and the evals do not have to change.

Three rules this module enforces, each of which the glob got wrong:

1. **A manifest entry that names a missing file is an error, not a skip.** A
   silently-skipped entry is how a corpus falls out of coverage while the gate
   stays green.
2. **The resolved set is reported, not just used.** `describe()` feeds a
   `corpora` block into every report, so a green run states its own coverage
   instead of leaving the reader to assume it was complete.
3. **Explicit arguments still win.** `--scored-file` and an explicit `--glob`
   override the manifest, because the evals are also used to inspect one batch by
   hand. `pattern=None` — the default — means "not specified", which is what lets
   an explicit `--glob scored_*.csv` mean the glob rather than being
   indistinguishable from the default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "evals" / "active_corpora.json"
DEFAULT_PATTERN = "scored_*.csv"


class CorpusManifestError(ValueError):
    """Raised when the manifest is unreadable, malformed, or names a missing file."""


def load_manifest(manifest_path: Path | str | None = None) -> list[str] | None:
    """The corpus names the manifest lists, or None when there is no manifest.

    None and `[]` are different answers: no manifest means "fall back to the
    glob", while an empty list means "the manifest says nothing is active",
    which is a configuration error rather than a silent full-corpus replay.
    """
    path = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusManifestError(f"corpus manifest {path} is unreadable: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CorpusManifestError(f"corpus manifest {path} must contain an object")
    names = loaded.get("active")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise CorpusManifestError(f"corpus manifest {path} needs an 'active' list of filenames")
    if not names:
        raise CorpusManifestError(f"corpus manifest {path} lists no active corpora")
    return names


def resolve_scored_paths(
    *,
    output_dir: Path | str = Path("output"),
    pattern: str | None = None,
    scored_files: Sequence[Path] | None = None,
    manifest_path: Path | str | None = None,
) -> tuple[list[Path], str]:
    """The corpora to replay, plus a one-word label for how they were chosen.

    Precedence is explicit files, then an explicit glob, then the manifest, then
    the default glob — the same order the three evals used to implement
    separately, with the manifest step inserted before the fallback.
    """
    if scored_files is not None:
        return [Path(path) for path in scored_files], "explicit"

    directory = Path(output_dir)
    if pattern is not None:
        return sorted(directory.glob(pattern)), "glob"

    names = load_manifest(manifest_path)
    if names is None:
        return sorted(directory.glob(DEFAULT_PATTERN)), "glob"

    paths, missing = [], []
    for name in names:
        candidate = directory / name
        (paths if candidate.is_file() else missing).append(candidate)
    if missing:
        raise CorpusManifestError(
            "corpus manifest names files that are not in "
            f"{directory}: {', '.join(sorted(path.name for path in missing))}"
        )
    return paths, "manifest"


def describe(paths: Sequence[Path], source: str) -> dict[str, Any]:
    """The coverage block every eval report carries, so a green run says what it read."""
    return {
        "source": source,
        "count": len(paths),
        "files": sorted(path.name for path in paths),
    }
