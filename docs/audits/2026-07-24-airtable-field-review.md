# Airtable Field Review — Redundant / Unnecessary Columns

**Date:** 2026-07-24
**Branch:** `claude/airtable-field-review-whmcpc`
**Scope:** read-only review of the 65-column Airtable prospect schema against the code that writes and
reads it. No Airtable calls made (no credentials in this environment); no pipeline behaviour changed.

> **Implemented 2026-07-24.** The base was cut over by hand and the code followed in five
> commits — label mappings first (`a504695`, `f888cb5`), then the deletions (`fac9469`), the
> demotions (`053d130`), and the `Packet Tier` consolidation (`d0a24d3`). The schema is now
> **54 columns**; `docs/airtable_schema_required.md` is regenerated from `_FIELD_SCHEMA`.
> Two things changed against the plan below, both found while implementing — see
> **Outcome** at the end. Follow-up findings were fixed in `6942e84` and `51aad92`.

**Verification basis:** read of `lead_machine/airtable_sync.py`, `upload_airtable.py`,
`lead_machine/sendability.py`, `lead_machine/score_schema.py`, `lead_machine/manual_scores.py`,
`score_leads.py`, `query_ready_to_send.py`, `mark_sent.py`, `lead_machine/hermes_queue.py`,
`lead_machine/lead_card.py`, `make_packets.py`, `lead_machine/dashboard/data.py`;
`python -m unittest discover -s tests` → **536 passed, OK** (baseline, unchanged);
plus four executable probes described inline below.

---

## Summary

The schema is not merely bloated — **parts of it are already empty in the base without anyone being
told.** Three single-select columns are fed values the write path cannot normalize, so they are
silently dropped from every payload. Separately, five columns are exact restatements of other
columns, and one pair should collapse into a single richer select.

| Finding | Columns | Recommendation |
|---|---|---|
| A. Unfillable — write path drops 100% of values | Best Service Angle | **Delete** |
| B. Unfillable — write path drops most values | Offer Fit, Site Status | **Fix the mapping** (do not delete) |
| C. Exact duplicate of another column's content | Money Signal, Review Demand Band | **Delete** |
| D. 1:1 restatement of Status | Next Action | **Delete** |
| E. Never written by anything | Assigned To | **Delete** |
| F. Two booleans that should be one select | Sendable + Packet Eligible | **Consolidate → `Packet Tier`** |
| G. Redundant *as a gate*, useful as diagnostics | Commercial Fit State, Contact Eligibility | **Keep, document** |
| H. Write-only, packet-only detail | 4 form/CTA columns, Current Website Quality | **Demote** (drop from Airtable, keep in CSV/packets) |

Net: **12 columns deleted, 1 added — 65 → 54** with no loss of information, plus 2 correctness fixes.

---

## A. `Best Service Angle` — the column can never receive a value

`best_service_angle()` (`score_leads.py:955-972`) returns one of six prose blurbs keyed off the
vertical, e.g. `"Roof replacement, roof repair, high-intent estimate requests, and faster
call-to-booking flow"`. `Best Service Angle` is in `SERVICE_ANGLE_FIELDS`
(`lead_machine/airtable_sync.py:104`), so every write is normalized through `SERVICE_ANGLE_LABELS`,
which contains none of those six strings.

Probe — every value the function can return:

```
Residential roof replacement, storm-damage lead capture...  -> None
Roof replacement, roof repair, high-intent estimate...       -> None
Masonry, retaining walls, patios, concrete, brickwork...     -> None
High-ticket outdoor living, patios, retaining walls...       -> None
Hardscaping, patios, retaining walls, drainage...            -> None
High-intent local service leads, estimate requests...        -> None
```

`build_airtable_payload_with_drops` (`airtable_sync.py:246-250`) discards a `None` normalization and,
because the field is typed, records it as a dropped field — so every single record also emits
`[WARN] Dropped unsupported Airtable field values` (`upload_airtable.py:622-630`). End-to-end:

