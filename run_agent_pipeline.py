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
import time
from contextlib import contextmanager
from dataclasses import dataclass
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
DEFAULT_LLM_BATCH_SIZE = 5
DEFAULT_LLM_MAX_TOKENS = 8192

# Preference order for an *existing* scored CSV, most-derived first. The API
# scoring stage writes scored_<batch>_api.csv; the tier refresh writes
# scored_<batch>_api_tiered.csv; the legacy manual flow wrote scored_<batch>.csv.
SCORED_SUFFIX_PREFERENCE = ("_api_tiered", "_api", "")

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
        message = f"Command failed ({result.returncode}): {' '.join(cmd)}"
        if capture:
            # On a captured run the child's output was suppressed; surface both
            # streams so the failure is diagnosable instead of silent.
            message += (
                f"\n[CHILD STDOUT]\n{result.stdout or ''}"
                f"\n[CHILD STDERR]\n{result.stderr or ''}"
            )
        raise PipelineRefusal(message)
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


def check_airtable_lock_compatibility(airtable: str, lock_file: str) -> None:
    """Refuse an Airtable sync that runs behind a batch-private lock.

    `--lock-file` and `--airtable` are individually safe and jointly are not.
    Every output this wrapper writes is keyed by batch name, so a private lock
    correctly lets distinct batches run search, enrich and score in parallel.
    The Airtable base is the one piece of shared state with no batch-keyed
    path, and `upload_airtable`'s fetch_existing_records -> plan_airtable_sync
    -> send_airtable_plan is an unlocked read-then-write that is the only
    cross-batch dedupe there is. Two syncs behind two private locks each read a
    base without the other's rows and both POST, so one business lands twice,
    both rows `Status: New`, both matching QUEUE_FILTER_FORMULA. `mark_sent`
    flips one and the twin is served to a rep again -- the 2026-07-31
    double-contact, by a route no downstream gate can close:
    MARK_SENT_ALLOWED_SOURCE_STATUSES cannot reject the twin, because the twin
    legitimately *is* new.

    So the pair is checked here, once, before the run takes a lock or spends
    anything. Compared by resolved path, not by string: a lock spelled
    absolutely, or with a `..` in it, that lands on the default file still
    serialises and is still allowed.

    SCOPE -- this narrows the route, it does not close it. Two known gaps:

    * `DEFAULT_LOCK_FILE` is CWD-relative (`output/.agent_pipeline.lock`), so
      both sides of the comparison resolve against the working directory. Two
      runs started in different directories each pass this check, take
      *different* lockfiles, and sync to the same base. Today only the fact
      that `.env` lives in one checkout stops that, which is config placement,
      not a control.
    * Every other live-upload path is unlocked: `hermes_commands.cmd_sync_live`
      (documented as the only command that builds `upload_airtable.py --live`),
      `run_pipeline.py --airtable-live`, `make airtable-live`, and
      `upload_airtable.py --live` by hand. None consults this lock.

    The invariant this enforces is "which lock this wrapper was told to hold";
    the invariant actually wanted is "one writer per Airtable base at a time",
    which belongs in `upload_airtable.py` keyed on base+table. Until that
    exists, treat this as one door bolted on a building with five others.
    """
    if airtable == "skip":
        return
    if Path(lock_file).resolve() == DEFAULT_LOCK_FILE.resolve():
        return
    raise PipelineRefusal(
        f"--airtable {airtable} requires the default lock file "
        f"({DEFAULT_LOCK_FILE}), but --lock-file is {lock_file}. A private "
        "lock lets another pipeline run at the same time, and the Airtable "
        "sync is an unlocked read-then-write against a base that is shared by "
        "every batch: two concurrent syncs each read the base without the "
        "other's records and both POST, creating two Status=New records for "
        "one business and double-contacting it. Drop --lock-file so runs "
        "serialise on the shared lock and let this run do the sync. If you "
        "must keep the private lock, pass --airtable skip -- but note that "
        "syncing later by hand (/sync-live, make airtable-live, "
        "run_pipeline.py --airtable-live) takes no lock at all, so do it only "
        "when no other pipeline is running."
    )


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


