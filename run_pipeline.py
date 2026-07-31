#!/usr/bin/env python3
"""
One-command local lead pipeline.

Runs:
1. places_search.py
2. enrich_leads.py
3. score_leads.py
4. make_packets.py

Optional, opt-in signal stages run before scoring:
- collect_external_signals.py merges already-collected normalized observations.
- collect_review_signals.py attaches DataForSEO newest-review evidence across
  two runs — merge before scoring, submit after it.

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
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


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


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def yes(value: Any) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def count_priority_ab(rows: list[dict[str, str]]) -> int:
    return sum(1 for row in rows if row.get("Lead Priority") in {"A", "B"})


def count_sendable(rows: list[dict[str, str]]) -> int:
    return sum(1 for row in rows if yes(row.get("Sendable")))


def count_airtable_rows(rows: list[dict[str, str]]) -> int:
    return len(rows)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def cap_value(caps: dict[str, Any], key: str) -> Any:
    value = caps.get(key)
    return 0 if value in ("", None) else value


def build_summary(
    *,
    batch: str,
    mode: str,
    raw: Path,
    enriched: Path,
    scored: Path,
    cards: Path,
    packets: Path,
    queue_dir: Path,
    scores_dir: Path,
    llm_usage_path: Path,
    summary_path: Path,
    airtable_status: str,
    waiting_for_manual_scores: bool,
    review_receipt: Path,
    review_signals: Path,
    review_collection_summary: Path,
    review_merge_summary: Path,
    review_signals_status: str,
    external_signal_paths: list[Path],
) -> dict[str, Any]:
    scored_rows = read_csv_rows(scored)
    packet_index = packets / "packet_index.csv"
    llm_usage = read_json(llm_usage_path)
    caps = llm_usage.get("caps") if isinstance(llm_usage.get("caps"), dict) else {}
    receipt = read_json(review_receipt)
    collection = read_json(review_collection_summary)
    review_merge = read_json(review_merge_summary)
    receipt_tasks = receipt.get("tasks") if isinstance(receipt.get("tasks"), list) else []
    submitted_count = int(receipt.get("submitted_count") or len(receipt_tasks))
    estimated_cost = float(receipt.get("estimated_usd") or 0.0)
    collection_cost = float(collection.get("cost_usd") or 0.0)
    submission_cost = float(receipt.get("cost_usd") or 0.0)
    external_cost = 0.0
    for signal_path in external_signal_paths:
        signal = read_json(signal_path)
        observation = signal.get("observation") if isinstance(signal.get("observation"), dict) else {}
        external_cost += float(observation.get("cost_usd") or 0.0)

    return {
        "batch": batch,
        "mode": mode,
        "waiting_for_manual_scores": waiting_for_manual_scores,
        "counts": {
            "leads_in": count_lines(raw),
            "enriched": count_jsonl(enriched),
            "scored": 0 if waiting_for_manual_scores else len(scored_rows),
            "priority_ab": 0 if waiting_for_manual_scores else count_priority_ab(scored_rows),
            "sendable": 0 if waiting_for_manual_scores else count_sendable(scored_rows),
            "packets": 0 if waiting_for_manual_scores else count_lines(packet_index),
            "airtable_rows": 0 if airtable_status == "skipped" or waiting_for_manual_scores else count_airtable_rows(scored_rows),
        },
        "llm": {
            "calls": {
                "used": int(llm_usage.get("calls") or 0),
                "cap": cap_value(caps, "MAX_LLM_CALLS_PER_RUN"),
            },
            "tokens": {
                "used": int(llm_usage.get("total_tokens") or 0),
                "input": int(llm_usage.get("input_tokens") or 0),
                "output": int(llm_usage.get("output_tokens") or 0),
                "cap": cap_value(caps, "MAX_LLM_TOKENS_PER_RUN"),
            },
            "usd": {
                "used": float(llm_usage.get("estimated_usd") or 0.0),
                "cap": cap_value(caps, "MAX_LLM_USD_PER_RUN"),
            },
            "stopped_by_cap": bool(llm_usage.get("stopped_by_cap")),
            "stop_reason": str(llm_usage.get("stop_reason") or ""),
            "usage_path": str(llm_usage_path) if llm_usage_path.exists() else "",
        },
        "airtable": {
            "status": airtable_status,
            "scope": "all_scored_rows",
        },
        "review_signals": {
            "status": review_signals_status,
            "receipt": str(review_receipt),
            "signals": str(review_signals),
            "collection_summary": str(review_collection_summary),
            "merge_summary": str(review_merge_summary),
            "submitted": submitted_count,
            "complete": int(collection.get("complete") or 0),
            "pending": int(collection.get("pending") or 0),
            "attached": int(review_merge.get("attached") or 0),
            "estimated_usd": round(estimated_cost, 6),
            # Submit and collect report the same DataForSEO tasks at different
            # points, so take the greater known total instead of double-counting.
            "cost_usd": max(submission_cost, collection_cost),
            "submission_cost_usd": submission_cost,
            "collection_cost_usd": collection_cost,
        },
        "external_signals": {
            "status": "merged" if external_signal_paths else "skipped",
            "merged": len(external_signal_paths),
            "cost_usd": round(external_cost, 6),
            "artifacts": [str(path) for path in external_signal_paths],
        },
        "artifacts": {
            "raw": str(raw),
            "enriched": str(enriched),
            "scored": str(scored),
            "cards": str(cards),
            "packets": str(packets),
            "packet_index": str(packet_index),
            "llm_usage": str(llm_usage_path),
            "summary": str(summary_path),
            "llm_queue": str(queue_dir),
            "scores_dir": str(scores_dir),
        },
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_summary(summary: dict[str, Any]) -> None:
    counts = summary["counts"]
    llm = summary["llm"]
    artifacts = summary["artifacts"]

    print(f"Raw leads:       {counts['leads_in']}")
    print(f"Enriched leads:  {counts['enriched']}")
    if summary["waiting_for_manual_scores"]:
        print(f"Scored leads:    not updated (waiting for manual scores in {artifacts['scores_dir']})")
        print("A/B leads:       not updated")
        print("Sendable leads:  not updated")
        print("Packets:         not updated")
        print("Packet index:    not updated")
    else:
        print(f"Scored leads:    {counts['scored']}")
        print(f"A/B leads:       {counts['priority_ab']}")
        print(f"Sendable leads:  {counts['sendable']}")
        print(f"Packets:         {counts['packets']}")
        print(f"Packet index:    {artifacts['packet_index']}")
        print(f"Open packets:    cursor {artifacts['packets']}")
    print(f"Review signals:  {summary['review_signals']['status']}")
    print(f"External signals:{summary['external_signals']['status']}")
    print(f"Airtable rows:   {counts['airtable_rows']} ({summary['airtable']['status']})")
    print(f"LLM calls:       {llm['calls']['used']} / {llm['calls']['cap']}")
    print(f"LLM tokens:      {llm['tokens']['used']} / {llm['tokens']['cap']}")
    print(f"LLM USD:         {llm['usd']['used']:.6f} / {llm['usd']['cap']}")
    if llm["stopped_by_cap"]:
        print(f"Stopped by cap: {llm['stop_reason']}")
    print(f"Summary JSON:    {artifacts['summary']}")


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument("--batch", required=True, help="Batch name, e.g. hardscaping_round2")
    parser.add_argument("--vertical", required=True, help="Fallback vertical, e.g. hardscaping or roofing")
    parser.add_argument("--searches", default="", help="Search CSV path. Default: data/searches_<batch>.csv")

    parser.add_argument("--skip-search", action="store_true", help="Skip Google Places search")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip enrichment/screenshots")
    parser.add_argument("--skip-score", action="store_true", help="Skip scoring")
    parser.add_argument("--skip-packets", action="store_true", help="Skip packet generation")

    # Opt-in, not --skip-*, unlike every stage above. Those stages already run on
    # every batch, so their skip flag can only ever remove spend. Review collection
    # runs nowhere today and bills DataForSEO per task, so a default-on
    # --skip-review-signals would add that spend to every batch command already in
    # use without the command changing. Submit and merge are separate flags because
    # DataForSEO's review endpoint is asynchronous and because the two halves attach
    # at opposite ends of scoring.
    parser.add_argument(
        "--merge-review-signals",
        action="store_true",
        help="Before scoring, collect an earlier run's review tasks and merge them into the enriched JSONL",
    )
    parser.add_argument(
        "--submit-review-signals",
        action="store_true",
        help="After scoring, submit bounded DataForSEO review tasks (paid) for the deep-enrichment selection",
    )
    parser.add_argument(
        "--external-signal",
        action="append",
        default=[],
        help=(
            "Merge one normalized collect_external_signals.py artifact before "
            "scoring; repeat for multiple observations"
        ),
    )

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
    parser.add_argument(
        "--mode",
        choices=["api-llm", "manual-llm", "deterministic"],
        default="api-llm",
        help="Scoring mode. Default: api-llm",
    )
    parser.add_argument(
        "--manual-llm",
        action="store_true",
        help="Deprecated alias for --mode manual-llm",
    )
    parser.add_argument("--llm-queue-dir", default="", help="Manual LLM queue directory. Default: review_queue_<batch>")
    parser.add_argument("--scores-dir", default="", help="Manual LLM scores directory. Default: <queue>/scores")
    parser.add_argument("--cards-output", default="", help="Lead cards JSONL. Default: output/lead_cards_<batch>.jsonl")
    parser.add_argument(
        "--skip-airtable",
        "--no-airtable",
        action="store_true",
        help="Skip Airtable upload/dry-run tail",
    )
    parser.add_argument("--airtable-live", action="store_true", help="Run upload_airtable.py --live instead of dry-run")

    args = parser.parse_args()

    root = Path.cwd()
    batch = args.batch.strip()
    mode = "manual-llm" if args.manual_llm else args.mode

    searches = Path(args.searches) if args.searches else Path(f"data/searches_{batch}.csv")

    raw = Path(f"output/raw_places_{batch}.csv")
    enriched = Path(f"output/enriched_{batch}.jsonl")
    scored = Path(f"output/scored_{batch}.csv")
    cards = Path(args.cards_output) if args.cards_output else Path(f"output/lead_cards_{batch}.jsonl")
    packets = Path(f"packets_{batch}")
    queue_dir = Path(args.llm_queue_dir) if args.llm_queue_dir else Path(f"review_queue_{batch}")
    scores_dir = Path(args.scores_dir) if args.scores_dir else queue_dir / "scores"
    llm_usage = Path(f"output/llm_usage_{batch}.json")
    summary_path = Path(f"output/run_summary_{batch}.json")
    review_receipt = Path(f"output/review_tasks_{batch}.json")
    review_signals = Path(f"output/review_signals_{batch}.jsonl")
    review_collection_summary = Path(f"output/review_collection_{batch}.json")
    review_merge_summary = Path(f"output/review_merge_{batch}.json")
    external_signal_paths = [Path(value) for value in args.external_signal]

    def persist_summary(
        *,
        airtable_status: str,
        waiting_for_manual_scores: bool,
        review_signals_status: str,
    ) -> dict[str, Any]:
        summary = build_summary(
            batch=batch,
            mode=mode,
            raw=raw,
            enriched=enriched,
            scored=scored,
            cards=cards,
            packets=packets,
            queue_dir=queue_dir,
            scores_dir=scores_dir,
            llm_usage_path=llm_usage,
            summary_path=summary_path,
            airtable_status=airtable_status,
            waiting_for_manual_scores=waiting_for_manual_scores,
            review_receipt=review_receipt,
            review_signals=review_signals,
            review_collection_summary=review_collection_summary,
            review_merge_summary=review_merge_summary,
            review_signals_status=review_signals_status,
            external_signal_paths=external_signal_paths,
        )
        write_summary(summary_path, summary)
        return summary

    print("\nLead pipeline")
    print("-------------")
    print(f"Batch:      {batch}")
    print(f"Vertical:   {args.vertical}")
    print(f"Mode:       {mode}")
    print(f"Searches:   {searches}")
    print(f"Raw:        {raw}")
    print(f"Enriched:   {enriched}")
    print(f"Scored:     {scored}")
    print(f"Cards:      {cards}")
    print(f"Packets:    {packets}")
    if mode in {"manual-llm", "api-llm"}:
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

    review_signals_status = "skipped"

    for signal_path in external_signal_paths:
        if not enriched.exists():
            raise SystemExit(f"\n[ERROR] Enriched file not found: {enriched}")
        if not signal_path.exists():
            raise SystemExit(f"\n[ERROR] External signal artifact not found: {signal_path}")
        run([
            sys.executable,
            "collect_external_signals.py",
            "merge",
            "--input",
            str(enriched),
            "--signal",
            str(signal_path),
            "--output",
            str(enriched),
        ])
    if not external_signal_paths:
        print("\n[SKIP] collect_external_signals.py merge")

    # Ahead of scoring on purpose. scoring_v3 reads review_items/review_collection
    # off the enriched record, so evidence merged after score_leads has run is paid
    # for and then read by nothing until some later run happens to re-score.
    if args.merge_review_signals:
        if not enriched.exists():
            raise SystemExit(f"\n[ERROR] Enriched file not found: {enriched}")
        if not review_receipt.exists():
            raise SystemExit(f"\n[ERROR] Review task receipt not found: {review_receipt}")

        run([
            sys.executable,
            "collect_review_signals.py",
            "collect",
            "--receipt",
            str(review_receipt),
            "--output",
            str(review_signals),
            "--summary-output",
            str(review_collection_summary),
        ])

        if not review_collection_summary.exists():
            raise SystemExit(
                "\n[ERROR] Review collection did not write its required summary: "
                f"{review_collection_summary}"
            )
        collection = read_json(review_collection_summary)
        if collection.get("status") not in {"complete", "empty", "partial", "pending"}:
            raise SystemExit(
                "\n[ERROR] Review collection summary is malformed: "
                f"{review_collection_summary}"
            )
        pending = int(collection.get("pending") or 0)
        if pending:
            review_signals_status = str(collection.get("status") or "pending")
            persist_summary(
                airtable_status="skipped",
                waiting_for_manual_scores=False,
                review_signals_status=review_signals_status,
            )
            raise SystemExit(
                f"\n[WAITING] {pending} review tasks are still pending; "
                "scoring was not run against partial review evidence."
            )

        # Merged back over the same enriched file so scoring needs no new input
        # path. Keying is by place_id, so re-running a merge is idempotent.
        run([
            sys.executable,
            "collect_review_signals.py",
            "merge",
            "--input",
            str(enriched),
            "--signals",
            str(review_signals),
            "--output",
            str(enriched),
            "--summary-output",
            str(review_merge_summary),
        ])
        review_signals_status = "merged"
    else:
        print("\n[SKIP] collect_review_signals.py merge")

    score_was_run = False
    waiting_for_manual_scores = False

    if mode == "manual-llm" and not args.skip_score:
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

    manual_scores_ready = mode == "manual-llm" and scores_dir.exists() and any(scores_dir.glob("*.json"))

    if not args.skip_score:
        if not enriched.exists():
            raise SystemExit(f"\n[ERROR] Enriched file not found: {enriched}")

        if mode == "manual-llm" and not manual_scores_ready:
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
            "--mode",
            mode,
            ]

            if mode in {"manual-llm", "api-llm"}:
                cmd.extend([
                    "--scores-dir",
                    str(scores_dir),
                    "--cards-output",
                    str(cards),
                ])
            if mode == "api-llm":
                cmd.extend(["--llm-usage-output", str(llm_usage)])

            if args.limit > 0:
                cmd.extend(["--limit", str(args.limit)])

            run(cmd)
            score_was_run = True
    else:
        print("\n[SKIP] score_leads.py")

    # Behind scoring on purpose. select_deep_enrichment ranks on Scoring V3
    # Disposition/Rank, which exist only once score_leads has written the CSV; run
    # earlier it would bill 25 DataForSEO tasks against an unranked selection. The
    # tasks opened here are picked up by a later run with --merge-review-signals.
    if args.submit_review_signals:
        if waiting_for_manual_scores or not scored.exists():
            print("\n[SKIP] collect_review_signals.py submit")
            print("[WAITING] Scored CSV not created yet.")
        else:
            run([
                sys.executable,
                "collect_review_signals.py",
                "submit",
                "--input",
                str(scored),
                "--output",
                str(review_receipt),
            ])
            review_signals_status = "merged+submitted" if review_signals_status == "merged" else "submitted"
    else:
        print("\n[SKIP] collect_review_signals.py submit")

    packets_allowed = score_was_run or (args.skip_score and scored.exists()) or (mode != "manual-llm" and scored.exists())

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

    airtable_status = "skipped"
    if args.skip_airtable:
        print("\n[SKIP] upload_airtable.py")
    elif waiting_for_manual_scores:
        print("\n[SKIP] upload_airtable.py")
        print("[WAITING] Scored CSV not created yet.")
    elif not scored.exists():
        print("\n[SKIP] upload_airtable.py")
        print("[WAITING] Scored CSV not created yet.")
    else:
        cmd = [
            sys.executable,
            "upload_airtable.py",
            "--input",
            str(scored),
            "--packet-dir",
            str(packets),
        ]
        if args.airtable_live:
            cmd.append("--live")
            airtable_status = "live"
        else:
            cmd.append("--dry-run")
            airtable_status = "dry-run"
        run(cmd)

    print("\n" + "=" * 100)
    print("PIPELINE COMPLETE")
    print("=" * 100)

    summary = persist_summary(
        airtable_status=airtable_status,
        waiting_for_manual_scores=waiting_for_manual_scores,
        review_signals_status=review_signals_status,
    )
    print_summary(summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