```
card fields: Business Name, Best Service Angle, Offer Fit
payload fields: ['Business Name', 'Place Key']
dropped:        ['Best Service Angle', 'Offer Fit']
```

On the LLM/manual path the column is not merely empty, it is **definitionally a duplicate**:
`manual_scores.py:397` and `:406` assign `Best Service Angle` and `Offer Fit` from the identical
source, `validated.get("best_angle", "")`. `manual_scores.py:280` does `row["Offer Fit"] =
row["best_angle"]`, and both `lead_card.py:83` and `scoring_v3.py:411` read them as one value with a
fallback chain (`row.get("Offer Fit") or row.get("Best Service Angle")`).

**Delete `Best Service Angle`.** It is empty in the base today, and where it is populated at all it is
a byte-for-byte copy of `Offer Fit`. Also drop it from `hermes_queue.ITEM_FIELD_MAP:114`, which is
currently mapping a permanently blank column into the send queue.

---

## B. `Offer Fit` and `Site Status` silently drop most of their values

Not a redundancy finding, but it is why the base looks emptier than the schema suggests, and it must
be fixed before any deletion pass — otherwise you will "verify" columns as unused that are only
unused because the writer is broken.

**`Offer Fit`** — `choose_offer_fit()` (`score_leads.py:784-869`) returns ten distinct labels.
Seven do not survive `SERVICE_ANGLE_LABELS`:

```
Website Rebuild              -> 'Website Rebuild'      ✓
Premium Perception Gap       -> 'Website Rebuild'      ✓
Estimate Funnel              -> 'Estimate Funnel'      ✓
Portfolio Rebuild            -> None                   ✗
Mobile Quote Path Cleanup    -> None                   ✗
Review/Trust Proof Upgrade   -> None                   ✗
Service Page Cleanup         -> None                   ✗
Website Cleanup              -> None                   ✗
Advanced Optimization        -> None                   ✗
Tracking / Missed Call       -> None                   ✗
```

Two of the four declared select options — `No-Site Launch` and `Automation / Follow-Up` — are
**unreachable**: the labels that map to them (`social/jobber escape`, `optimization only`) are legacy
strings the current scorer no longer emits. Social-only sites now return `Website Rebuild`
(`score_leads.py:793-795`) and optimization-only returns `Advanced Optimization`, which maps to
nothing.

**`Site Status`** — the enricher emits `Broken Website` and `Jobber Only`; `SITE_STATUS_LABELS`
(`airtable_sync.py:91-101`) has `broken` but not `broken website`, and nothing for `jobber only`:

```
No Website     -> 'No Website'      ✓        Broken Website -> None  ✗
Social Only    -> 'Social Only'     ✓        Jobber Only    -> None  ✗
Loaded         -> 'Loaded'          ✓
Unknown        -> 'Unknown'         ✓
```

So `Site Status` is blank in Airtable for exactly the broken-site leads that grade A
(`base_priority_from_delta`, `score_leads.py:901-905`).

**Fix:** add `broken website → Broken` and `jobber only → Social Only` to `SITE_STATUS_LABELS`; add
the seven missing offer-fit labels to `SERVICE_ANGLE_LABELS` (or widen the `Offer Fit` select to the
ten labels the scorer actually produces, which is the more honest option — the four-option select is a
lossy bucketing nobody reads back).

**Related value-domain mismatch to check in the base:** `Contact Form Detected` and `Estimate Form
Detected` are documented as `Single select — Yes, No`, but `score_leads.py:1392-1393` writes
`"Unknown"` as the default. These fields are *not* in `TYPED_AIRTABLE_FIELDS`, so nothing normalizes
them and nothing drops them — the value goes to Airtable as-is. If the live base does not have
`Unknown` as an option, the write returns 422 and `send_airtable_plan` (`upload_airtable.py:657-661`)
aborts the entire sync. Confirm against the live base.