def _generate_config_or_refuse(
    *, vertical: str, market: str, size: str, configs_dir: Path
) -> None:
    """Generate ``<vertical>_<market>_<size>_001.json`` from profiles.

    Imported lazily so the wrapper has no hard dependency on the generator. A
    missing vertical/market profile (or any generation error) is re-raised as a
    PipelineRefusal carrying the generator's ``[REFUSE]`` message. Never makes a
    paid call — config generation is pure file I/O.
    """
    try:
        import generate_search_config as gsc
    except ImportError as exc:  # pragma: no cover - generator ships with the repo
        raise PipelineRefusal(f"Config generator unavailable: {exc}") from exc
    try:
        gsc.generate_config(
            vertical=vertical, market=market, size=size, configs_dir=configs_dir
        )
    except gsc.ConfigGenError as exc:
        raise PipelineRefusal(str(exc)) from exc


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
        # No existing config: try to generate one from vertical + market
        # profiles (pure file I/O, never a paid call). Refuses with a clear
        # [REFUSE] line if a profile is missing.
        _generate_config_or_refuse(
            vertical=vertical, market=market, size=size, configs_dir=configs_dir
        )
        indexed = _existing_indexed_configs(base, configs_dir)
        if not indexed:
            raise PipelineRefusal(
                f"No search config found matching {base}_NNN.json under {configs_dir}/ "
                "and config generation produced nothing. Pass --batch or add profiles "
                "(vertical_profiles/, market_profiles/)."
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
# Scored-CSV path resolution
# --------------------------------------------------------------------------- #
@dataclass
class PipelinePaths:
    """Resolved file locations for a single pipeline run."""

    enriched: Path
    scored_csv: Optional[Path]  # existing scored CSV read for resume / packets input
    scored_output: Path  # where API scoring writes (score modes)
    tiered_csv: Path  # where refreshed packet tiers are written
    packets: Path
    report_csv: Path  # file whose rows are counted in the summary


def best_existing_scored(batch: str, output_dir: Path = OUTPUT_DIR) -> Optional[Path]:
    """Return the most-derived existing scored CSV for `batch`, or None."""
    for suffix in SCORED_SUFFIX_PREFERENCE:
        candidate = output_dir / f"scored_{batch}{suffix}.csv"
        if candidate.exists():
            return candidate
    return None


def _tiered_default_for(source: Path, batch: str, output_dir: Path) -> Path:
    """Default tier-refresh target for a given scored source.

    An `*_api.csv` source refreshes into `scored_<batch>_api_tiered.csv`; an
    already-tiered or legacy plain CSV is refreshed in place.
    """
    if source.name.endswith("_api.csv"):
        return output_dir / f"scored_{batch}_api_tiered.csv"
    return source


def resolve_pipeline_paths(
    *,
    batch: str,
    mode: str,
    args: argparse.Namespace,
    output_dir: Path = OUTPUT_DIR,
) -> PipelinePaths:
    enriched = output_dir / f"enriched_{batch}.jsonl"
    default_api = output_dir / f"scored_{batch}_api.csv"
    packets = Path(f"packets_{batch}")

    if mode in SCORE_DRY_MODES:
        # Scoring writes (and resumes from) the API CSV unless explicitly retargeted.
        scored_output = Path(args.scored_output) if args.scored_output else default_api
        source: Optional[Path] = scored_output
    else:
        # Non-scoring modes operate on an existing scored CSV.
        if args.scored_csv:
            source = Path(args.scored_csv)
        else:
            source = best_existing_scored(batch, output_dir)
        scored_output = source if source is not None else default_api

    if args.tiered_output:
        tiered = Path(args.tiered_output)
    elif source is not None:
        tiered = _tiered_default_for(source, batch, output_dir)
    else:
        tiered = output_dir / f"scored_{batch}_api_tiered.csv"

    report = tiered if mode in PACKET_MODES else (source if source is not None else default_api)

    return PipelinePaths(
        enriched=enriched,
        scored_csv=source,
        scored_output=scored_output,
        tiered_csv=tiered,
        packets=packets,
        report_csv=report,
    )


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
    *,
    enriched: Path,
    scored: Path,
    vertical: str,
    scores_dir: Path,
    limit: int,
    llm_batch_size: int,
    llm_max_tokens: int,
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
        "--llm-batch-size",
        str(llm_batch_size),
        "--llm-max-tokens",
        str(llm_max_tokens),
    ]
    if limit > 0:
        cmd.extend(["--limit", str(limit)])
    return cmd


