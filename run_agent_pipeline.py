#!/usr/bin/env python3
"""Hermes-safe one-command lead pipeline wrapper.

Wraps the existing lead-machine stages (search/enrich -> API-LLM scoring ->
packet tiers -> packets -> optional Airtable) behind a single command with the
safety rails a Telegram agent (Hermes) needs to trigger it unattended:

  * every paid API-LLM run is preceded by a mandatory dry-run cost estimate,
  * the run refuses if the projected cost exceeds --max-llm-usd,
  * a lockfile prevents two pipelines from running at once,
  * fresh scores-dir per paid run, no clobbering previous outputs without
    --overwrite (a new batch is forked instead),
  * machine-greppable status lines ([PIPELINE]/[SEARCH]/[COST]/[SCORING]/
    [PACKETS]/[AIRTABLE]/[DONE]/[REFUSE]) so Hermes can relay state to Telegram.

Examples:
    python run_agent_pipeline.py --vertical detailing --market cleveland \
        --size medium --mode score-dry-run
    python run_agent_pipeline.py --vertical detailing --market cleveland \
        --size medium --mode full --max-llm-usd 1.00 --yes
    python run_agent_pipeline.py --batch detailing_cleveland_medium_001 \
        --vertical detailing --market cleveland --size medium --mode packets-only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lead_machine.sendability import (
    PACKET_TIER_MANUAL,
    PACKET_TIER_NONE,
    PACKET_TIER_SENDABLE,
    PACKET_TIER_WATCHLIST,
)

CONFIGS_DIR = Path("search_configs")
OUTPUT_DIR = Path("output")
DEFAULT_LOCK_FILE = OUTPUT_DIR / ".agent_pipeline.lock"
DEFAULT_MAX_LLM_USD = 1.00
DEFAULT_LIMIT = 25
DEFAULT_SPEND_CONFIRM_USD = 0.50

PROJECTED_USD_RE = re.compile(r"projected_usd=([0-9]+(?:\.[0-9]+)?)")

SEARCH_MODES = {"search-only", "full"}
SCORE_DRY_MODES = {"score-dry-run", "full", "score-confirmed"}
PAID_SCORE_MODES = {"score-confirmed", "full"}
PACKET_MODES = {"packets-only", "score-confirmed", "full"}


class PipelineRefusal(Exception):
    """Raised to abort the run with a [REFUSE] line and a nonzero exit."""


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #
def status(tag: str, message: str) -> None:
    print(f"[{tag}] {message}", flush=True)


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> Any:
    """Run a subprocess. Patched in tests.

    With capture=True the child's stdout is returned (and echoed) so callers can
    parse it; otherwise stdout is inherited.
    """
    status("RUN", " ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if capture and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if check and result.returncode != 0:
        raise PipelineRefusal(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    return result


# --------------------------------------------------------------------------- #
# Lockfile
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(path: Path, batch: str) -> bool:
    """Atomically create the lockfile. Returns False if a live run holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reap a stale lock left by a crashed run (dead PID).
    if path.exists():
        try:
            held_pid = int((path.read_text(encoding="utf-8").split() or ["0"])[0])
        except (ValueError, OSError):
            held_pid = -1
        if held_pid > 0 and not _pid_alive(held_pid):
            status("PIPELINE", f"Reaping stale lock from dead pid {held_pid}")
            try:
                path.unlink()
            except OSError:
                return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()} {batch}\n")
    return True