---

## C. `Money Signal` and `Review Demand Band` are already inside `Buyer Intent Signals`

`buyer_intent_summary()` (`lead_machine/buyer_signals.py:125-131`) emits:

```
money_signal=yes; tools=...; review_demand_band=high; review_velocity=...; ads=...
```

Both `Money Signal` (`sendability.py:673`, always `"Yes"`/`"No"`) and `Review Demand Band`
(`score_leads.py:1446`) are separate columns holding substrings of that same field. Nothing reads
either back from Airtable — `query_ready_to_send.py` fetches `Money Signal` into `DISPLAY_FIELDS`
alongside `Buyer Intent Signals`, and `Review Demand Band` is not fetched at all.

Two further signs these are vestigial:

- `score_leads.py:1445-1446` writes `Review Fit Band` and `Review Demand Band` from the *same call*
  to `review_fit_band(record, vertical)` — an outright duplicate assignment (`Review Fit Band` is not
  synced, so only the dead half is invisible).
- `Money Signal` is declared `Single line text` and has two incompatible value domains in production:
  `sendability.py:673` writes `"Yes"`/`"No"`, while `manual_lead_insert.py:441` writes
  `"High-ticket service mentioned"`. Consumers testing it as a boolean
  (`buyer_signals.py:100-105`) read the prose as falsey.

**Delete both.** Keep `Buyer Intent Signals` as the single structured column.

---

## D. `Next Action` is a 1:1 restatement of `Status`

`Next Action` has exactly two writers in the entire codebase, each set in lockstep with a `Status`:

| Writer | Status | Next Action |
|---|---|---|
| `upload_airtable.py:411-414` | `Mockup Needed` | `Build mockup` |
| `manual_lead_insert.py:617,624` | `Manual Review` | `Review lead` |

Nothing reads it back. It is a second column encoding a strictly coarser version of `Status`.

**Delete.** If a per-lead free-text next step is wanted later, that is a genuinely new field with a
human writing into it — not this one.

---

## E. `Assigned To` is never written

The only reference in the repo is the `FIELDS` list of `parse_airtable_records.py:31`, a one-off
parser for a pasted text dump of the base. No writer, no reader, single operator.

**Delete.**

---

## F. `Sendable` + `Packet Eligible` → one `Packet Tier` select

These two booleans encode a four-state variable that the pipeline **already computes and then throws
away**. `evaluate_packet_tier` (`sendability.py:637-652`) produces
`Sendable Packet | Manual Review Packet | Watchlist | No Packet`, stored as `Packet Tier`
(`sendability.py:693`) — which is not in `AIRTABLE_ALLOWED_FIELDS` and never syncs. The two booleans
are then derived from it (`sendability.py:696-706`):

| Packet Tier | Sendable | Packet Eligible |
|---|---|---|
| Sendable Packet | `Yes` | `Yes` |
| Manual Review Packet | `No` | `Yes` |
| Watchlist | `No` | `""` |
| No Packet | `No` | `""` |

The two booleans **cannot distinguish Watchlist from No Packet**. One `Packet Tier` single-select is
one column instead of two and strictly more informative.

**Consolidate.** Add `Packet Tier` to `AIRTABLE_ALLOWED_FIELDS` / `CSV_AIRTABLE_FIELDS`, backfill from
`(Sendable, Packet Eligible)`, then delete both booleans. Note this touches the readiness gate in
`sendability.contact_readiness_failures` and the `filterByFormula` in `query_ready_to_send.build_formula`
— see G before changing either.

---

## G. The "four-field readiness contract" is one bit spread over four columns

`contact_readiness_failures` (`sendability.py:477-492`) and the Airtable formula in
`query_ready_to_send.build_formula:97-107` both require all four of:

```
Sendable = Yes  AND  Packet Eligible = Yes
AND Commercial Fit State = commercially-sendable  AND  Contact Eligibility = contact-now
```

