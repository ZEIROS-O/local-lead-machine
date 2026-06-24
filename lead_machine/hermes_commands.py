#!/usr/bin/env python3
"""Deterministic command layer for the Hermes/Telegram lead-pipeline driver.

A non-deterministic LLM (the external Hermes Agent) must never construct
money-spending commands. So all command grammar, validation, money-safety, and
command construction live here, in a pure, importable, unit-tested module. Hermes
is reduced to transport: its routing skill shells out to

    python -m lead_machine.hermes_commands <verb> ... [--execute]

and relays this module's stdout verbatim. The real safety boundary is still
``run_agent_pipeline.py`` -- this module only *builds and forwards* commands to
it (and writes profile JSON for create-*); it never re-implements cost
estimation, batch forking, or Airtable logic.

Verbs: run, confirm, status, create-vertical, create-market, help.

Preview-by-default: every mutating verb prints the exact command(s)/JSON it
*would* run and exits 0 with no side effects. It only acts (subprocess for
run/confirm, file write for create-*) when ``--execute`` is passed. ``status``
and ``help`` are read-only/static and always act.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Reuse, don't redefine: size set + profile dirs from the generator, packet-tier
# strings from sendability, the cap default + scored-CSV resolver from the
# pipeline. Importing these modules is side-effect-free (no paid calls, no
# argparse at import) and avoids the optional ``tldextract`` dependency.
from generate_search_config import (
    VALID_SIZES,
    VERTICAL_PROFILES_DIR,
    MARKET_PROFILES_DIR,
)
from lead_machine.sendability import (
    PACKET_TIER_SENDABLE,
    PACKET_TIER_MANUAL,
    PACKET_TIER_WATCHLIST,
    PACKET_TIER_NONE,
)
from run_agent_pipeline import (
    DEFAULT_MAX_LLM_USD,
    best_existing_scored,
)

PIPELINE_SCRIPT = "run_agent_pipeline.py"
OUTPUT_DIR = Path("output")
RAW_DIR = Path("data") / "raw"
PACKETS_ROOT = Path(".")

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Status lines emitted by run_agent_pipeline that we parse back out of stdout.
BATCH_RE = re.compile(r"\[PIPELINE\] batch=(\S+)")
PROJECTED_RE = re.compile(r"projected_usd=([0-9]+(?:\.[0-9]+)?)")
REFUSE_RE = re.compile(r"\[REFUSE\].*")

# Telegram message budget; summaries must stay well under this.
TELEGRAM_BUDGET = 3500
# Placeholder shown in /run preview for the batch id that only exists after the
# search step actually runs and (possibly) forks.
BATCH_PLACEHOLDER = "<batch-from-step-1>"


class CommandError(Exception):
    """Raised to abort a verb with a one-line refusal (exit code 2)."""


@dataclass
class CommandResult:
    message: str
    exit_code: int = 0


@dataclass
class ParsedBatch:
    vertical: str
    market: str
    size: str
    seq: str
    name: str


# --------------------------------------------------------------------------- #
# Validation / parsing (pure)
# --------------------------------------------------------------------------- #
def validate_slug(slug: str) -> str:
    """Return the slug if it is a safe identifier, else raise CommandError.

    ``^[a-z][a-z0-9_]*$`` blocks path separators, ``..``, a leading underscore,
    and uppercase -- preventing path traversal and unparseable batch names.
    """
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise CommandError(
            f"Invalid slug '{slug}'. Use lowercase letters, digits and underscores "
            "only (must start with a letter), e.g. 'epoxy' or 'pool_builders'."
        )
    return slug


def validate_size(size: str) -> str:
    if size not in VALID_SIZES:
        raise CommandError(
            f"Invalid size '{size}'. Expected one of {', '.join(VALID_SIZES)}."
        )
    return size


def _profile_stems(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {p.stem for p in directory.glob("*.json")}


def known_verticals(vertical_dir: Path = VERTICAL_PROFILES_DIR) -> set[str]:
    return _profile_stems(vertical_dir)


def known_markets(market_dir: Path = MARKET_PROFILES_DIR) -> set[str]:
    return _profile_stems(market_dir)


def parse_batch(
    name: str,
    *,
    vertical_dir: Path = VERTICAL_PROFILES_DIR,
    market_dir: Path = MARKET_PROFILES_DIR,
) -> ParsedBatch:
    """Profile-aware parse of ``<vertical>_<market>_<size>_NNN``.

    Multi-word slugs (``pool_builders``) survive because we strip the trailing
    ``_NNN`` (digits) and ``_<size>`` (in VALID_SIZES) first, then match the
    remainder against known vertical+market stems (longest known vertical prefix
    whose suffix is a known market). Legacy market-first names do not resolve and
    are rejected.
    """
    raw = (name or "").strip()
    parts = raw.split("_")
    if len(parts) < 4:
        raise CommandError(
            f"Unrecognized batch '{name}'. Expected <vertical>_<market>_<size>_NNN "
            "(e.g. epoxy_cleveland_medium_001)."
        )
    seq = parts[-1]
    if not seq.isdigit():
        raise CommandError(
            f"Unrecognized batch '{name}': trailing '_{seq}' is not a numeric index."
        )
    size = parts[-2]
    if size not in VALID_SIZES:
        raise CommandError(
            f"Unrecognized batch '{name}': '{size}' is not a size "
            f"({', '.join(VALID_SIZES)})."
        )
    remainder = "_".join(parts[:-2])
    verticals = known_verticals(vertical_dir)
    markets = known_markets(market_dir)
    for vertical in sorted(verticals, key=len, reverse=True):
        prefix = f"{vertical}_"
        if remainder.startswith(prefix):
            market = remainder[len(prefix):]
            if market in markets:
                return ParsedBatch(
                    vertical=vertical, market=market, size=size, seq=seq, name=raw
                )
    raise CommandError(
        f"Could not resolve batch '{name}' to a known vertical + market. "
        "Check vertical_profiles/ and market_profiles/, or /create-* the profile."
    )


def split_terms(value: Optional[str]) -> list[str]:
    """Semicolon-split a CLI value into a trimmed, non-empty list."""
    if not value:
        return []
    return [t.strip() for t in str(value).split(";") if t.strip()]


# --------------------------------------------------------------------------- #
# Command builders (pure -- return argv lists; never run anything)
# --------------------------------------------------------------------------- #
def _fmt_cap(cap: float) -> str:
    return f"{cap:.2f}"


def build_run_commands(
    vertical: str,
    market: str,
    size: str,
    *,
    cap: float = DEFAULT_MAX_LLM_USD,
    batch: Optional[str] = None,
) -> list[list[str]]:
    """Two zero-LLM commands: search-only, then score-dry-run.

    No ``--yes``, never ``--mode full``; always passes ``--max-llm-usd``. The
    dry-run step needs the resolved batch id, which only exists after step 1
    runs (and possibly forks), so preview passes a placeholder.
    """
    search = [
        sys.executable, PIPELINE_SCRIPT,
        "--vertical", vertical, "--market", market, "--size", size,
        "--mode", "search-only", "--max-llm-usd", _fmt_cap(cap),
    ]
    dry = [
        sys.executable, PIPELINE_SCRIPT,
        "--vertical", vertical, "--market", market, "--size", size,
        "--batch", batch or BATCH_PLACEHOLDER,
        "--mode", "score-dry-run", "--max-llm-usd", _fmt_cap(cap),
    ]
    return [search, dry]


def build_confirm_command(
    vertical: str,
    market: str,
    size: str,
    batch: str,
    *,
    cap: float = DEFAULT_MAX_LLM_USD,
) -> list[str]:
    """The single paid command: score-confirmed + packets + live Airtable.

    ``--yes`` is added here and only here. Sendable-only is the pipeline default,
    so ``--include-manual-review-airtable`` is deliberately omitted.
    """
    return [
        sys.executable, PIPELINE_SCRIPT,
        "--vertical", vertical, "--market", market, "--size", size,
        "--batch", batch,
        "--mode", "score-confirmed", "--yes",
        "--max-llm-usd", _fmt_cap(cap),
        "--airtable", "live",
    ]


def build_vertical_profile(
    slug: str,
    *,
    display: str = "",
    airtable: str = "",
    terms: Optional[list[str]] = None,
    high_intent: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    min_rating: Optional[float] = None,
    min_reviews: Optional[int] = None,
) -> dict:
    profile: dict = {
        "vertical": slug,
        "display_name": display or slug.replace("_", " ").title(),
        "airtable_vertical": airtable or "",
        "search_terms": list(terms or []),
        "high_intent_terms": list(high_intent or []),
        "exclude_terms": list(exclude or []),
    }
    if min_rating is not None:
        profile["min_rating"] = float(min_rating)
    if min_reviews is not None:
        profile["min_reviews"] = int(min_reviews)
    return profile


def build_market_profile(
    slug: str,
    *,
    display: str = "",
    state: str = "",
    small: Optional[list[str]] = None,
    medium: Optional[list[str]] = None,
    full: Optional[list[str]] = None,
) -> dict:
    """Build a market profile dict.

    ``geos`` (the field generate_search_config requires) is ``--full`` when
    given, else the larger of small/medium -- because the generator always uses
    all ``geos`` for size 'full'. So when ``--full`` is omitted, full == medium.
    """
    small = list(small or [])
    medium = list(medium or [])
    if full:
        geos = list(full)
    else:
        geos = medium if len(medium) >= len(small) else small
    return {
        "market": slug,
        "display_name": display or slug.replace("_", " ").title(),
        "state": state or "",
        "geos": geos,
        "sizes": {"small": small, "medium": medium},
    }


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def render_command(argv: list[str]) -> str:
    """Human/Telegram rendering: 'python <script> ...' (hides the interp path)."""
    rest = argv[1:]
    return "python " + " ".join(_quote(a) for a in rest)


def _truncate(text: str, budget: int = TELEGRAM_BUDGET) -> str:
    if len(text) <= budget:
        return text
    # Keep the tail (the actionable status/refusal lines) over the head.
    keep = text[-(budget - 1):]
    return "…" + keep


def _last_refusal(text: str) -> Optional[str]:
    matches = REFUSE_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def _grep_int(pattern: str, text: str) -> Optional[int]:
    m = re.search(pattern, text or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Subprocess (only reached in --execute; patched in tests)
# --------------------------------------------------------------------------- #
def _exec(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, text=True, capture_output=True)


# --------------------------------------------------------------------------- #
# Verb handlers
# --------------------------------------------------------------------------- #
def cmd_run(
    vertical: str,
    market: str,
    size: str,
    *,
    cap: float = DEFAULT_MAX_LLM_USD,
    execute: bool = False,
    vertical_dir: Path = VERTICAL_PROFILES_DIR,
    market_dir: Path = MARKET_PROFILES_DIR,
) -> CommandResult:
    validate_slug(vertical)
    validate_slug(market)
    validate_size(size)

    if not (vertical_dir / f"{vertical}.json").exists():
        raise CommandError(
            f"No vertical profile '{vertical}'. Create it first: "
            f"/create-vertical {vertical} --terms \"…\""
        )
    if not (market_dir / f"{market}.json").exists():
        raise CommandError(
            f"No market profile '{market}'. Create it first: "
            f"/create-market {market} --small \"…\" --medium \"…\""
        )

    search_cmd, dry_cmd = build_run_commands(vertical, market, size, cap=cap)

    if not execute:
        msg = (
            f"▶ preview /run {vertical} {market} {size} "
            f"(zero LLM spend; reply with --execute to actually search)\n"
            f"  1. {render_command(search_cmd)}\n"
            f"  2. {render_command(dry_cmd)}"
        )
        return CommandResult(_truncate(msg))

    # --- execute: search (Places spend), then dry-run cost (no LLM) --------- #
    searched = _exec(search_cmd)
    out = (searched.stdout or "") + "\n" + (searched.stderr or "")
    if searched.returncode != 0:
        reason = _last_refusal(out) or f"search exited {searched.returncode}"
        return CommandResult(f"failed: {reason}", exit_code=2)
    bm = BATCH_RE.search(out)
    if not bm:
        return CommandResult(
            "failed: search produced no [PIPELINE] batch= line; cannot continue.",
            exit_code=2,
        )
    batch = bm.group(1)
    enriched = _grep_int(r"Enriched rows:\s*(\d+)", out)

    dry_cmd = build_run_commands(vertical, market, size, cap=cap, batch=batch)[1]
    dry = _exec(dry_cmd)
    dout = (dry.stdout or "") + "\n" + (dry.stderr or "")
    if dry.returncode != 0:
        reason = _last_refusal(dout) or f"cost estimate exited {dry.returncode}"
        return CommandResult(f"failed: {reason}", exit_code=2)
    pm = PROJECTED_RE.search(dout)
    projected = float(pm.group(1)) if pm else None

    n = enriched if enriched is not None else "?"
    cost = f"${projected:.2f}" if projected is not None else "?"
    msg = (
        f"▶ {batch} · searched {n} leads · est. scoring {cost}. "
        f"Reply /confirm {batch} to score + build packets + sync sendable to Airtable."
    )
    return CommandResult(_truncate(msg))


def cmd_confirm(
    batch: str,
    *,
    cap: float = DEFAULT_MAX_LLM_USD,
    execute: bool = False,
    vertical_dir: Path = VERTICAL_PROFILES_DIR,
    market_dir: Path = MARKET_PROFILES_DIR,
    output_dir: Path = OUTPUT_DIR,
) -> CommandResult:
    parsed = parse_batch(batch, vertical_dir=vertical_dir, market_dir=market_dir)
    enriched = output_dir / f"enriched_{parsed.name}.jsonl"
    if not enriched.exists():
        raise CommandError(
            f"Batch '{parsed.name}' has not been searched yet (no {enriched}). "
            f"Run /run {parsed.vertical} {parsed.market} {parsed.size} first."
        )

    cmd = build_confirm_command(
        parsed.vertical, parsed.market, parsed.size, parsed.name, cap=cap
    )

    if not execute:
        msg = (
            f"✓ preview /confirm {parsed.name} "
            f"(paid scoring + packets + LIVE Airtable; reply with --execute to spend)\n"
            f"  {render_command(cmd)}"
        )
        return CommandResult(_truncate(msg))

    result = _exec(cmd)
    out = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        reason = _last_refusal(out) or f"pipeline exited {result.returncode}"
        return CommandResult(f"failed: {reason}", exit_code=2)

    scored = _grep_int(r"Scored rows:\s*(\d+)", out)
    sendable = _grep_int(r"Sendable Packets:\s*(\d+)", out)
    manual = _grep_int(r"Manual Review Packets:\s*(\d+)", out)
    synced = _grep_int(r"Airtable synced:\s*(\d+)", out)
    pm = PROJECTED_RE.search(out)
    spend = f"${float(pm.group(1)):.2f}" if pm else "?"
    packets = (sendable or 0) + (manual or 0)

    msg = (
        f"✅ {parsed.name} · scored {scored if scored is not None else '?'} · "
        f"sendable {sendable if sendable is not None else '?'} · "
        f"manual {manual if manual is not None else '?'} · "
        f"packets {packets} · "
        f"Airtable synced {synced if synced is not None else '?'} (sendable-only) · "
        f"est. spend {spend}."
    )
    return CommandResult(_truncate(msg))


@dataclass
class StatusReport:
    batch: str
    searched: bool
    enriched: bool
    scored: bool
    packets: bool
    tier_counts: dict[str, int]
    enriched_newer_than_scored: bool = False

    @property
    def anything(self) -> bool:
        return self.searched or self.enriched or self.scored or self.packets


def _count_tiers(packet_index: Path) -> dict[str, int]:
    import csv

    counts = {
        PACKET_TIER_SENDABLE: 0,
        PACKET_TIER_MANUAL: 0,
        PACKET_TIER_WATCHLIST: 0,
        PACKET_TIER_NONE: 0,
    }
    if not packet_index.exists():
        return counts
    with packet_index.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tier = (row.get("Packet Tier") or "").strip()
            if tier in counts:
                counts[tier] += 1
    return counts


def status_report(
    batch: str,
    *,
    output_dir: Path = OUTPUT_DIR,
    raw_dir: Path = RAW_DIR,
    packets_root: Path = PACKETS_ROOT,
) -> StatusReport:
    raw = raw_dir / f"{batch}.csv"
    enriched = output_dir / f"enriched_{batch}.jsonl"
    scored = best_existing_scored(batch, output_dir)
    packet_index = packets_root / f"packets_{batch}" / "packet_index.csv"

    enriched_newer = False
    if enriched.exists() and scored is not None and scored.exists():
        enriched_newer = enriched.stat().st_mtime > scored.stat().st_mtime

    return StatusReport(
        batch=batch,
        searched=raw.exists(),
        enriched=enriched.exists(),
        scored=scored is not None and scored.exists(),
        packets=packet_index.exists(),
        tier_counts=_count_tiers(packet_index),
        enriched_newer_than_scored=enriched_newer,
    )


def cmd_status(
    batch: str,
    *,
    output_dir: Path = OUTPUT_DIR,
    raw_dir: Path = RAW_DIR,
    packets_root: Path = PACKETS_ROOT,
) -> CommandResult:
    rep = status_report(
        batch, output_dir=output_dir, raw_dir=raw_dir, packets_root=packets_root
    )
    if not rep.anything:
        return CommandResult(f"📊 {batch} — nothing run yet. Try /run to start it.")

    stages = [
        ("search", rep.searched),
        ("enrich", rep.enriched),
        ("score", rep.scored),
        ("packets", rep.packets),
    ]
    flags = " · ".join(f"{name} {'✓' if ok else '✗'}" for name, ok in stages)
    first_missing = next((name for name, ok in stages if not ok), None)

    tc = rep.tier_counts
    tiers = (
        f"Sendable {tc[PACKET_TIER_SENDABLE]} · Manual {tc[PACKET_TIER_MANUAL]} · "
        f"Watchlist {tc[PACKET_TIER_WATCHLIST]} · No Packet {tc[PACKET_TIER_NONE]}"
    )
    msg = f"📊 {batch} — {flags} | {tiers}"
    if first_missing:
        msg += f"\nNext stage: {first_missing}."
    if rep.enriched_newer_than_scored:
        msg += "\n⚠ enriched is newer than scored — outputs may be stale; re-/confirm."
    return CommandResult(_truncate(msg))


def cmd_create_vertical(
    slug: str,
    *,
    display: str = "",
    airtable: str = "",
    terms: Optional[str] = None,
    high_intent: Optional[str] = None,
    exclude: Optional[str] = None,
    min_rating: Optional[float] = None,
    min_reviews: Optional[int] = None,
    overwrite: bool = False,
    execute: bool = False,
    vertical_dir: Path = VERTICAL_PROFILES_DIR,
) -> CommandResult:
    validate_slug(slug)
    term_list = split_terms(terms)
    if not term_list:
        raise CommandError(
            "--terms is required and must be non-empty (maps to search_terms). "
            "Example: --terms \"pool builder; inground pool contractor\""
        )
    profile = build_vertical_profile(
        slug,
        display=display,
        airtable=airtable,
        terms=term_list,
        high_intent=split_terms(high_intent),
        exclude=split_terms(exclude),
        min_rating=min_rating,
        min_reviews=min_reviews,
    )
    path = vertical_dir / f"{slug}.json"
    payload = json.dumps(profile, ensure_ascii=False, indent=2)

    if not execute:
        return CommandResult(
            _truncate(
                f"🧩 preview /create-vertical {slug} "
                f"(reply with --execute to write {path}):\n{payload}"
            )
        )
    if path.exists() and not overwrite:
        raise CommandError(
            f"{path} already exists. Pass --overwrite to replace it."
        )
    vertical_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    return CommandResult(
        f"🧩 {path} written · {len(term_list)} search terms. "
        f"Try /run {slug} <market> medium."
    )


def cmd_create_market(
    slug: str,
    *,
    display: str = "",
    state: str = "",
    small: Optional[str] = None,
    medium: Optional[str] = None,
    full: Optional[str] = None,
    overwrite: bool = False,
    execute: bool = False,
    market_dir: Path = MARKET_PROFILES_DIR,
) -> CommandResult:
    validate_slug(slug)
    small_list = split_terms(small)
    medium_list = split_terms(medium)
    full_list = split_terms(full)
    if not (small_list or medium_list or full_list):
        raise CommandError(
            "At least one geo list is required (maps to geos). "
            "Provide --small and/or --medium (and optionally --full)."
        )
    profile = build_market_profile(
        slug,
        display=display,
        state=state,
        small=small_list,
        medium=medium_list,
        full=full_list,
    )
    path = market_dir / f"{slug}.json"
    payload = json.dumps(profile, ensure_ascii=False, indent=2)
    full_note = "" if full_list else " (full ⇒ medium)"

    if not execute:
        return CommandResult(
            _truncate(
                f"🗺 preview /create-market {slug} "
                f"(reply with --execute to write {path}):\n{payload}"
            )
        )
    if path.exists() and not overwrite:
        raise CommandError(
            f"{path} already exists. Pass --overwrite to replace it."
        )
    market_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    geos = profile["geos"]
    return CommandResult(
        f"🗺 {path} written · small {len(small_list)} / medium {len(medium_list)} "
        f"/ full {len(geos)} geos{full_note}."
    )


def cmd_help() -> CommandResult:
    msg = (
        "Lead-machine commands:\n"
        "• /run <vertical> <market> <size>  — search + cost preview (no LLM spend). "
        "e.g. /run epoxy cleveland medium\n"
        "• /confirm <batch>  — pay to score + build packets + sync sendable to Airtable. "
        "e.g. /confirm epoxy_cleveland_medium_001\n"
        "• /status <batch>  — show pipeline stages + packet tiers. "
        "e.g. /status epoxy_cleveland_medium_001\n"
        "• /create-vertical <slug> --terms \"a; b\"  — add a niche profile. "
        "e.g. /create-vertical pool_builders --terms \"pool builder; pool contractor\"\n"
        "• /create-market <slug> --small \"…\" --medium \"…\"  — add a metro profile. "
        "e.g. /create-market pittsburgh --state PA --small \"Pittsburgh PA\" --medium \"Pittsburgh PA; Mt Lebanon PA\""
    )
    return CommandResult(_truncate(msg))


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes_commands",
        description="Deterministic command layer for the lead pipeline (Hermes-safe).",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_run = sub.add_parser("run", help="search + cost preview (no LLM spend)")
    p_run.add_argument("vertical")
    p_run.add_argument("market")
    p_run.add_argument("size")
    p_run.add_argument("--max-llm-usd", type=float, default=DEFAULT_MAX_LLM_USD)
    p_run.add_argument("--execute", action="store_true")

    p_conf = sub.add_parser("confirm", help="pay to score + packets + live Airtable")
    p_conf.add_argument("batch")
    p_conf.add_argument("--max-llm-usd", type=float, default=DEFAULT_MAX_LLM_USD)
    p_conf.add_argument("--execute", action="store_true")

    p_stat = sub.add_parser("status", help="show pipeline stages + packet tiers")
    p_stat.add_argument("batch")

    p_cv = sub.add_parser("create-vertical", help="write a vertical profile JSON")
    p_cv.add_argument("slug")
    p_cv.add_argument("--display", default="")
    p_cv.add_argument("--airtable", default="")
    p_cv.add_argument("--terms", default="")
    p_cv.add_argument("--high-intent", dest="high_intent", default="")
    p_cv.add_argument("--exclude", default="")
    p_cv.add_argument("--min-rating", dest="min_rating", type=float, default=None)
    p_cv.add_argument("--min-reviews", dest="min_reviews", type=int, default=None)
    p_cv.add_argument("--overwrite", action="store_true")
    p_cv.add_argument("--execute", action="store_true")

    p_cm = sub.add_parser("create-market", help="write a market profile JSON")
    p_cm.add_argument("slug")
    p_cm.add_argument("--display", default="")
    p_cm.add_argument("--state", default="")
    p_cm.add_argument("--small", default="")
    p_cm.add_argument("--medium", default="")
    p_cm.add_argument("--full", default="")
    p_cm.add_argument("--overwrite", action="store_true")
    p_cm.add_argument("--execute", action="store_true")

    sub.add_parser("help", help="list commands")
    return parser


def dispatch(args: argparse.Namespace) -> CommandResult:
    if args.verb == "run":
        return cmd_run(
            args.vertical, args.market, args.size,
            cap=args.max_llm_usd, execute=args.execute,
        )
    if args.verb == "confirm":
        return cmd_confirm(
            args.batch, cap=args.max_llm_usd, execute=args.execute
        )
    if args.verb == "status":
        return cmd_status(args.batch)
    if args.verb == "create-vertical":
        return cmd_create_vertical(
            args.slug, display=args.display, airtable=args.airtable,
            terms=args.terms, high_intent=args.high_intent, exclude=args.exclude,
            min_rating=args.min_rating, min_reviews=args.min_reviews,
            overwrite=args.overwrite, execute=args.execute,
        )
    if args.verb == "create-market":
        return cmd_create_market(
            args.slug, display=args.display, state=args.state,
            small=args.small, medium=args.medium, full=args.full,
            overwrite=args.overwrite, execute=args.execute,
        )
    if args.verb == "help":
        return cmd_help()
    raise CommandError(f"Unknown verb '{args.verb}'.")


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Tolerate a leading slash on the verb (Telegram users type '/run').
    if raw and raw[0].startswith("/"):
        raw[0] = raw[0][1:]
    parser = build_parser()
    args = parser.parse_args(raw)
    try:
        result = dispatch(args)
    except CommandError as exc:
        print(f"[REFUSE] {exc}", flush=True)
        return 2
    print(result.message, flush=True)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
