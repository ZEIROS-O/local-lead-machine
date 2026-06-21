---
name: build-asset-bundle
description: >-
  Generate the deliver-first asset bundle for an A-grade local-lead-machine lead —
  a branded mockup + a customer-POV walkthrough video script + a scored audit —
  grounded in a real, named site leak and checked by an independent verifier. Use
  this whenever the user wants to build a mockup, make the asset/video for a lead,
  create the "what's broken and what it costs you" audit, or prep outreach assets
  for a prospect. This is Loop B, the moat. DRAFT-ONLY: it produces assets for
  human review and never sends anything.
---

# Build an asset bundle (Loop B — the moat)

The personalized asset is the un-fakeable mechanism that survives a Stage-5
market: a real rebuild of *their* site beats any clever opener. The leverage is
automating the **asset**, while the **message** and the **close** stay human. So
this skill builds the artifacts and stops.

## Status / prerequisites — read this first
- **Phase B; not built, and gated by 🚦 Gate 0.** Do **not** wire this into
  automated sending before a first close proves the offer — automating an
  unvalidated motion scales rejection (see `docs/outreach-loop-strategy.md`).
- Until then this is a **draft-only accelerator** for hand-built bundles: it drafts
  the assets, a human reviews, a human sends. Build it on the autonomy ladder —
  draft-only first, earn more autonomy on a demonstrated accept-rate.

## Inputs
- An **A-grade, sendable** lead and its packet (from `make_packets.py`): the
  opener, `evidence_refs`, `best_angle`, `safe_outreach_claims`, the named site
  leak, and the screenshots in `screenshots/`. If the lead isn't A/sendable, stop
  — assets are for qualified leads only.

## Procedure
1. **Anchor on one real leak.** Pull the specific, verifiable on-site problem from
   the packet evidence (broken form, no mobile, dead CTA, premium-perception gap,
   …). Everything in the bundle must trace to it. If you can't name a real leak,
   stop — there's no honest asset to build.
2. **Mockup** — a branded homepage rebuild that fixes that leak, using their real
   business name/services. Output a hostable artifact (the `Mockup URL` column in
   the packet is where the link goes).
3. **Customer-POV video script** — a 15–40s walkthrough: their *live* site failing
   at the leak, then the fix. Script + shot list (screen-record of the real site).
   Link goes in the packet `Video URL` column.
4. **Scored audit** — a short "what's broken and what it costs you" report tied to
   the same leak, in their language, no fabricated metrics.

## Verifier (non-negotiable — maker ≠ checker)
Before any asset is marked review-ready, an **independent, read-only** check (a
cheap model is fine) must confirm: the asset names a *real, verifiable* leak that
is actually present on the live site, and the opener references it. Same
evidence-not-claim discipline as `verify-batch` / `sendability.py`. Reject and
regenerate any asset that asserts a problem not on the site, or any claim of
results/money — those inversions ("seller didn't understand my business" / a
detectable lie) are fatal in a small repeated-game market.

## Output & stop
Write the bundle to a per-lead review folder, mark it **draft / needs human
approval**, and stop. Pages, sends, and the close are human acts under Loop C and
Gate 0 — this skill does not perform them.
