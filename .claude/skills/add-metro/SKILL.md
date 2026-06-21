---
name: add-metro
description: >-
  Add a new metro / geography to local-lead-machine search for the active
  vertical — shared geo slices plus a per-metro search config. Use this whenever
  the user wants to expand or change where they prospect: "expand to Columbus",
  "add Cincinnati / Pittsburgh", "run this in a new market", "we're running out of
  leads in Cleveland". Trigger on any request to broaden geographic reach. Low-
  density verticals (e.g. epoxy) need metro expansion to keep good leads flowing,
  so reach for this skill proactively when a metro's list looks exhausted.
---

# Add a metro

List volume scales by adding metros, not by loosening the ICP. This skill adds a
geography for the **currently active vertical** without hand-editing query lists.

## Status / prerequisites
- **Active now:** use the shared `geo_slices_ref` pattern. Metro slices live in
  `search_configs/geo_slices/<metro>.json` and per-vertical configs reference
  them so geography is authored once and reused across verticals.

## Procedure
1. **Author the metro's geo slices** as `search_configs/geo_slices/<metro>.json`
   (a JSON array of "City ST" + sub-area strings — core city, its east/west
   sides, and the affluent suburbs where high-ticket buyers cluster). Mirror the
   granularity of the existing Cleveland slices.
2. **Create the config(s)** `search_configs/<vertical>_<metro>_<size>.json` for
   the active vertical: reuse that vertical's queries (swap the geo token),
   `boost_terms`, and `exclude_terms`; set `geo_slices_ref: "<metro>"` (or inline
   the slices if `_ref` isn't supported); keep `min_rating`/`min_reviews` aligned
   with the ICP gate (4.3 / 5).
3. **Validate the config loads** — run the config-load path
   (`make_search_batch.py`) and confirm no missing-field / parse error.
4. **Dry-run a small search** to confirm the geo resolves and returns local
   businesses in the new metro before committing a full run.

## Guardrails
- Don't drop `min_rating`/`min_reviews` or strip `exclude_terms` to inflate
  volume — that re-introduces the low-ticket / bad-fit leads the ICP exists to
  reject. Volume comes from *more metros*, not a looser gate.
- One metro per invocation; prove it with a small config before scaling to `full`.