def build_dry_run_cmd(
    *,
    enriched: Path,
    scored: Path,
    vertical: str,
    scores_dir: Path,
    limit: int,
    llm_batch_size: int,
    llm_max_tokens: int,
) -> list[str]:
    return _score_base_cmd(
        enriched=enriched, scored=scored, vertical=vertical, scores_dir=scores_dir,
        limit=limit, llm_batch_size=llm_batch_size, llm_max_tokens=llm_max_tokens,
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
    llm_batch_size: int,
    llm_max_tokens: int,
) -> list[str]:
    return _score_base_cmd(
        enriched=enriched, scored=scored, vertical=vertical, scores_dir=scores_dir,
        limit=limit, llm_batch_size=llm_batch_size, llm_max_tokens=llm_max_tokens,
    ) + [
        "--confirm-llm-spend",
        "--fresh-scores-dir",
        "--resume-scored",
        "--cards-output",
        str(cards),
        "--llm-usage-output",
        str(llm_usage),
    ]


def build_refresh_cmd(source: Path, output: Optional[Path] = None) -> list[str]:
    cmd = [sys.executable, "refresh_packet_tiers.py", str(source)]
    if output is not None and Path(output) != Path(source):
        cmd.extend(["--output", str(output)])
    return cmd


def build_packets_cmd(scored: Path, packets: Path) -> list[str]:
    return [
        sys.executable,
        "make_packets.py",
        "--inputs",
        str(scored),
        "--output-dir",
        str(packets),
    ]


