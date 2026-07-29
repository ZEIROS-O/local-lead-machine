# Opportunity Scoring and Visual Website Audit

> Local snapshot copied from Notion on 2026-07-28T11:26:33.367Z.  
> Source: [Opportunity Scoring and Visual Website Audit](https://app.notion.com/p/3abc57f0654f80869221e3f46911f3b8)

## Summary
Add a capped deep-evidence stage between enrichment and final scoring. It will visually inspect candidate websites  
with Playwright and Codex vision, detect established businesses that underperform their local review cohort, and  
normalize every proven pain—website, estimate funnel, missed-call handling, reviews, operations, or automation—as a  
peer sales opportunity.
The completed plumbing batch will not be rescored. The first shadow pilot will use a new medium-high Cleveland  
water-restoration batch covering the city plus east and west suburbs.
## Implementation Changes
### Deep-evidence pipeline
- Keep base enrichment fast and run it without screenshots.
- Add collect_opportunity_signals.py after enrichment and before final LLM scoring in run_pipeline.py.
- Select 20 top or uncertain owned-site leads plus five deterministic lower-ranked control leads. Exclude no-site,  
social-only, Jobber-only, franchise, and hard-ICP rejects.
- Capture the actual homepage with Playwright:
	- Desktop initial viewport: 1440x1200.
	- Mobile initial viewport: 390x844.
	- One size-capped full-page desktop screenshot when feasible.
	- Record final URL, status, capture time, viewport, screenshot hash, redirects, and failures.
- Treat bot blocks, timeouts, and inaccessible sites as Unknown; never infer that the website is broken from a  
rendering failure.
- Stop the batch before final scoring if authentication, CLI invocation, schema validation, or image processing  
fails systemically. Individual-site failures remain Unknown and continue.
### Independent visual audit
- Add a reusable Codex subscription runner using the locally verified CLI interface:  
codex exec --ephemeral --sandbox read-only --ignore-user-config --output-schema … -i \<desktop\> -i \<mobile\> -.
- Run two independent, structured calls per inspected lead:
	1. Auditor proposes observable findings.
	2. Checker sees the same images plus the proposals and accepts or rejects each finding.
- Store receipt-based results under run_state/visual_audit/\<batch\>/, keyed by screenshot, prompt, schema, and model  
hashes. Changed evidence or prompts force reevaluation.
- Supported visual findings:
	- Outdated visual design
	- Premium-perception gap
	- Weak mobile layout
	- Unclear hero or service proposition
	- Weak CTA visibility
	- Weak gallery presentation
	- Weak trust-proof placement
- A visual opportunity qualifies only with either two distinct checker-confirmed findings or one severe cross-device  
failure. An overall subjective quality rating cannot qualify by itself.
- Use professional, concrete language in data and outreach; never store insulting descriptions.
### Opportunity model and scoring
Add a normalized opportunity_signals\[\] interface:
signal_id  
kind  
state  
angle  
offer_component  
pain_score  
confidence  
qualified  
evidence_refs  
observed_at  
expires_at  
safe_claims  
conditional_solutions  
blockers
- Normalize existing website, estimate-funnel, missed-call, tracking, review, and operational detectors into this  
interface without weakening their current evidence rules.
- Score each opportunity deterministically out of 100:
	- Evidence strength: 30
	- Commercial materiality: 25
	- Recurring likelihood: 20
	- Solution fit: 15
	- Resistance to a trivial one-off fix: 10
- Qualification requires score \>=65, confidence \>=0.75, valid evidence references, and the signal-specific proof  
rule.
- Select the strongest qualified opportunity by score, confidence, freshness, then stable signal ID. Preserve every  
other qualified opportunity.
- The LLM may select and phrase only validated candidates; it cannot invent opportunities or assign its own score.
- Retain the overall lead gates: A/B grade, demand/money evidence, reachability, commercial fit, safe claims,  
concrete opener, and sendability. A strong pain opportunity is necessary but not sufficient.
### Reviews, age, and reactivation
- Extract high-confidence business age only from explicit website claims such as “established,” “since,” or “serving  
for N years,” preserving the quote and source URL. Ignore copyright dates and generic staff-experience claims.
- Calculate the oldest timestamp among reviews actually observed and expose it as age_lower_bound_years. Label it  
“oldest observed review,” never “first review.”
- Compare each business against the full deduplicated, independent-business cohort for its exact vertical and metro;  
require at least 20 cohort members.
- Qualify Review Automation when:
	- Business age lower bound is at least three years.
	- Rating meets the vertical minimum.
	- Review count is at or below cohort p25 and no more than 60% of the cohort median.
- Five-plus years strengthens the evidence. Measured stale review velocity strengthens recurrence, active velocity  
weakens it, and unknown velocity remains neutral.
- Keep existing Review/Trust Proof Upgrade for poor placement of existing reviews on the website; Review Automation  
is a separate Keep opportunity.
- Reactivation remains a conditional solution: “if a usable past-customer list exists.” It cannot become a named  
leak or opener without first-party confirmation.
- Because DataForSEO documents newest/highest/lowest/relevant sorting but not oldest-first, the observed date must  
remain a conservative lower bound. DataForSEO review task documentation  
([https://docs.dataforseo.com/v3/business_data-google-reviews-task_post/](https://docs.dataforseo.com/v3/business_data-google-reviews-task_post/))
## Dashboard, Airtable, and Compatibility
- Add these output fields:
	- Opportunity Score
	- Selected Opportunity
	- Opportunity Signals
	- Opportunity Qualification
	- Visual Audit Verdict
	- Visual Findings
	- Business Age Lower Bound
	- Business Age Source
	- Review Count Percentile
	- Review Capture Gap
	- Conditional Solutions
- Update the dashboard to show deep-evidence progress, selected opportunity, opportunity score, visual verdict,  
review gap, and stale/missing evidence warnings.
- Preserve the existing behavior that exports every A/B lead to Airtable, including non-sendable leads.
- Reuse current Airtable fields where possible and negotiate optional new fields at runtime. Missing optional fields  
produce receipt warnings; missing required identity/contact fields remain fatal.
- Preserve scoring-schema-v2 compatibility. Precomputed opportunities are injected into existing scoring rather than  
requiring an immediate historical-artifact migration.
- Keep current dashboard/Airtable worktree changes intact.
## Shadow Pilot and Activation
- Add a medium_high search size with two result pages.
- Create water_restoration_cleveland_medium_high_003.json using Cleveland plus:
	- West: Lakewood, Rocky River, Westlake, Parma, Strongsville, North Royalton
	- East: Beachwood, Mayfield Heights, Solon, Twinsburg, Chagrin Falls, Mentor, Willoughby
- Pilot caps:
	- MAX_SEARCHES=64
	- MAX_DETAILS=150
	- MAX_ENRICH=120
	- MAX_VISUAL_LEADS_PER_RUN=25
	- MAX_VISUAL_CALLS_PER_RUN=50
	- One visual-call retry per lead
- Initially calculate new opportunity fields and a shadow disposition without changing the current Sendable result.
- Blind-label 40 unique pilot leads and report performance by opportunity kind.
- Activate the new gate only after existing calibration requirements pass:
	- Aggregate approval at least 90%
	- Aggregate false-reject rate at most 5%
	- Per-cell approval at least 85%
	- Per-cell false-reject rate at most 10%
	- Cohen’s kappa at least 0.70
- Do not modify or rescore the completed plumbing artifacts.
- Do not add autonomous sending; the pipeline continues to stop at scoring, packets, dashboard, and Airtable.
## Test Plan
```plain text
velocity modifiers, and score boundaries at 64/65.
```
- Test deterministic 20+5 selection, exclusions, screenshot metadata, bot-block handling, and screenshot freshness.
- Test Codex login preflight, safe argv construction, image attachment, schemas, receipt idempotence, retry  
behavior, and systemic-versus-individual failures.
- Test auditor/checker disagreement and rejection of unsupported visual claims.
- Test deterministic opportunity selection, tie-breaking, preservation of secondary opportunities, and prevention of  
LLM-invented opportunities.
- Verify shadow mode leaves existing sendability unchanged and active mode adds the qualified-opportunity gate.
- Verify dashboard state detection and Airtable export of sendable and non-sendable A/B leads.
- Run:
	- python -m unittest discover -s tests
	- Dashboard frontend tests and production build
	- Python compilation checks
	- git diff --check
- Pilot acceptance requires complete artifacts, nonzero visual coverage, no systemic visual failure, checker-result  
coverage, human calibration results, and no regression in existing scoring or exports.

