---
name: verify-batch
description: >-
  Independently audit a scored lead batch against the sendability quality gate —
  the read-only maker≠checker verifier for local-lead-machine. Use this whenever
  the user wants to check/review a batch, asks "why was this lead skipped / marked
  sendable", wants to sanity-check scoring after a change, spot-check N leads, or
  confirm a batch is safe to act on. Always run this before trusting grades or
  acting on a batch — the scorer is the maker, this is the checker, and they must
  stay separate. Never edits scores; it only judges them on evidence.
---

# Verify a batch (the maker ≠ checker gate)

The scorer proposes; this skill disposes. It re-checks a scored batch against the
**objective** gate so quality is held by something other than the thing that
produced it. The unit of judgment is *evidence* (the row's blockers, leak, opener,
claims) — never the scorer's own "this is an A".

## Status / prerequisites
- **Active now.** Operates on a scored CSV / packet index + `lead_machine/sendability.py`
  blockers, all of which exist today.

## Procedure
1. **Load** the scored output (`output/scored_*.csv` or the packet index) and the
   sendability result per row. The blockers are defined in `lead_machine/sendability.py`
   (e.g. `SCORE_BLOCKER`, `CONVERSION_ISSUE_BLOCKER`, `OPENER_BLOCKER`,
   `NO_SAFE_CLAIM_BLOCKER`, and `INVALID_REF_BLOCKER`).
2. **For each lead marked sendable, confirm the non-negotiables hold:**
   - a real, *named, verifiable* leak/pain (not a generic "your site could be better");
   - an evidence-backed opener that cites that leak;
   - safe outreach claims with valid `evidence_refs` (no fabricated results/metrics);
   Flag any "sendable" lead that fails one of these — that's a verifier catch.
   A confirmed money signal relaxes the mature-band visible-pain threshold from
   40 to 35 and counts as demand evidence.
   Missing money signal is not a standalone blocker; the planned tightener did not ship.
3. **For a sample of skipped/low leads**, confirm the block reason is correct
   (catch false-skips — good leads wrongly gated).
4. **Report** the grade distribution (A/B/C/Skip), the sendable count, every
   flagged sendable-but-weak lead with its missing element, and any suspected
   false-skips. Recommend score/sendability tweaks; do **not** edit scores here.

## Why read-only
Self-grading structurally fails — a scorer prefers conclusions consistent with its
own reasoning. Keeping this checker read-only, on the objective gate, is what lets
the scorer be tuned aggressively: the bar is held outside it. The KPI is
accept-rate; if a human reviewer rejects more than ~half of what passes this gate,
the gate (or the scorer) needs work, not the reviewer.
