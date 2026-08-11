#!/usr/bin/env python3
"""Freeze what the commercial tribunal *would* have said about every scored lead.

Why this exists. The tribunal was demoted to a ranking on branch
`strip/commercial-tribunal`, taking the sendable corpus from 87 to 432 distinct
leads. Whether it was protecting anything real is unanswerable from the base
alone: `evals/accept_rate.py` counts a logged dial as an acceptance, and once
the leads are dialled the counterfactual is gone. The cheap way to keep the
question open is to record the old verdict per lead *now*, while the pre-strip
code is still one `git worktree` away, and compare approval rates afterward.

This deliberately does not reimplement the old gate. It checks out the
pre-strip commit into a temporary worktree and calls the real
`lead_machine.verdict.evaluate_verdict` from that tree, so the label cannot
drift from what actually shipped. Running it in-process is impossible — both
trees export the same `lead_machine` package name — so the old pass runs as a
subprocess.

Output is `data/tribunal_labels.json`, keyed by Place Key, committed and then
left alone. Regenerating it after the corpus is re-scored would silently change
the labels the experiment is being read against; if that is ever needed, write
a new dated file rather than overwriting this one.

    .venv/bin/python tools/freeze_tribunal_labels.py [--pre-strip-ref REF]
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The branch point of `strip/commercial-tribunal`: the last commit at which
# `sendability` still ran the nine judgement gates and the tribunal.
DEFAULT_PRE_STRIP_REF = "1e46d02"
OUTPUT = REPO / "data" / "tribunal_labels.json"

_OLD_PASS = '''
import csv, json, sys
from pathlib import Path
sys.path.insert(0, {tree!r})
limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(limit); break
    except OverflowError:
        limit //= 2
import lead_machine.sendability as s
assert hasattr(s, "COMMERCIAL_FIT_BLOCKER"), "this tree has no tribunal; wrong ref"
from lead_machine.verdict import evaluate_verdict
out = {{}}
for path in sorted(Path({output_dir!r}).glob("scored_*.csv")):
    with path.open(newline="", encoding="utf-8") as h:
        for i, row in enumerate(csv.DictReader(h)):
            out[path.name + "::" + str(i)] = evaluate_verdict(row).verdict
print(json.dumps(out))
'''


def _widen_csv_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 2


def old_verdicts(ref: str) -> dict[str, str]:
    """Every scored row's pre-strip verdict, keyed `<file>::<row index>`."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "pre-strip"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(tree), ref],
            cwd=REPO, check=True, capture_output=True, text=True,
        )
        try:
            script = Path(tmp) / "old_pass.py"
            script.write_text(
                _OLD_PASS.format(tree=str(tree), output_dir=str(REPO / "output")),
                encoding="utf-8",
            )
            done = subprocess.run(
                [str(REPO / ".venv/bin/python"), str(script)],
                capture_output=True, text=True, check=True,
            )
            return json.loads(done.stdout)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=REPO, check=False, capture_output=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre-strip-ref", default=DEFAULT_PRE_STRIP_REF)
    parser.add_argument("--out", default=str(OUTPUT))
    args = parser.parse_args()

    _widen_csv_limit()
    print(f"[1/2] recomputing pre-strip verdicts at {args.pre_strip_ref} ...")
    old = old_verdicts(args.pre_strip_ref)
    print(f"      {len(old)} row verdicts")

    sys.path.insert(0, str(REPO))
    from lead_machine.verdict import evaluate_verdict  # noqa: E402
    from lead_machine.dial_rank import dial_rank  # noqa: E402

    print("[2/2] joining against the current verdict ...")
    leads: dict[str, dict] = {}
    for path in sorted((REPO / "output").glob("scored_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for i, row in enumerate(csv.DictReader(handle)):
                key = (row.get("Place Key") or "").strip()
                if not key:
                    continue
                cand = {
                    "tribunal": old.get(f"{path.name}::{i}", "unknown"),
                    "current": evaluate_verdict(row).verdict,
                    "rank": dial_rank(row),
                    "business_name": (row.get("Business Name") or "").strip(),
                    "vertical": (row.get("Vertical") or "").strip(),
                }
                # One lead appears in several files once it has been rescored.
                # Keep the row the pipeline would actually act on: a send beats
                # a non-send, then the higher rank.
                cur = leads.get(key)
                if cur is None or (cand["current"] == "send", cand["rank"]) > (
                    cur["current"] == "send", cur["rank"]
                ):
                    leads[key] = cand

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "pre_strip_ref": args.pre_strip_ref,
                "distinct_leads": len(leads),
                "labels": leads,
            },
            indent=1,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    sendable = {k: v for k, v in leads.items() if v["current"] == "send"}
    tally = Counter(v["tribunal"] for v in sendable.values())
    print(f"\ndistinct leads: {len(leads)}")
    print(f"currently sendable: {len(sendable)}")
    print(f"  tribunal would have passed: {tally.get('send', 0)}")
    print(f"  tribunal would have held:   {len(sendable) - tally.get('send', 0)}")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