def release_lock(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Config / batch resolution
# --------------------------------------------------------------------------- #
def _config_path(batch: str, configs_dir: Path) -> Path:
    return configs_dir / f"{batch}.json"


def batch_outputs_exist(batch: str, output_dir: Path) -> bool:
    candidates = [
        output_dir / f"enriched_{batch}.jsonl",
        output_dir / f"scored_{batch}.csv",
        Path(f"packets_{batch}"),
        Path("data") / "raw" / f"{batch}.csv",
    ]
    return any(p.exists() for p in candidates)


def _existing_indexed_configs(base: str, configs_dir: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in configs_dir.glob(f"{base}_[0-9][0-9][0-9].json"):
        try:
            idx = int(path.stem.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            continue
        found.append((idx, path))
    return sorted(found)


def _next_free_index(base: str, configs_dir: Path, output_dir: Path) -> int:
    idx = 1
    while (
        _config_path(f"{base}_{idx:03d}", configs_dir).exists()
        or batch_outputs_exist(f"{base}_{idx:03d}", output_dir)
    ):
        idx += 1
    return idx


def resolve_batch(
    *,
    vertical: str,
    market: str,
    size: str,
    explicit_batch: str,
    mode: str,
    overwrite: bool,
    configs_dir: Path = CONFIGS_DIR,
    output_dir: Path = OUTPUT_DIR,
) -> tuple[str, Path]:
    """Resolve (batch_name, config_path).

    Forks a fresh `<base>_NNN` batch (copying the template config) when a search
    run would otherwise clobber an existing batch's outputs and --overwrite was
    not passed.
    """
    if explicit_batch:
        return explicit_batch.strip(), _config_path(explicit_batch.strip(), configs_dir)

    base = f"{vertical}_{market}_{size}"
    indexed = _existing_indexed_configs(base, configs_dir)
    if not indexed:
        raise PipelineRefusal(
            f"No search config found matching {base}_NNN.json under {configs_dir}/. "
            "Create one (see the add-vertical / add-metro skills) or pass --batch."
        )

    template_idx, template_path = indexed[-1]
    current_batch = template_path.stem

    # Non-search modes operate on the most recent existing batch's outputs.
    if mode not in SEARCH_MODES:
        return current_batch, template_path

    if overwrite or not batch_outputs_exist(current_batch, output_dir):
        return current_batch, template_path

    # Fork: copy the template config to the next free index so prior outputs
    # are left untouched.
    new_idx = _next_free_index(base, configs_dir, output_dir)
    new_batch = f"{base}_{new_idx:03d}"
    new_config_path = _config_path(new_batch, configs_dir)
    template_cfg = json.loads(template_path.read_text(encoding="utf-8"))
    template_cfg["batch"] = new_batch
    new_config_path.write_text(
        json.dumps(template_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    status(
        "PIPELINE",
        f"Outputs for {current_batch} exist; forked fresh batch {new_batch} "
        f"(config {new_config_path}). Pass --overwrite to reuse {current_batch}.",
    )
    return new_batch, new_config_path


# --------------------------------------------------------------------------- #
# Command builders (pure)
# --------------------------------------------------------------------------- #
def build_search_cmd(config_path: Path, batch: str) -> list[str]:
    return [
        sys.executable,
        "make_search_batch.py",
        "--config",
        str(config_path),
        "--batch",
        batch,
    ]


def _score_base_cmd(
    *, enriched: Path, scored: Path, vertical: str, scores_dir: Path, limit: int
) -> list[str]:
    cmd = [
        sys.executable,
        "score_leads.py",
        "--input",
        str(enriched),
        "--output",
        str(scored),
        "--vertical",
        vertical,
        "--mode",
        "api-llm",
        "--scores-dir",
        str(scores_dir),
    ]
    if limit > 0:
        cmd.extend(["--limit", str(limit)])
    return cmd


def build_dry_run_cmd(
    *, enriched: Path, scored: Path, vertical: str, scores_dir: Path, limit: int
) -> list[str]:
    return _score_base_cmd(
        enriched=enriched, scored=scored, vertical=vertical, scores_dir=scores_dir, limit=limit
    ) + ["--dry-run-llm-cost"]


def build_paid_score_cmd(
    *,
    enriched: Path,
    scored: Path,
    vertical: str,
    scores_dir: Path,
    limit: int,
    cards: Path,
    llm_usage: Path,
) -> list[str]:
    return _score_base_cmd(
        enriched=enriched, scored=scored, vertical=vertical, scores_dir=scores_dir, limit=limit
    ) + [
        "--confirm-llm-spend",
        "--fresh-scores-dir",
        "--resume-scored",
        "--cards-output",
        str(cards),
        "--llm-usage-output",
        str(llm_usage),
    ]


def build_refresh_cmd(scored: Path) -> list[str]:
    return [sys.executable, "refresh_packet_tiers.py", str(scored)]


def build_packets_cmd(scored: Path, packets: Path) -> list[str]:
    return [
        sys.executable,
        "make_packets.py",
        "--inputs",
        str(scored),
        "--output-dir",
        str(packets),
    ]


def build_airtable_cmd(
    *, scored: Path, packets: Path, sendable_only: bool, live: bool
) -> list[str]:
    cmd = [
        sys.executable,
        "upload_airtable.py",
        "--input",
        str(scored),
        "--packet-dir",
        str(packets),
    ]
    if sendable_only:
        cmd.append("--only-sendable")
    cmd.append("--live" if live else "--dry-run")
    return cmd


# --------------------------------------------------------------------------- #
# Cost estimation
# --------------------------------------------------------------------------- #
def parse_projected_usd(text: str) -> Optional[float]:
    matches = PROJECTED_USD_RE.findall(text or "")
    if not matches:
        return None
    return float(matches[-1])


def estimate_llm_cost(
    *, enriched: Path, scored: Path, vertical: str, scores_dir: Path, limit: int
) -> float:
    """Run the API-LLM dry-run and parse the projected USD. Never makes a paid call."""
    cmd = build_dry_run_cmd(
        enriched=enriched, scored=scored, vertical=vertical, scores_dir=scores_dir, limit=limit
    )
    result = run(cmd, capture=True)
    projected = parse_projected_usd(getattr(result, "stdout", "") or "")
    if projected is None:
        raise PipelineRefusal("Could not parse projected LLM cost from dry-run output.")
    return projected


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def _raise_csv_limit() -> None:
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    _raise_csv_limit()
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())


def _is_yes(value: Any) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def count_airtable_rows(rows: list[dict[str, str]], *, sendable_only: bool) -> int:
    count = 0
    for row in rows:
        tier = row.get("Packet Tier", "")
        if sendable_only:
            if _is_yes(row.get("Sendable")) and _is_yes(row.get("Packet Eligible")):
                count += 1
        elif tier in {PACKET_TIER_SENDABLE, PACKET_TIER_MANUAL}:
            count += 1
    return count


def build_summary(
    *,
    batch: str,
    mode: str,
    enriched: Path,
    scored: Path,
    packets: Path,
    airtable_status: str,
    airtable_sendable_only: bool,
) -> dict[str, Any]:
    rows = read_csv_rows(scored)
    tier_counts = {
        PACKET_TIER_SENDABLE: 0,
        PACKET_TIER_MANUAL: 0,
        PACKET_TIER_WATCHLIST: 0,
        PACKET_TIER_NONE: 0,
    }
    for row in rows:
        tier = row.get("Packet Tier", "")
        if tier in tier_counts:
            tier_counts[tier] += 1
    packet_index = packets / "packet_index.csv"
    airtable_rows = (
        0
        if airtable_status == "skipped"
        else count_airtable_rows(rows, sendable_only=airtable_sendable_only)
    )
    return {
        "batch": batch,
        "mode": mode,
        "counts": {
            "enriched": count_jsonl(enriched),
            "scored": len(rows),
            "sendable_packet": tier_counts[PACKET_TIER_SENDABLE],
            "manual_review_packet": tier_counts[PACKET_TIER_MANUAL],
            "watchlist": tier_counts[PACKET_TIER_WATCHLIST],
            "no_packet": tier_counts[PACKET_TIER_NONE],
            "airtable_synced": airtable_rows,
        },
        "airtable": {"status": airtable_status, "sendable_only": airtable_sendable_only},
        "artifacts": {
            "scored_csv": str(scored),
            "packet_index": str(packet_index),
            "packets_dir": str(packets),
            "enriched": str(enriched),
        },
    }


def print_summary(summary: dict[str, Any]) -> None:
    counts = summary["counts"]
    artifacts = summary["artifacts"]
    status("DONE", "Pipeline summary")
    print(f"  Batch:                  {summary['batch']}")
    print(f"  Enriched rows:          {counts['enriched']}")
    print(f"  Scored rows:            {counts['scored']}")
    print(f"  Sendable Packets:       {counts['sendable_packet']}")
    print(f"  Manual Review Packets:  {counts['manual_review_packet']}")
    print(f"  Watchlist:              {counts['watchlist']}")
    print(f"  No Packet:              {counts['no_packet']}")
    if summary["airtable"]["status"] != "skipped":
        print(f"  Airtable synced:        {counts['airtable_synced']} ({summary['airtable']['status']})")
    print(f"  Output CSV:             {artifacts['scored_csv']}")
    print(f"  Packet index:           {artifacts['packet_index']}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes-safe lead pipeline wrapper")
    parser.add_argument("--vertical", required=True, help="e.g. detailing, landscaping, hardscaping")
    parser.add_argument("--market", required=True, help="e.g. cleveland")
    parser.add_argument("--size", required=True, choices=["small", "medium", "full"])
    parser.add_argument("--batch", default="", help="Explicit batch name (overrides resolution)")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["search-only", "score-dry-run", "score-confirmed", "packets-only", "full"],
    )
    parser.add_argument("--max-llm-usd", type=float, default=DEFAULT_MAX_LLM_USD,
                        help="Hard ceiling on projected LLM spend; never overridden")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max records for API scoring")
    parser.add_argument("--airtable-sendable-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-manual-review-airtable", action="store_true", default=False)
    parser.add_argument(
        "--airtable",
        choices=["skip", "dry-run", "live"],
        default="skip",
        help="Airtable sync mode; not called unless dry-run/live (default skip)",
    )
    parser.add_argument("--yes", "--confirm-spend", dest="yes", action="store_true",
                        help="Authorize paid scoring above the confirmation threshold")
    parser.add_argument("--overwrite", action="store_true", help="Reuse the existing batch instead of forking")
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE))
    return parser.parse_args(argv)


def _run_pipeline(args: argparse.Namespace) -> int:
    status("PIPELINE", f"mode={args.mode} vertical={args.vertical} market={args.market} size={args.size}")

    batch, config_path = resolve_batch(
        vertical=args.vertical,
        market=args.market,
        size=args.size,
        explicit_batch=args.batch,
        mode=args.mode,
        overwrite=args.overwrite,
    )
    enriched = OUTPUT_DIR / f"enriched_{batch}.jsonl"
    scored = OUTPUT_DIR / f"scored_{batch}.csv"
    cards = OUTPUT_DIR / f"lead_cards_{batch}.jsonl"
    llm_usage = OUTPUT_DIR / f"llm_usage_{batch}.json"
    scores_dir = Path(f"review_queue_{batch}") / "scores"
    packets = Path(f"packets_{batch}")
    summary_path = OUTPUT_DIR / f"agent_run_summary_{batch}.json"

    status("PIPELINE", f"batch={batch} config={config_path}")

    # ---- Search / enrich ------------------------------------------------- #
    if args.mode in SEARCH_MODES:
        if not config_path.exists():
            raise PipelineRefusal(f"Search config not found: {config_path}")
        status("SEARCH", f"Searching + enriching from {config_path}")
        run(build_search_cmd(config_path, batch))
    else:
        status("SEARCH", "skipped (mode does not search)")

    # ---- Scoring --------------------------------------------------------- #
    if args.mode in SCORE_DRY_MODES:
        if not enriched.exists():
            raise PipelineRefusal(f"Enriched file not found, cannot score: {enriched}")

        status("COST", "Estimating API-LLM cost (dry-run, no paid call)")
        projected = estimate_llm_cost(
            enriched=enriched, scored=scored, vertical=args.vertical,
            scores_dir=scores_dir, limit=args.limit,
        )
        status("COST", f"projected_usd={projected:.4f} max_llm_usd={args.max_llm_usd:.2f}")

        if args.mode == "score-dry-run":
            status("COST", "score-dry-run complete; no paid scoring performed.")
        else:
            # Hard ceiling — never overridden, even with --yes.
            if args.max_llm_usd > 0 and projected > args.max_llm_usd:
                raise PipelineRefusal(
                    f"Projected LLM cost ${projected:.4f} exceeds --max-llm-usd "
                    f"${args.max_llm_usd:.2f}. Raise the ceiling or lower --limit."
                )
            confirm_threshold = min(DEFAULT_SPEND_CONFIRM_USD, args.max_llm_usd)
            if projected > confirm_threshold and not args.yes:
                raise PipelineRefusal(
                    f"Projected LLM cost ${projected:.4f} exceeds the confirmation "
                    f"threshold ${confirm_threshold:.2f}. Re-run with --yes / --confirm-spend."
                )

            status("SCORING", f"Running paid API-LLM scoring (projected ${projected:.4f})")
            os.environ["MAX_LLM_USD_PER_RUN"] = str(args.max_llm_usd)
            run(build_paid_score_cmd(
                enriched=enriched, scored=scored, vertical=args.vertical,
                scores_dir=scores_dir, limit=args.limit, cards=cards, llm_usage=llm_usage,
            ))
    else:
        status("SCORING", "skipped (mode does not score)")

    # ---- Packet tiers + packets ------------------------------------------ #
    if args.mode in PACKET_MODES:
        if not scored.exists():
            raise PipelineRefusal(f"Scored CSV not found, cannot build packets: {scored}")
        status("PACKETS", "Refreshing packet tiers")
        run(build_refresh_cmd(scored))
        status("PACKETS", f"Building packets -> {packets}")
        run(build_packets_cmd(scored, packets))
    else:
        status("PACKETS", "skipped (mode does not build packets)")

    # ---- Airtable -------------------------------------------------------- #
    airtable_status = "skipped"
    sendable_only = args.airtable_sendable_only and not args.include_manual_review_airtable
    if args.airtable == "skip":
        status("AIRTABLE", "skipped (not requested)")
    elif not scored.exists():
        status("AIRTABLE", "skipped (no scored CSV)")
    else:
        live = args.airtable == "live"
        airtable_status = "live" if live else "dry-run"
        scope = "sendable-only" if sendable_only else "sendable+manual-review"
        status("AIRTABLE", f"Syncing ({airtable_status}, {scope})")
        run(build_airtable_cmd(scored=scored, packets=packets, sendable_only=sendable_only, live=live))

    # ---- Summary --------------------------------------------------------- #
    summary = build_summary(
        batch=batch,
        mode=args.mode,
        enriched=enriched,
        scored=scored,
        packets=packets,
        airtable_status=airtable_status,
        airtable_sendable_only=sendable_only,
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_summary(summary)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    lock_path = Path(args.lock_file)

    try:
        batch_preview = args.batch or f"{args.vertical}_{args.market}_{args.size}"
        if not acquire_lock(lock_path, batch_preview):
            status("REFUSE", f"Another pipeline run holds the lock ({lock_path}). Try again later.")
            return 2
    except OSError as exc:
        status("REFUSE", f"Could not acquire lock {lock_path}: {exc}")
        return 2

    try:
        return _run_pipeline(args)
    except PipelineRefusal as exc:
        status("REFUSE", str(exc))
        return 2
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