def build_airtable_cmd(*, scored: Path, packets: Path, live: bool) -> list[str]:
    cmd = [
        sys.executable,
        "upload_airtable.py",
        "--input",
        str(scored),
        "--packet-dir",
        str(packets),
    ]
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
    *,
    enriched: Path,
    scored: Path,
    vertical: str,
    scores_dir: Path,
    limit: int,
    llm_batch_size: int,
    llm_max_tokens: int,
) -> float:
    """Run the API-LLM dry-run and parse the projected USD. Never makes a paid call."""
    cmd = build_dry_run_cmd(
        enriched=enriched, scored=scored, vertical=vertical, scores_dir=scores_dir,
        limit=limit, llm_batch_size=llm_batch_size, llm_max_tokens=llm_max_tokens,
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


def count_airtable_rows(rows: list[dict[str, str]]) -> int:
    return len(rows)


class StageClock:
    """Wall clock per stage, and the record the summary reports it from.

    The perf plan's own verification step was "record wall clock per stage so the
    win is measured, not asserted" — and nothing here timed anything, so every
    performance number in this project was measured by hand and cannot be
    reproduced from the artifacts. A stage records even when it is skipped or
    fails: "packets took 0.0s because the mode does not build packets" and
    "packets failed after 12s" are both things an operator needs to be able to
    read off the summary without having the terminal still open.
    """

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._stages: list[dict[str, Any]] = []

    def record(self, stage: str, *, seconds: float, status_: str) -> None:
        self._stages.append({"stage": stage, "seconds": round(seconds, 3), "status": status_})

    def skipped(self, stage: str) -> None:
        self.record(stage, seconds=0.0, status_="skipped")

    def stages(self) -> list[dict[str, Any]]:
        return list(self._stages)

    def elapsed(self) -> float:
        return round(time.perf_counter() - self._started, 3)


def build_summary(
    *,
    batch: str,
    mode: str,
    enriched: Path,
    scored: Path,
    packets: Path,
    airtable_status: str,
    clock: StageClock | None = None,
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
        else count_airtable_rows(rows)
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
        "airtable": {"status": airtable_status, "scope": "all_scored_rows"},
        "stages": clock.stages() if clock is not None else [],
        "elapsed_seconds": clock.elapsed() if clock is not None else 0.0,
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
    for entry in summary.get("stages") or []:
        print(f"  Stage {entry['stage']:<16} {entry['seconds']:>8.1f}s  ({entry['status']})")
    if summary.get("elapsed_seconds"):
        print(f"  Total elapsed:          {summary['elapsed_seconds']:.1f}s")


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
    parser.add_argument("--llm-batch-size", type=int, default=DEFAULT_LLM_BATCH_SIZE,
                        help="Leads per API-LLM batch (passed to score_leads.py)")
    parser.add_argument("--llm-max-tokens", type=int, default=DEFAULT_LLM_MAX_TOKENS,
                        help="Max output tokens per API-LLM batch (passed to score_leads.py)")
    parser.add_argument("--scored-csv", default="",
                        help="Existing scored CSV to use (overrides default resolution)")
    parser.add_argument("--scored-output", default="",
                        help="Target scored CSV for API scoring (default scored_<batch>_api.csv)")
    parser.add_argument("--tiered-output", default="",
                        help="Target CSV for refreshed packet tiers "
                             "(default scored_<batch>_api_tiered.csv when source is *_api.csv)")
    parser.add_argument("--allow-empty-scored", action="store_true",
                        help="Permit building packets from a 0-row scored CSV")
    parser.add_argument(
        "--airtable",
        choices=["skip", "dry-run", "live"],
        default="skip",
        help="Airtable sync mode; not called unless dry-run/live (default skip)",
    )
    parser.add_argument("--yes", "--confirm-spend", dest="yes", action="store_true",
                        help="Authorize paid scoring above the confirmation threshold")
    parser.add_argument("--overwrite", action="store_true", help="Reuse the existing batch instead of forking")
    parser.add_argument(
        "--lock-file",
        default=str(DEFAULT_LOCK_FILE),
        help=(
            f"Mutual-exclusion lockfile (default {DEFAULT_LOCK_FILE}). The default is global, so "
            "two runs serialise. Every output this wrapper touches is keyed by batch name "
            "(output/enriched_<batch>.jsonl, output/scored_<batch>*.csv, packets_<batch>/, "
            "review_queue_<batch>/, data/raw/<batch>.csv, reports/search_<batch>.md, ...), so "
            "giving each batch its own lock file (e.g. output/.agent_pipeline.<batch>.lock) lets "
            "distinct batches run in parallel through search, enrich and score. NOT through the "
            "Airtable sync, which is refused outright when this is not the default lock: use "
            "--mode search-only or --airtable skip. The base is shared state no "
            "batch-keyed path names, and upload_airtable's fetch_existing_records -> "
            "plan_airtable_sync -> send_airtable_plan is an unlocked read-then-write that is the "
            "only cross-batch dedupe there is -- two parallel syncs both read a base without the "
            "other's records and both POST, and since batch geographies overlap that means two "
            "records for one business, both Status=New, both served to the send queue. "
            "Pass an explicit --batch when running parallel: two concurrent runs that share "
            "--vertical/--market/--size and let the wrapper "
            "fork a name would race on the same next-free index and land on the same batch. Once "
            "parallel, the binding limits stop being this lock and become the Places API QPS/quota "
            "and the LLM provider's rate limits, which the per-run MAX_* caps do not coordinate "
            "across processes. See docs/hermes-commands.md#parallel-batch-runs."
        ),
    )
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
    paths = resolve_pipeline_paths(batch=batch, mode=args.mode, args=args)
    enriched = paths.enriched
    cards = OUTPUT_DIR / f"lead_cards_{batch}.jsonl"
    llm_usage = OUTPUT_DIR / f"llm_usage_{batch}.json"
    scores_dir = Path(f"review_queue_{batch}") / "scores"
    packets = paths.packets
    summary_path = OUTPUT_DIR / f"agent_run_summary_{batch}.json"

    status("PIPELINE", f"batch={batch} config={config_path}")
    status("PIPELINE", f"enriched_input={enriched}")
    status("PIPELINE", f"scored_csv={paths.scored_csv if paths.scored_csv is not None else 'none'}")
    status("PIPELINE", f"tiered_csv={paths.tiered_csv}")
    status("PIPELINE", f"packets_dir={packets}")

    # State the summary is built from. Hoisted above the stages because the
    # summary is now rewritten as each stage closes, not once at the end.
    report_csv = paths.report_csv
    airtable_status = "skipped"
    clock = StageClock()

    def flush_summary() -> dict[str, Any]:
        summary = build_summary(
            batch=batch,
            mode=args.mode,
            enriched=enriched,
            scored=report_csv,
            packets=packets,
            airtable_status=airtable_status,
            clock=clock,
        )
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary

    @contextmanager
    def stage(name: str):
        """Time a stage, and leave a truthful summary behind whatever it does.

        The flush is in `finally` on purpose: a run that refuses at packets used
        to leave the *previous* run's summary on disk, so the artifact described
        a run that was days old. A partial summary of this run beats a complete
        summary of a different one.
        """
        started = time.perf_counter()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            clock.record(name, seconds=time.perf_counter() - started, status_="failed" if failed else "ok")
            flush_summary()

    # ---- Search / enrich ------------------------------------------------- #
    if args.mode in SEARCH_MODES:
        with stage("search"):
            if not config_path.exists():
                raise PipelineRefusal(f"Search config not found: {config_path}")
            status("SEARCH", f"Searching + enriching from {config_path}")
            run(build_search_cmd(config_path, batch))
    else:
        status("SEARCH", "skipped (mode does not search)")
        clock.skipped("search")

    # ---- Scoring --------------------------------------------------------- #
    if args.mode in SCORE_DRY_MODES:
        with stage("scoring"):
            if not enriched.exists():
                raise PipelineRefusal(f"Enriched file not found, cannot score: {enriched}")

            status("COST", "Estimating API-LLM cost (dry-run, no paid call)")
            projected = estimate_llm_cost(
                enriched=enriched, scored=paths.scored_output, vertical=args.vertical,
                scores_dir=scores_dir, limit=args.limit,
                llm_batch_size=args.llm_batch_size, llm_max_tokens=args.llm_max_tokens,
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

                status("SCORING", f"Running paid API-LLM scoring (projected ${projected:.4f}) -> {paths.scored_output}")
                os.environ["MAX_LLM_USD_PER_RUN"] = str(args.max_llm_usd)
                run(build_paid_score_cmd(
                    enriched=enriched, scored=paths.scored_output, vertical=args.vertical,
                    scores_dir=scores_dir, limit=args.limit, cards=cards, llm_usage=llm_usage,
                    llm_batch_size=args.llm_batch_size, llm_max_tokens=args.llm_max_tokens,
                ))
    else:
        status("SCORING", "skipped (mode does not score)")
        clock.skipped("scoring")

    # ---- Packet tiers + packets ------------------------------------------ #
    if args.mode in PACKET_MODES:
        with stage("packets"):
            source = paths.scored_csv
            if source is None or not source.exists():
                raise PipelineRefusal(
                    "No scored CSV found to build packets from. Tried (in order) "
                    f"{', '.join('scored_' + batch + s + '.csv' for s in SCORED_SUFFIX_PREFERENCE)}. "
                    "Run scoring first or pass --scored-csv."
                )
            existing_rows = read_csv_rows(source)
            if not existing_rows and not args.allow_empty_scored:
                raise PipelineRefusal(
                    f"Selected scored CSV has 0 rows: {source}. "
                    "Run scoring first or pass --allow-empty-scored to proceed anyway."
                )
            status("PACKETS", f"Refreshing packet tiers from {source} -> {paths.tiered_csv}")
            run(build_refresh_cmd(source, paths.tiered_csv))
            status("PACKETS", f"Building packets -> {packets}")
            run(build_packets_cmd(paths.tiered_csv, packets))
    else:
        status("PACKETS", "skipped (mode does not build packets)")
        clock.skipped("packets")

    # ---- Airtable -------------------------------------------------------- #
    if args.airtable == "skip":
        status("AIRTABLE", "skipped (not requested)")
        clock.skipped("airtable")
    elif not report_csv.exists():
        status("AIRTABLE", "skipped (no scored CSV)")
        clock.skipped("airtable")
    else:
        with stage("airtable"):
            live = args.airtable == "live"
            airtable_status = "live" if live else "dry-run"
            status("AIRTABLE", f"Syncing ({airtable_status}, all scored rows)")
            run(build_airtable_cmd(scored=report_csv, packets=packets, live=live))

    # ---- Summary --------------------------------------------------------- #
    print_summary(flush_summary())
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    lock_path = Path(args.lock_file)

    # Before the lock, not after: a refusal must leave no lockfile behind, and
    # nothing may run ahead of it.
    try:
        check_airtable_lock_compatibility(args.airtable, args.lock_file)
    except PipelineRefusal as exc:
        status("REFUSE", str(exc))
        return 2

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
