# Scoring v3 operating contract

Scoring v3 is a shadow policy until the calibration gates below pass. It adds a
rank for prioritization, but keeps the objective sendability checker as the
authority for anything that can ship. Gate 0 still prohibits autonomous sends.

## Outputs

Every newly scored row receives:

- `Scoring Policy Version`, exact `Scoring Profile ID`, and profile version.
- `Scoring V3 Rank` from 0–100 and component scores.
- `Scoring V3 Disposition`: `auto-eligible`, `manual-review`, or `reject`.
- An offer lane, named leak, blockers, reason codes, review-velocity provenance,
  and the complete machine-readable `Scoring V3 JSON` object.

The 100 available points are ICP fit 20, business demand/capacity 15, review
velocity 10, problem evidence 25, offer fit 15, reachability 10, and evidence
confidence 5. Reputation/compliance risks are independent gates rather than
hidden negative points.

Exact profiles currently exist for detailing and water restoration in Cleveland
and Columbus, plus the plumbing/Cleveland shadow-expansion cell. An absent exact
profile returns `manual-review` with no rank; v3 never falls back to another
market's profile.

Profiles resolve in `scoring_profiles/` as global safety defaults, vertical,
metro, then exact vertical/metro overrides. The directory is validated before
scoring for missing supported cells, invalid weights, duplicate terms,
inclusion/exclusion conflicts, and offer mappings that reference unknown lanes.
Known markets outside the declared pilot cells reject as outside target geography;
missing or ambiguous geography remains fail-closed manual review.

## Review velocity

Lifetime review count is demand/reputation context, not velocity. Velocity uses:

1. Google review-count snapshots at least 14 days apart; or
2. up to 60 newest timestamped reviews as a bootstrap.

Search records aggregate Google counts in `run_state/review_velocity.sqlite3`.
Enrichment attaches the available history. `collect_review_signals.py` provides
bounded DataForSEO submit, collect, and merge steps for the newest-review path.
The external-data cap defaults to `$10` and the deep stage is capped at 25 leads:
20 top/uncertain records plus five deterministic reject audits.

Velocity is normalized within the exact vertical/metro cohort. A cohort requires
20 measurable businesses. Bands are `surging` (80th percentile plus positive
acceleration), `active` (50th+), `steady` (20th–49th), `stale` (below 20th), and
`unknown`. Unknown receives a neutral five ranking points with lower confidence;
it can never cause rejection. Negative count deltas are provider corrections.
Reliably zero activity over a measured 90 days is always stale, even when a
zero-heavy cohort would place it at or above the median. Review observations
expire after the profile's configured freshness window (45 days by default),
and expired observations retain raw audit values but become unknown and cannot
remain active.

Velocity is a proxy for demand throughput, review-request discipline, and GBP
attention. It is not proof of revenue, exact workload, or review-request
frequency and must not be used as an outreach claim. It increases the commercial
materiality of a separately verified leak but cannot create sendability by itself.

Data source contracts:

- Google Places exposes aggregate review count and only a limited relevance-sorted
  review sample: <https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places>
- DataForSEO supports newest-first Google review tasks and bills in blocks of ten
  returned reviews: <https://docs.dataforseo.com/v3/business_data-google-reviews-task_post/>
- Production map visibility uses the direct Semrush Map Rank Tracker API, not MCP:
  <https://developer.semrush.com/api/local/map-rank-tracker-2/>

`collect_external_signals.py` exposes read-only Semrush campaign/metric and
PageSpeed collection plus a deterministic `merge` step into enriched JSONL.
Fresh normalized observations are consumed by ScoreV3; stale or undated signals
are retained as unknown observations. Semrush observations preserve query, grid,
device, location, collection timestamp, average position, and share-of-voice
context. LSA absence stays unknown unless affirmative evidence is
available. Third-party traffic or bounce estimates are context-only and do not
affect disposition; true bounce and conversion rates require access to the
business's first-party analytics and are therefore not asserted by this system.

## Review pain

Review text is classified into unreachable, no callback, quote follow-up,
scheduling, logistics, retention, service quality, and integrity/safety. An
operational category becomes a named leak only after two different reviews pass
both the classifier and the separate objective verifier. One severe recent
review may qualify only with explicit structural corroboration references.
Answer It, Close It, and Keep are selected from the verified category.
Workmanship, fraud, and safety complaints are reputation risks, never positive
sales pain. Review IDs, timestamps, bounded excerpts, and evidence references are
preserved in the scoring JSON together with classifier and verifier results.
Review collections expire under the same profile freshness contract as velocity.

## Calibration and activation

Use:

```bash
python scoring_calibration.py sample \
  --input output/scored_pilot_pool.csv \
  --output output/scoring_v3_primary.csv \
  --audit-output output/scoring_v3_audit.csv

python scoring_calibration.py evaluate \
  --input output/scoring_v3_primary_labeled.csv \
  --predictions output/scored_pilot_pool.csv \
  --audit-input output/scoring_v3_audit_labeled.csv \
  --output output/scoring_v3_calibration_report.json
```

Sampling fails unless all four cells contain 40 unique leads. Predictions and
legacy verdict fields are removed from the blind sheets; 25% are repeated for an
independent audit. Required Cohen's kappa is 0.70; missing or failed audit kappa
forces `launch_passed: false` even when disposition metrics pass.

Activation requires at least 90% human approval of aggregate auto-eligible
predictions and no more than 5% false rejects. Every cell must separately achieve
85% approval and no more than 10% false rejects. Until then, profiles remain
`launch_mode: shadow` and existing `Sendable` values are untouched.
Reports include the aggregate and per-cell gates plus breakdowns by offer lane,
review-velocity band, evidence confidence, and data source.

Annotate existing files without rescoring or overwriting them:

```bash
python annotate_legacy_scoring.py \
  --input output/scored_existing.csv \
  --output output/scored_existing_legacy_annotated.csv
```