Three of those four conjuncts are logically implied by the first. `evaluate_commercial_fit` turns
`contact-now` without `commercially-sendable` into a state error (`sendability.py:530-531`), and any
state error becomes a send blocker (`sendability.py:384-385`), forcing `Sendable = No`.

Probe — 8,000 mutated rows built from the repo's own `merge_manual_score` fixture, varying grade,
score, confidence, reviews, rating, site status and all four commercial/asset states:

```
Sendable = Yes in 2704 rows
  of those, Packet Eligible      != 'Yes'                   : 0
  of those, Contact Eligibility  != 'contact-now'           : 0
  of those, Commercial Fit State != 'commercially-sendable' : 0
```

On the deterministic path the redundancy is explicit in source — `score_leads.py:1461` sets
`Contact Eligibility` as a pure function of `Commercial Fit State`:

```python
"Contact Eligibility": "contact-now" if commercial_state == "commercially-sendable" else "hold-commercial-case",
```

**Do not delete these columns.** The implication runs one way only: on *non-sendable* rows the three
extra columns are what explain *why*, and that diagnostic value is real. They also function as
tamper-detection against hand-edits in the base — which is exactly what
`query_ready_to_send.py:158-163` uses `is_contact_ready` for on read-back.

What should change is the framing: `AGENTS.md` and `docs/` describe this as four independent gates.
It is one gate plus three cross-checks. Document it that way so nobody "strengthens" the contract by
adding a fifth derived column.

**Related, keep as-is:** the asset quartet (`Asset State`, `Promise State`, `Asset ID`,
`Asset Verifier Verdict`) looks over-normalized but is not. Enumerating the state space against
`score_schema.validate_current_scored_row` gives **12 legal `(contact, asset, promise)` triples out of
27** — constrained, but genuinely more than the ~2 bits a single select would carry. `Asset Verifier
Verdict` is the weakest of the four (on any sendable row, `Asset State = verified-existing` already
implies `Asset ID` present and verdict `PASS`, per `sendability.py:534`), but it is the receipt that
makes that implication auditable. Keep.

---

## H. Write-only columns that belong in the packet, not the base

No code path reads these back from Airtable. The only Airtable readers are
`query_ready_to_send.DISPLAY_FIELDS` (47 fields), `mark_sent.py` (`Status`, `Last Contacted`,
`Outreach Channel`, `Consent Posture`), and `upload_airtable.fetch_existing_records` (`Place Key`,
`Google Place ID`, `Phone`, `Website URL`, `Business Name`, `City` — the upsert keys). The dashboard
reads local `run_state/`, not Airtable.

Of the 17 write-only columns, most earn their place as things a human reads in the base
(`Google Maps URL`, `Skip Reason`, `Send Blockers`, `Visible Pain Score`/`Reasons`, `Packet Path`) or
are load-bearing for upsert (`Place Key`). These six do not:

- `Contact Form Detected`, `Estimate Form Detected`, `Form Strength`, `Form Detection Method`,
  `CTA Strength` — five columns of scraper detail consumed entirely *before* Airtable, by
  `has_strong_estimate_form` / `choose_offer_fit` / `icp_lanes`. Their conclusion already reaches the
  base as `Offer Fit` + `Visible Pain Reasons`, and the full detail already reaches the operator via
  `make_packets.py`. `Form Strength` alone subsumes the two booleans
  (`has_strong_estimate_form` = `estimate_form_detected == yes AND form_strength == strong`,
  `score_leads.py:420-424`).
- `Current Website Quality` — a four-bucket restatement (`N/A`/`Poor`/`Okay`/`Good`) of
  `Site Status` + the weakness score, derived in `infer_website_quality` (`score_leads.py:872-898`).
  Two of its four buckets are pinned directly by `Site Status`. Used for local sort ordering
  (`score_leads.py:3124`), which does not need the column to exist in Airtable.

