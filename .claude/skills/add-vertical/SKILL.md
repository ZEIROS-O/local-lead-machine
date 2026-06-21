---
name: add-vertical
description: >-
  Add a new sales vertical/niche to local-lead-machine as DATA (a VerticalProfile
  + search configs), not scattered code edits. Use this whenever the user wants to
  target a new type of business — "add epoxy flooring", "let's go after window
  tinting / pressure washing / mobile mechanics", "set up a new vertical", or to
  migrate the live vertical. Trigger even if they only describe a new kind of
  business to prospect without saying the word "vertical". Adding a vertical
  should never require touching icp/scoring logic by hand — this skill keeps it a
  data change and runs the regression tests.
---

# Add a vertical (vertical-as-data)

The pipeline's per-vertical knowledge (target terms, service terms, high-ticket
terms, deny/low-ticket terms, franchise denylist) is meant to live in **one**
place — `lead_machine/verticals.py` (`VerticalProfile` / `VERTICAL_PROFILES`) —
so a new niche is a data addition, not surgery across `icp.py`, `icp_lanes.py`,
`enrich_leads.py`, and `score_leads.py`. This skill is that procedure.

## Status / prerequisites
- **Depends on `lead_machine/verticals.py`** (Phase A G2). If it doesn't exist
  yet, the migration that creates it IS G2 — do that first (see
  `docs/phase-a-backlog.md` §G2), then use this skill for the *next* vertical
  (e.g. the epoxy fast-follow). If it exists, proceed.

## Procedure
1. **Pin the economics.** Confirm the niche is high-ticket / demand-constrained
   enough to fit the ICP (it rejects ~90% of TAM on purpose). Identify the
   *high-ticket* sub-segment to target and the low-ticket variants to deny
   (e.g. detailing → target ceramic/PPF/correction, deny express/tunnel washes).
2. **Author the profile** in `lead_machine/verticals.py`: add a `VerticalProfile`
   with `batch_terms`, `service_terms`, `high_ticket_terms`, `deny_terms`,
   `low_ticket_terms`, `franchise_denylist`, `exclude_terms`, `geo_terms`,
   `family`. Reuse the existing profiles as the template; keep terms lowercased.
3. **Generate search configs** in `search_configs/` named
   `<vertical>_<metro>_<size>.json`: high-ticket queries, `boost_terms` mirroring
   the high-ticket terms, `exclude_terms` stripping the low-ticket variants,
   `min_rating: 4.3`, `min_reviews: 5`. (Use the `add-metro` skill to add more
   metros once the first config is proven.)
4. **Run the regression gate:**
   `python -m unittest tests.test_icp_lanes tests.test_lead_machine_contracts`.
   These assert the *existing* verticals' behavior — they must stay green, proving
   the new profile didn't perturb the others. Resolution must be keyed off each
   record's `vertical`/`source_query`; never introduce a global "active vertical".
5. **Spot-check a real search** (small/test config) and confirm it returns the
   intended high-ticket businesses, not the denied low-ticket ones.

## Why this shape
Centralizing terms is what makes the engine reusable across niches (and is the
reason this repo can become a general operator engine, not a one-niche tool).
Scattering terms back into the consumers is the regression that makes the next
vertical a rewrite — don't.
