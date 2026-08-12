#!/usr/bin/env python3
"""Replay every sendability gate over the scored corpus and count what fires.

A gate that never fires is either dead code or shadowed by an earlier one, and
either way it is a check certifying less than its name promises. Finding the two
unwired halves of the G3 tighten took a by-hand grep; this makes that analysis a
build step so it cannot recur.

Three questions, three categories:

- **unreferenced** — the constant exists and nothing outside its own declaration
  reads it. This is the exact shape of the deleted `MONEY_SIGNAL_BLOCKER`: fully
  written, wired to nothing. Tests do not count as a reader; a gate kept alive
  only by its own test still blocks no lead.
- **never_fires** — reachable from `evaluate_sendability` and zero rows in the
  whole corpus tripped it. Nothing proves it works.
- **outside_replay** — minted by a different entry point (`upload_airtable`,
  `manual_lead_insert`, `readiness_flags`). Reported with its callers, never
  failed: a corpus replay of one entry point cannot exercise it, and calling
  that death would be the same overclaim this eval exists to catch.

The roster is discovered from the module, never listed here, so a new blocker
joins the eval the moment it is declared.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lead_machine import sendability  # noqa: E402
from evals.corpus import (  # noqa: E402
    DEFAULT_PATTERN,
    CorpusManifestError,
    describe,
    resolve_scored_paths,
)

REPLAY_ENTRY_POINT = "evaluate_sendability"
SENDABILITY_SOURCE = REPO_ROOT / "lead_machine" / "sendability.py"
FINDING_CATEGORIES = ("unreferenced", "never_fires")
REPORTED_CATEGORIES = ("outside_replay",)
# Tests are excluded on purpose: a blocker referenced only by the test that
# asserts it exists still blocks no lead.
REFERENCE_EXCLUDED_DIRS = frozenset({"tests", ".venv", ".worktrees", "__pycache__"})

REMEDIES = {
    "unreferenced": (
        "Nothing reads these constants. Delete them, or wire the consumer that "
        "was never written."
    ),
    "never_fires": (
        "Reachable but never tripped by any scored row. Either an earlier gate "
        "shadows it, or the corpus cannot exercise it — confirm which, then "
        "delete the gate or widen the corpus."
    ),
}


def _set_csv_field_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def _text(value: Any) -> str:
    return str(value or "").strip()


def blocker_constants(module: Any) -> dict[str, str]:
    """Every `*_BLOCKER` the module declares, discovered rather than listed."""
    return {
        name: getattr(module, name)
        for name in dir(module)
        if name.endswith("_BLOCKER") and isinstance(getattr(module, name), str)
    }


def reachable_blockers(source: str, entry_point: str) -> set[str]:
    """Blockers reachable from *entry_point*, following calls within the module.

    Transitive on purpose: `HARD_ICP_SKIP_BLOCKER` is emitted two calls below
    `evaluate_sendability`, and a scan of the entry body alone would report a
    live gate as out of reach.
    """
    tree = ast.parse(source)
    declared: dict[str, tuple[set[str], set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        blockers: set[str] = set()
        calls: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id.endswith("_BLOCKER"):
                blockers.add(child.id)
            elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                calls.add(child.func.id)
        declared[node.name] = (blockers, calls)

    reached: set[str] = set()
    seen: set[str] = set()
    stack = [entry_point]
    while stack:
        name = stack.pop()
        if name in seen or name not in declared:
            continue
        seen.add(name)
        blockers, calls = declared[name]
        reached |= blockers
        stack.extend(calls)
    return reached


def repo_references(
    names: Iterable[str],
    *,
    root: Path = REPO_ROOT,
    declaration_source: Path = SENDABILITY_SOURCE,
) -> dict[str, list[str]]:
    """Map each blocker to the repo files that read it, ignoring its declaration."""
    wanted = set(names)
    references: dict[str, set[str]] = {name: set() for name in wanted}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if REFERENCE_EXCLUDED_DIRS & set(relative.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        declaring = path.resolve() == declaration_source.resolve()
        for node in ast.walk(tree):
            # An assignment target is the declaration itself, never a read.
            if declaring and isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in wanted:
                        break
                else:
                    continue
                continue
            if isinstance(node, ast.Name) and node.id in wanted:
                if isinstance(node.ctx, ast.Store):
                    continue
                references[node.id].add(relative.as_posix())
            elif isinstance(node, ast.Attribute) and node.attr in wanted:
                references[node.attr].add(relative.as_posix())
    return {name: sorted(paths) for name, paths in references.items()}


def count_fires(
    results: Iterable[dict[str, Any]],
    constants: dict[str, str],
) -> Counter:
    """Count rows that tripped each blocker.

    Matches the bare message and the `"<message>: <detail>"` form the commercial
    fit and hard-ICP gates ship, which a plain equality check would miss
    entirely. Counts a blocker once per row, so one row emitting four
    commercial-fit variants does not read as four leads blocked.
    """
    fires: Counter = Counter({name: 0 for name in constants})
    for result in results:
        messages = result.get("send_blockers") or []
        for name, message in constants.items():
            if any(
                item == message or item.startswith(f"{message}:")
                for item in (_text(entry) for entry in messages)
            ):
                fires[name] += 1
    return fires


def _read_scored_rows(paths: Sequence[Path]) -> tuple[list[dict[str, str]], int]:
    _set_csv_field_limit()
    rows: list[dict[str, str]] = []
    count = 0
    for path in sorted(paths, key=lambda item: (item.name, str(item))):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                count += 1
                rows.append(
                    {
                        str(key): _text(value)
                        for key, value in raw.items()
                        if key is not None
                    }
                )
    return rows, count


def _replay(row: dict[str, Any]) -> list[str]:
    return list(sendability.evaluate_sendability(row)["send_blockers"])


def evaluate_gate_liveness(
    scored_files: Sequence[Path],
    *,
    constants: dict[str, str] | None = None,
    in_scope: set[str] | None = None,
    references: dict[str, list[str]] | None = None,
    replay: Callable[[dict[str, Any]], list[str]] | None = None,
) -> dict[str, Any]:
    paths = [Path(path) for path in scored_files]
    if not paths:
        raise ValueError("no scored CSV files matched")
    resolved_constants = (
        constants if constants is not None else blocker_constants(sendability)
    )
    resolved_scope = (
        in_scope
        if in_scope is not None
        else reachable_blockers(
            SENDABILITY_SOURCE.read_text(encoding="utf-8"), REPLAY_ENTRY_POINT
        )
    )
    resolved_references = (
        references
        if references is not None
        else repo_references(resolved_constants)
    )
    replay_fn = replay or _replay

    rows, scored_row_count = _read_scored_rows(paths)
    if not rows:
        # Every gate would read as dead. That is a corpus failure, and reporting
        # it as N code failures is exactly the misdirection this eval exists to
        # prevent.
        raise ValueError("scored corpus is empty; gate liveness cannot be judged")

    fires = count_fires(
        ({"send_blockers": replay_fn(row)} for row in rows), resolved_constants
    )

    findings: dict[str, list[dict[str, Any]]] = {
        category: [] for category in FINDING_CATEGORIES + REPORTED_CATEGORIES
    }
    for name in sorted(resolved_constants):
        message = resolved_constants[name]
        referenced_by = resolved_references.get(name) or []
        if not referenced_by:
            findings["unreferenced"].append({"blocker": name, "message": message})
        elif name in resolved_scope:
            if not fires[name]:
                findings["never_fires"].append(
                    {
                        "blocker": name,
                        "message": message,
                        "referenced_by": referenced_by,
                    }
                )
        else:
            findings["outside_replay"].append(
                {"blocker": name, "message": message, "referenced_by": referenced_by}
            )

    failed = {
        category: len(findings[category])
        for category in FINDING_CATEGORIES
        if findings[category]
    }
    return {
        "ok": not failed,
        "entry_point": REPLAY_ENTRY_POINT,
        "counts": {
            "scored_files": len(paths),
            "scored_rows": scored_row_count,
            "gates": len(resolved_constants),
            "in_replay_scope": len(resolved_scope & set(resolved_constants)),
            "findings": {
                category: len(findings[category])
                for category in FINDING_CATEGORIES + REPORTED_CATEGORIES
            },
        },
        "remedies": {category: REMEDIES[category] for category in sorted(failed)},
        "fires": dict(sorted(fires.items())),
        "findings": findings,
    }


def evaluate_blocker_references(
    *,
    constants: dict[str, str] | None = None,
    in_scope: set[str] | None = None,
    references: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """The half of this eval that needs no scored corpus.

    `unreferenced` — a blocker constant no shipped module mentions — is pure
    static analysis over the checkout, so it is the only part that can run in
    hosted CI. The corpus lives in `output/`, which is gitignored and exists
    only on the machine that scored it, so `never_fires` cannot be judged there:
    with no rows every gate reads as dead, and `evaluate_gate_liveness` raises
    rather than report that as N code failures.

    Separated so a hosted job can catch a dead gate on every push instead of
    waiting for a self-hosted runner that may not exist.
    """
    resolved_constants = (
        constants if constants is not None else blocker_constants(sendability)
    )
    resolved_scope = (
        in_scope
        if in_scope is not None
        else reachable_blockers(
            SENDABILITY_SOURCE.read_text(encoding="utf-8"), REPLAY_ENTRY_POINT
        )
    )
    resolved_references = (
        references if references is not None else repo_references(resolved_constants)
    )

    findings: dict[str, list[dict[str, Any]]] = {"unreferenced": [], "outside_replay": []}
    for name in sorted(resolved_constants):
        message = resolved_constants[name]
        referenced_by = resolved_references.get(name) or []
        if not referenced_by:
            findings["unreferenced"].append({"blocker": name, "message": message})
        elif name not in resolved_scope:
            findings["outside_replay"].append(
                {"blocker": name, "message": message, "referenced_by": referenced_by}
            )

    failed = bool(findings["unreferenced"])
    return {
        "ok": not failed,
        "mode": "references-only",
        "entry_point": REPLAY_ENTRY_POINT,
        "counts": {
            "gates": len(resolved_constants),
            "in_replay_scope": len(resolved_scope & set(resolved_constants)),
            "findings": {
                "unreferenced": len(findings["unreferenced"]),
                "outside_replay": len(findings["outside_replay"]),
            },
        },
        "remedies": {"unreferenced": REMEDIES["unreferenced"]} if failed else {},
        "findings": findings,
    }


def run_eval(
    *,
    output_dir: Path = Path("output"),
    pattern: str | None = None,
    scored_files: Sequence[Path] | None = None,
    references_only: bool = False,
) -> tuple[int, dict[str, Any]]:
    if references_only:
        report = evaluate_blocker_references()
        return (0 if report["ok"] else 2), report
    paths, source = resolve_scored_paths(
        output_dir=output_dir, pattern=pattern, scored_files=scored_files
    )
    report = evaluate_gate_liveness(paths)
    # A green run that does not say what it replayed reads as full coverage
    # whether or not it was.
    report["corpora"] = describe(paths, source)
    return (0 if report["ok"] else 2), report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay every sendability gate over the scored corpus (read-only)."
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--glob",
        default=None,
        help=(
            "Select corpora by glob instead of evals/active_corpora.json. "
            f"Unset means the manifest; pass {DEFAULT_PATTERN!r} to replay everything."
        ),
    )
    parser.add_argument("--scored-file", type=Path, action="append")
    parser.add_argument(
        "--references-only",
        action="store_true",
        help=(
            "Run only the checks that need no scored corpus, so hosted CI can "
            "catch a dead gate without the operator machine's output/ directory."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        code, report = run_eval(
            output_dir=args.output_dir,
            pattern=args.glob,
            scored_files=args.scored_file,
            references_only=args.references_only,
        )
    except (OSError, ValueError, csv.Error) as exc:
        code = 2
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