**Demote:** keep them in the scored CSV and the packets, remove from `AIRTABLE_ALLOWED_FIELDS`.
Keep `Form Strength` in the base as the one at-a-glance form signal; drop the other four plus
`Current Website Quality`.

---

## Recommended cut

Delete (12 columns):

```
Best Service Angle        (A — always empty; duplicate of Offer Fit)
Money Signal              (C — inside Buyer Intent Signals)
Review Demand Band        (C — inside Buyer Intent Signals)
Next Action               (D — 1:1 with Status)
Assigned To               (E — never written)
Sendable                  (F — replaced by Packet Tier)
Packet Eligible           (F — replaced by Packet Tier)
Contact Form Detected     (H — subsumed by Form Strength)
Estimate Form Detected    (H — subsumed by Form Strength)
Form Detection Method     (H — scraper detail, packet-only)
CTA Strength              (H — scraper detail, packet-only)
Current Website Quality   (H — restates Site Status + weakness)
```

Add (1): `Packet Tier` single-select — `Sendable Packet | Manual Review Packet | Watchlist | No Packet`.

Net **65 → 54**. `Form Strength`, `Site Status`, `Offer Fit`, `Buyer Intent Signals`, `Status`, and
`Packet Tier` between them carry everything the twelve deleted columns carried.

Fix before deleting anything (B): `SITE_STATUS_LABELS` and `SERVICE_ANGLE_LABELS` coverage; confirm
the `Unknown` option exists on the two form-detected selects, or stop writing it.

## Suggested order

1. **B first.** Land the label-mapping fixes and re-run a live sync. Until the writer stops dropping
   values, an "is this column empty?" check in the base cannot distinguish unused from broken.
2. **A, C, D, E.** Pure deletions, no gate logic involved. Remove from `_FIELD_SCHEMA`,
   `CSV_AIRTABLE_FIELDS`, `AIRTABLE_ALLOWED_FIELDS`, `AIRTABLE_CARD_FIELDS`, `ITEM_FIELD_MAP`,
   `DISPLAY_FIELDS`, and `docs/airtable_schema_required.md`.
3. **H.** Same shape as step 2, but verify the packet templates still render the detail first
   (`make_packets.py:56-115`).
4. **F last.** It changes the readiness gate and the `filterByFormula`, so it wants its own batch
   with `tests/test_send_queue_scripts.py` and `tests/test_readiness_migration.py` green.

Each step is an independently-revertible batch verified with `python -m unittest discover -s tests`,
per `CLAUDE.md`.

## Migration mechanics

`airtable_schema_cutover.py` already implements exactly this pattern for the seven `Legacy *` fields —
GET-only snapshot, backup with SHA-256, conflict report, generated manual-deletion checklist, then a
`verify` pass that confirms record count held and the columns are gone. Reuse it: extend
`LEGACY_FIELD_PAIRS` (or generalize it to a `FIELDS_TO_DELETE` list for the columns with no canonical
survivor) rather than deleting by hand in the UI.

## Not verifiable from here

No Airtable credentials in this environment, so everything above is derived from code, not from the
live base. Two things to confirm with `airtable_schema_cutover.py prepare` before acting:

- Whether the seven `Legacy *` columns still exist (the cutover may never have been executed).
- Whether stragglers referenced in code but absent from the write schema are still present:
  `Google Place ID` (read by `upload_airtable.fetch_existing_records:596`), and
  `Evidence Notes`, `Manual Reviewed`, `Desktop Screenshot Path`, `Mobile Screenshot Path`,
  `Created Time`, `Last Modified Time` (all in `parse_airtable_records.FIELDS`, none in the write
  schema). If present and unpopulated, they are additional deletion candidates.

---

## Outcome (implementation notes, 2026-07-24)

Landed as planned, in the sequence above, each batch green on the full suite. The schema went
**65 → 54**; suite went 536 → 543 tests. Two things differed from the plan.

