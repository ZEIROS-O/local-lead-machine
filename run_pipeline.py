#!/usr/bin/env python3
"""
One-command local lead pipeline.

Runs:
1. places_search.py
2. enrich_leads.py
3. score_leads.py
4. make_packets.py

Example:
    python run_pipeline.py --batch hardscaping_round2 --vertical hardscaping

With explicit search file:
    python run_pipeline.py --batch akron_masonry --vertical hardscaping --searches data/searches_akron_masonry.csv

Skip search and reuse raw file:
    python run_pipeline.py --batch hardscaping_round2 --vertical hardscaping --skip-search

Skip enrichment too:
    python run_pipeline.py --batch hardscaping_round2 --vertical hardscaping --skip-search --skip-enrich
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> None:
    print("\n" + "=" * 100, flush=True)
    print("RUN:", " ".join(cmd), flush=True)
    print("=" * 100, flush=True)

    result = subprocess.run(cmd)

    if result.returncode != 0:
        raise SystemExit(f"\n[FAILED] Command exited with code {result.returncode}: {' '.join(cmd)}")


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return max(0, len(path.read_text(encoding="utf-8", errors="ignore").splitlines()) - 1)


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()])


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument("--batch", required=True, help="Batch name, e.g. hardscaping_round2")
    parser.add_argument("--vertical", required=True, help="Fallback vertical, e.g. hardscaping or roofing")
    parser.add_argument("--searches", default="", help="Search CSV path. Default: data/searches_<batch>.csv")

    parser.add_argument("--skip-search", action="store_true", help="Skip Google Places search")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip enrichment/screenshots")
    parser.add_argument("--skip-score", action="store_true", help="Skip scoring")
    parser.add_argument("--skip-packets", action="store_true", help="Skip packet generation")

    parser.add_argument("--include-all-packets", action="store_true", help="Make packets for all scored rows")
    parser.add_argument("--priority", default="", help="Optional packet priority filter: A, B, C, Skip")
    parser.add_argument("--max-packets", type=int, default=0, help="Optional max packet count")
    parser.add_argument("--no-screenshots", action="store_true", help="Skip screenshots during enrichment")
    parser.add_argument(
        "--rendered-form-check",
        action="store_true",
        help="Use Playwright DOM rendering for uncertain form detection during enrichment",
    )
    parser.add_argument(
        "--rendered-site-check",
        action="store_true",
        help="Use Playwright browser rendering to classify loaded/broken/timeout site status during enrichment",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max leads to enrich")
    parser.add_argument("--manual-llm", action="store_true", help="Generate/merge manual Claude or ChatGPT JSON scores")
    parser.add_argument("--llm-queue-dir", default="", help="Manual LLM queue directory. Default: review_queue_<batch>")
    parser.add_argument("--scores-dir", default="", help="Manual LLM scores directory. Default: <queue>/scores")
    parser.add_argument("--cards-output", default="", help="Lead cards JSONL. Default: output/lead_cards_<batch>.jsonl")

    args = parser.parse_args()

    root = Path.cwd()
    batch = args.batch.strip()

    searches = Path(args.searches) if args.searches else Path(f"data/searches_{batch}.csv")

    raw = Path(f"output/raw_places_{batch}.csv")
    enriched = Path(f"output/enriched_{batch}.jsonl")
    scored = Path(f"output/scored_{batch}.csv")
    cards = Path(args.cards_output) if args.cards_output else Path(f"output/lead_cards_{batch}.jsonl")
    packets = Path(f"packets_{batch}")
    queue_dir = Path(args.llm_queue_dir) if args.llm_queue_dir else Path(f"review_queue_{batch}")
    scores_dir = Path(args.scores_dir) if args.scores_dir else queue_dir / "scores"

    print("\nLead pipeline")
    print("-------------")
    print(f"Batch:      {batch}")
    print(f"Vertical:   {args.vertical}")
    print(f"Searches:   {searches}")
    print(f"Raw:        {raw}")
    print(f"Enriched:   {enriched}")
    print(f"Scored:     {scored}")
    print(f"Cards:      {cards}")
    print(f"Packets:    {packets}")
    if args.manual_llm:
        print(f"LLM queue:  {queue_dir}")
        print(f"Scores dir: {scores_dir}")

    if not args.skip_search:
        if not searches.exists():
            raise SystemExit(f"\n[ERROR] Search CSV not found: {searches}")

        run([
            sys.executable,
            "places_search.py",
            "--searches",
            str(searches),
            "--output",
            str(raw),
        ])
    else:
        print("\n[SKIP] places_search.py")

    if not args.skip_enrich:
        if not raw.exists():
            raise SystemExit(f"\n[ERROR] Raw places file not found: {raw}")

        cmd = [
            sys.executable,
            "enrich_leads.py",
            "--input",
            str(raw),
            "--output",
            str(enriched),
        ]

        if args.no_screenshots:
            cmd.append("--no-screenshots")
        if args.rendered_form_check:
            cmd.append("--rendered-form-check")
        if args.rendered_site_check:
            cmd.append("--rendered-site-check")

        if args.limit > 0:
            cmd.extend(["--limit", str(args.limit)])

        run(cmd)
    else:
        print("\n[SKIP] enrich_leads.py")

    score_was_run = False
    waiting_for_manual_scores = False

    if args.manual_llm and not args.skip_score:
        if not enriched.exists():
            raise SystemExit(f"\n[ERROR] Enriched file not found: {enriched}")

        queue_cmd = [
            sys.executable,
            "make_llm_queue.py",
            "--input",
            str(enriched),
            "--output-dir",
            str(queue_dir),
            "--vertical",
            args.vertical,
        ]
        if args.limit > 0:
            queue_cmd.extend(["--limit", str(args.limit)])
        run(queue_cmd)

    manual_scores_ready = args.manual_llm and scores_dir.exists() and any(scores_dir.glob("*.json"))

    if not args.skip_score:
        if not enriched.exists():
            raise SystemExit(f"\n[ERROR] Enriched file not found: {enriched}")

        if args.manual_llm and not manual_scores_ready:
            print(f"\n[SKIP] score_leads.py")
            print(f"[WAITING] Paste manual LLM JSON scores into: {scores_dir}")
            print("[RESUME] Re-run this command with the same args after scores exist.")
            waiting_for_manual_scores = True
        else:
            cmd = [
            sys.executable,
            "score_leads.py",
            "--input",
            str(enriched),
            "--output",
            str(scored),
            "--vertical",
            args.vertical,
            ]

            if args.manual_llm:
                cmd.extend([
                    "--mode",
                    "manual-llm",
                    "--scores-dir",
                    str(scores_dir),
                    "--cards-output",
                    str(cards),
                ])

            if args.limit > 0:
                cmd.extend(["--limit", str(args.limit)])

            run(cmd)
            score_was_run = True
    else:
        print("\n[SKIP] score_leads.py")

    packets_allowed = score_was_run or (args.skip_score and scored.exists()) or (not args.manual_llm and scored.exists())

    if not args.skip_packets and packets_allowed:
        if not scored.exists():
            raise SystemExit(f"\n[ERROR] Scored file not found: {scored}")

        cmd = [
            sys.executable,
            "make_packets.py",
            "--inputs",
            str(scored),
            "--output-dir",
            str(packets),
        ]

        if args.include_all_packets:
            cmd.append("--include-all")

        if args.priority:
            cmd.extend(["--priority", args.priority])

        if args.max_packets > 0:
            cmd.extend(["--max", str(args.max_packets)])

        run(cmd)
    elif not args.skip_packets:
        print("\n[SKIP] make_packets.py")
        print("[WAITING] Scored CSV not created yet.")
    else:
        print("\n[SKIP] make_packets.py")

    print("\n" + "=" * 100)
    print("PIPELINE COMPLETE")
    print("=" * 100)

    print(f"Raw leads:       {count_lines(raw)}")
    print(f"Enriched leads:  {count_jsonl(enriched)}")
    if waiting_for_manual_scores:
        print(f"Scored leads:    not updated (waiting for manual scores in {scores_dir})")
    else:
        print(f"Scored leads:    {count_lines(scored)}")

    index = packets / "packet_index.csv"
    if waiting_for_manual_scores:
        print("Packets:         not updated")
        print("Packet index:    not updated")
    else:
        print(f"Packets:         {count_lines(index)}")
        print(f"Packet index:    {index if index.exists() else 'not created'}")
        print(f"Open packets:    cursor {packets}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
