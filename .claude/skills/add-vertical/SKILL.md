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
- `lead_machine/verticals.py` exists and is live (Phase A G2 landed). Seven
  profiles are registered: hardscaping, detailing, water_restoration,
  garage_doors, plumbing, epoxy, roofing. Proceed straight to the procedure.

## Procedure
1. **Pin the economics.** Confirm the niche is high-ticket / demand-constrained
   enough to fit the ICP (it rejects ~90% of TAM on purpose). Identify the
   *high-ticket* sub-segment to target and the low-ticket variants to deny
   (e.g. detailing → target ceramic/PPF/correction, deny express/tunnel washes).
2. **Author the profile** in `lead_machine/verticals.py`: add a `VerticalProfile`
   with `key`, `label`, `family`, `batch_terms`, `service_terms`,
   `high_ticket_terms`, `deny_terms`, `low_ticket_terms`, `franchise_denylist`,
   `exclude_terms`, `aliases`, `min_reviews`, `adjacency_exclusions`. Reuse the
   existing profiles as the template; keep terms lowercased. There is no
   `geo_terms` field — geography is decided by the search configs' geo slices and
   assessed by `scoring_v3`, not by the ICP gate.
   - **`min_reviews` is 8** on every profile and should stay 8 unless you are
     deliberately changing the global floor. There is **no review ceiling**:
     `max_reviews` was removed as a concept (the one booked call to date was a
     222-review business). Maturity is banded by
     `buyer_signals.REVIEW_DEMAND_THRESHOLDS`, not gated.
   - **Terms match on a left word boundary, not bare substring.** A term that is
     the *tail* of a real word will not fire — `roofing` no longer swallows
     `waterproofing` — but a term still matches its own suffix growth (`gutter`
     matches `gutters`). Write terms singular.
3. **Author `vertical_profiles/<key>.json`** — the search-side half: query
   phrasing, display name, `airtable_vertical`, `min_rating` (4.3),
   `min_reviews`. **`min_reviews` here must equal the `VerticalProfile`'s**: this
   file sets the *search-time* floor and `verticals.py` sets the *score-time*
   floor, so a mismatch means leads get searched for and then skipped.
   `test_every_review_floor_matches_its_own_json_profile` (in
   `tests/test_geo_and_vertical_resolution.py`) fails if they disagree.
4. **Generate the search config** — do not hand-write it:
   `.venv/bin/python generate_search_config.py --vertical <key> --market <metro> --size medium`.
   It writes `search_configs/<vertical>_<metro>_<size>_001.json`, sourcing
   boost/exclude terms from `VERTICAL_PROFILES` (so there is one source of truth)
   and `min_rating`/`min_reviews` from the JSON profile, and refreshes
   `search_configs/geo_slices/<metro>.json`. No paid API calls. (Use the
   `add-metro` skill to add more metros once the first config is proven.)
5. **Register the vertical in Airtable before the first upload.** `Prospects.Vertical`
   is a single select and nothing sets `typecast` any more, so an unregistered
   slug 422s the upload. Check the live choice list in
   `docs/airtable_schema_required.md`. Registering a profile also *relabels*
   existing leads whose copy matches the new terms — that is intended, and pinned.
6. **Run the regression gate:**
   `.venv/bin/python -m unittest tests.test_icp_lanes tests.test_lead_machine_contracts tests.test_geo_and_vertical_resolution tests.test_generate_search_config`.
   The first two assert the *existing* verticals' behavior — they must stay green,
   proving the new profile didn't perturb the others. The third catches term
   collisions and the two-surface review floor; the fourth catches config
   generation. Resolution must be keyed off each record's `vertical`/`source_query`;
   never introduce a global "active vertical".
   (Bare `python` has none of the deps here — always `.venv/bin/python`.)
7. **Spot-check a real search** (small/test config) and confirm it returns the
   intended high-ticket businesses, not the denied low-ticket ones.

## Why this shape
Centralizing terms is what makes the engine reusable across niches (and is the
reason this repo can become a general operator engine, not a one-niche tool).
Scattering terms back into the consumers is the regression that makes the next
vertical a rewrite — don't.