**1. Section B was worse than reported — a second vocabulary.** The review checked the values
`enrich_leads` assigns (`Broken Website`, `Jobber Only`, …). It missed that
`evidence._status_from_site` returns a *different, lowercase* set — `missing`, `broken`, `weak`,
`okay`, `social_only`, `jobber_only`, `unknown` — and that `manual_scores.py:407` writes that
straight into the same `Site Status` column. That is the LLM path, which is the path real batches
run on. `weak` and `okay` still dropped after the first fix; both now map to `Loaded`, since the
severity separating them is already carried by Visible Pain Score and Reasons and this select has
no option for it. Coverage now asserts against **both** vocabularies, which is what would have
caught it.

Verified end-to-end after the fix: all seven evidence statuses build a payload with **zero**
dropped fields.

**2. `Sendable` + `Packet Eligible` needed a derivation in both directions.** The plan only
covered reading the tier back. Two more cases turned up:

- `sendability.readiness_flags` reconstructs the two booleans from `Packet Tier` for rows read
  back from the base, which no longer carry them. Explicit columns always win, so a hand-edited
  `No` in Airtable is still honoured — the cross-check value described in section G survives.
- `upload_airtable` needed the mirror: a scored CSV written before `Packet Tier` existed would
  otherwise upload with no readiness column at all, and `query_ready_to_send`'s filter could never
  match it. It now judges readiness on the source CSV row rather than the filtered payload and
  derives a tier when absent, marked defaulted so a PATCH cannot clobber a tier already in the base.

One behaviour change falls out of this and is worth knowing: a legacy-shaped CSV carrying
`Sendable=Yes` with no commercial fit or contact eligibility now lands as **Manual Review Packet**,
not as a sendable row. That is correct — it never satisfied the four-field contract; it just
carried a `Sendable` string that the old code passed through verbatim.

### Still open

**Nothing in the schema. One data problem, described under "Live base verification" below.**

### Closed after the outcome above

**`No-Site Launch` is now reachable from the deterministic path** (`6942e84`). It was only ever
produced by the LLM path, via `Social/Jobber Escape`; `choose_offer_fit` called every no-site case
`Website Rebuild`, so the operator got a "rebuild your site" pitch for a business whose only web
presence is a Facebook page or a Jobber booking link. The scorer now splits on whether an owned site
exists — `No Website` / `Social Only` / `Jobber Only` and the booking-URL case return
`No-Site Launch`, `Broken Website` keeps `Website Rebuild`. Same offer score, same A grade.

`CONCRETE_PACKET_OFFERS`, `base_priority_from_delta` and `infer_website_quality` were updated in the
same commit — without the first, a no-site lead would have silently lost packet eligibility.
`DELTA_RANK` and `sendability.MANUAL_REVIEW_OFFERS` already carried the label.

One scoring behaviour change fell out, deliberately: **`Jobber Only` was missing from both the
offer-fit and the grade paths**, so a business with no owned site scored as `Advanced Optimization`
with `Good` website quality. It is now grouped with `No Website` / `Social Only`, which is how
`enrich_leads.py:1299,1310` has always treated it. A differential across every site-status ×
booking-flag combination shows this is the only case that moves — **11 of 12 identical** in grade,
packet eligibility, sendability, score and tier.

**The two orphaned `Legacy *` pairs are handled** (`51aad92`). Left as pairs, `prepare` would have
reported the canonical half missing and refused forever — and because every value in an orphan is
legacy-only by definition, the other blocking condition, they could never have been cleared. They
now live in `LEGACY_ORPHAN_FIELDS`: backed up, listed in the delete checklist, and verified absent
afterwards like any other legacy field, but excluded from a pair comparison with nothing to compare
against. Their populated-row counts are reported and the checklist warns that `backup.json` is the
only remaining copy. A cross-file test ties `DEMOTED_CANONICAL_FIELDS` to `LEGACY_ORPHAN_FIELDS` so
neither list can drift alone.

---

## Live base verification (2026-07-24, via the Airtable connector)

Read-only pass over `Local Lead Machine CRM` / `Prospects` (`appLbCDdUDSZf7qaK` /
`tblUwMo16aVtOnCEE`). All **132 of 132** records read. Nothing written.

### Schema: clean

- **All 54 columns the code writes exist in the base.** Nothing would 422
  `validate_airtable_fields`.
- **All 12 deleted columns are gone.**
- **No `Legacy *` columns** — the earlier cutover was completed, so
  `LEGACY_ORPHAN_FIELDS` has nothing left to clean up in this base. The handling stays as a guard.
- **No stragglers**: `Google Place ID`, `Evidence Notes`, `Manual Reviewed`, the screenshot paths,
  `Created Time` and `Last Modified Time` are all absent. `fetch_existing_records` still probes
  `Google Place ID` and degrades correctly when it is missing.
- **Every select option the code can emit exists**, checked value-by-value against
  `SITE_STATUS_LABELS`, `SERVICE_ANGLE_LABELS`, `AI_CONFIDENCE_LABELS`, `VERTICAL_LABELS`,
  the commercial/asset state sets and the packet tiers. Zero gaps — a live sync cannot 422 on a
  select.
- 11 base columns the pipeline never touches (`Notes`, `Lead Source`, `Sales Activities` and the
  `Sales — *` block). Human CRM fields; leave them.

### Data: `Packet Tier` was never backfilled

**`Packet Tier` is empty on 130 of 132 records** (1 `No Packet`, 1 `Manual Review Packet`). The
hand cutover deleted `Sendable` and `Packet Eligible` without populating their replacement, so:

**`query_ready_to_send` currently returns 0 records.** The send queue is dark.

Three records satisfy every other clause of the filter — `commercially-sendable` + `contact-now` +
`New` — and are excluded only by the empty tier:

| Business | Grade | Place Key |
|---|---|---|
| Carey Plumbing | A | `ChIJVbUnpRLxMIgRvxKj43fAVAw` |
| C & S Plumbing Heating & Sewer | B | `ChIJ6ZbQ5EDvMIgR09hMIC5dEPM` |
| Wiemels-Hiros Plumbing Co. | B | `ChIJ2exhP5jtMIgR3K7rErzGbxw` |

**This cannot be safely repaired from inside Airtable.** The tier is a function of the full
sendability gate — grade, score, confidence, visible pain, evidence refs, safe claims — and the two
columns that recorded its outcome were deleted. Nothing left in the base reconstructs it, and no
scored CSVs exist in this checkout. Deriving a tier from the commercial gates alone would mark leads
sendable on a guess, which is the exact failure the contract exists to prevent.

**The correct fix is a pipeline re-sync.** `upload_airtable` matches existing records by `Place Key`
and PATCHes, so re-running it over the scored CSVs for these batches sets `Packet Tier` from real
scored data and leaves everything else intact. Until then the base is accurate but the queue is
empty — a safe failure, not a silent one.

### Cosmetic: unused select options

Only 2 records use any non-code option: `Offer Fit = "Review / Trust Proof"` (Malone's Epoxy,
Armorcoat Inc, both from the 2026-06-07 batch). The current code maps
`Review/Trust Proof Upgrade → Website Rebuild`, so that option will never be written again.

Unused and safe to delete if you want the selects tidy: `AI Confidence → "No-Site Launch"`,
`Site Status → "Audit site / booking flow"`, `Lead Priority → "(440) 537-2459"`, `"D"`, `"New"`,
`Offer Fit → "Mediocre"`, `"Not Fit"`. The first three are data-entry accidents — a phone number and
an offer-fit value that landed in the wrong column and auto-created an option.

**Do not delete `Lead Priority → "Skip"`** even though no record uses it: `classify_priority` can
return `Skip` and `build_airtable_payload` writes the grade through untyped, so removing the option
would 422 the next sync that includes a skip-graded lead.
