# Local Lead Machine — Architecture Review

> Local snapshot copied from Notion on 2026-07-28T11:20:46.542Z.  
> Source: [Local Lead Machine — Architecture Review](https://app.notion.com/p/3abc57f0654f804aae5edeb4c564b780)

**Reviewed against:** the audit you supplied (enrichment, evidence, LLM scoring, sendability, shadow scoring-v3).  
**Verdict up front:** the engineering is careful and the failure modes are documented honestly. The design is wrong in one specific structural way, and that one thing generates most of the individual defects below.
---
## The diagnosis
Your audit frames the problem as *"the primary score is an LLM judgment rather than a deterministic formula."* That's true, but it's the symptom.
**The actual structural defect: you have two systems of record for lead quality, and neither one is authoritative.**
The LLM produces a 100-point score that gates nothing on its own. Fifteen deterministic gates produce `Sendable` and can veto the LLM. A third system — exact-cell scoring_v3 — produces the most defensible number in the entire pipeline and is architecturally forbidden from affecting anything. When three systems disagree, the resolution rule is "whichever one happened to run last in the function." That is not a scoring architecture; it's three scoring architectures in a trench coat.
The consequence is concrete and you already found it: **rows with ****`hard_icp_skip=true`**** became sendable.** That's not a bug in `icp.py`. It's what happens when the same decision is implemented in three places and only two of them agree on who decides.
**And the second thing, which is bigger:** nothing in this system is validated against whether a business ever replied. Seven weighted dimensions, fifteen gates, four review bands, five calibrated cells — and zero feedback from outcome. Every weight in the model is currently a guess with a decimal point on it. You have built a very precise instrument that has never been calibrated against the only variable that pays you.
Everything below is prioritized by that lens: **P0 = it can send something false or unlawful. P1 = it makes the system unmeasurable. P2 = it costs money or velocity.**
---
## P0 — Fix before the next send
### P0-1 · The hard ICP gate is not a gate
**Finding:** the `api-llm` production path drops suppressed leads but does not filter `derived.hard_icp_skip`. The manual batch builder does. Stored artifacts contain `hard_icp_skip=true` rows that reached `Sendable=Yes`.
**Why it's P0:** those rows are franchises, chains, adjacent businesses, and sub-threshold operators. Every one you emailed was a wasted send at best and a brand-damaging one at worst — you told a SERVPRO franchisee their website has problems they cannot fix and would not be paid to fix.
**Fix:**
1. Extract a single `admit(lead) -> Admitted | Rejected(reason)` function. It is the *only* place a lead may enter scoring. Both entry points call it. Neither entry point may re-implement any part of it.
2. Move `hard_icp_skip`, suppression, and the universal disqualifiers into it.
3. Quarantine the historical artifacts: `UPDATE ... SET sendable='quarantined' WHERE hard_icp_skip = true`. Do not silently leave them in a sendable state.
**Proof of fix — make this a contract test that runs in CI:**
```plain text
assert count(rows where sendable == 'Yes' and hard_icp_skip == true) == 0
assert count(rows where sendable == 'Yes' and suppressed == true) == 0
```
These two assertions should run against every artifact file in the repo, not just against fixtures.
**Effort:** 1 day.
---
### P0-2 · Two scoring paths that see different data
**Finding:** the API path sends a distilled capsule (\~15–40% of the enrichment record). The manual/subscription path sends the full evidence object, buyer-intent object, ICP lane, and up to 12,000 characters of site text.
**Why it's P0:** the same lead gets different scores depending on which door it walked through, and you cannot tell from the stored row which door that was. This makes every calibration exercise you run — including the shadow scorer's disagreement detection — meaningless, because disagreement may be a data-availability artifact rather than a scoring artifact.
**Fix:** one `build_capsule(lead, profile, verbosity)` function. Path differences become a parameter, not a code fork. Stamp `capsule_version` and `capsule_verbosity` on every scored row.
**Effort:** 2 days. **Do this before P1-5**, because you cannot promote the deterministic scorer while the input to the LLM is nondeterministic.
---
### P0-3 · The suppression tests are the three that fail
**Finding:** 681 tests, 3 failures, all suppression "missing source" tests whose entry points reload the real `.env`.
**Why it's P0 and not P2:** of all the subsystems you could have red tests in, suppression is the one where a defect is a legal and reputational event rather than a quality event. "The suite is green except for suppression" is the same sentence as "the brakes work except when stopping."
**Fix:** the tests are failing because the code reads global environment state at call time. That's the real defect — the tests are correctly reporting it. Inject config as a parameter; construct it once at the process boundary. `monkeypatch`-ing `.env` is a workaround, not a fix.
**Effort:** 1 day. **Rail:** no send job may execute if the suppression test module is not green. Wire that into the send script, not just CI.
---
### P0-4 · Screenshot paths sent to a text-only endpoint
**Finding:** the request includes local screenshot *paths*, not pixels. The model cannot see them. Visual-audit fields may nonetheless influence scoring and copy.
**Why it's P0:** a filename in the context is an invitation to confabulate. `/screens/joespaving_mobile.png` in a prompt about conversion pain is a strong prior toward "their mobile site is cluttered" — a claim the model has no basis for and the prospect can refute in one tap.
**Fix — pick one, today:**
- **(a) Strip them.** Remove all screenshot paths from the capsule. Add an explicit instruction: no claim about visual appearance is permitted unless a `visual_audit_*` field is present in the input. Zero cost.
- **(b) Send the pixels.** The Messages API accepts base64 images. Attach the mobile screenshot only, at reduced resolution. Costs tokens; buys real visual grounding.
Do (a) now and evaluate (b) as an experiment against a labeled set. Do not leave the current state, which has the cost of (a) and the risk of (b).
**Effort:** 2 hours.
---
## P1 — Fix this month
### P1-5 · Invert the authority: deterministic scores, LLM extracts
**Finding:** scoring_v3 runs *after* sendability, in shadow, and can neither promote nor demote. Nine of fourteen niche×metro cells have no profile at all.
This is the design change that matters most. The current split gives the LLM the job it's worst at (assigning a calibrated numeric score from a rubric) and denies it the job it's best at (reading a messy page and reporting what's on it).
**Target split:**
<table header-row="true">
<tr>
<td>Job</td>
<td>Owner</td>
<td>Why</td>
</tr>
<tr>
<td>Is this the right business?</td>
<td>Deterministic gate</td>
<td>Binary, auditable, free</td>
</tr>
<tr>
<td>Is it the right size?</td>
<td>Deterministic gate</td>
<td>Arithmetic</td>
</tr>
<tr>
<td>What's on the page?</td>
<td>**LLM**</td>
<td>Genuinely hard; unstructured input; this is the model's actual comparative advantage</td>
</tr>
<tr>
<td>How bad is the pain, numerically?</td>
<td>Deterministic (scoring_v3 weights)</td>
<td>Reproducible, tunable, cheap, explainable to yourself in six months</td>
</tr>
<tr>
<td>What do we say to them?</td>
<td>**LLM**, constrained to templates</td>
<td>Language, with a leash</td>
</tr>
</table>
**Sequence:** ship the composed profiles (all 14 cells exist — see `icp_profiles_v1.json`) → run v3 in shadow for two weeks measuring agreement → promote v3 to authoritative → demote the LLM score to a tiebreak/ranking input.
**Kill criterion for the LLM score:** if, on a 200-lead labeled set, deterministic v3 and the LLM agree on `Sendable` more than 90% of the time, the LLM score is not earning its cost and comes out of the gating path entirely.
---
### P1-6 · The LLM rewrites the claim it was supposed to cite
**Finding:** the model can return rewritten pain reasons and send triggers that override the enrichment versions. Evidence IDs are validated; *semantic equality with the original trigger is not.*
**Why it matters:** this is the exact mechanism by which a correctly-detected weak form ("form present, 2 fields, no phone field") becomes an outreach line that says "your contact form is broken." The evidence ID validates. The claim is false. You find out when someone replies angrily, or more likely, when nobody replies at all and you never learn why.
**Fix:** the model does not write claims. It selects them.
```plain text
claim_templates:
  form_absent_high_ticket:
    text: "There's no way to request a quote on {domain} — every lead has to call."
    requires: [form_state == absent, ticket_low >= 2000]
  form_weak:
    text: "The form on {domain} asks for {field_count} fields and doesn't capture {missing}."
    requires: [form_state == present_weak]
  no_mobile_cta:
    text: "On a phone, there's no tap-to-call above the fold on {domain}."
    requires: [mobile_cta_state == absent, render_status == ok]
```
The model returns `{"template_id": "form_weak", "evidence_ids": ["e_412","e_418"]}`. Rendering is deterministic. Free-text is confined to the personalized first line, which references the *business*, never a technical claim.
This also collapses your review burden: you audit \~30 templates once instead of auditing every outbound email forever.
---
### P1-7 · Five leads per call is costing you both accuracy and money
**Finding:** the default capsule packs roughly five leads per API call.
**The accuracy problem:** leads in the same call anchor on each other. Lead 3 gets scored relative to leads 1 and 2, not relative to the rubric. You cannot detect this in production because you never score the same lead twice.
**The cost problem:** batching exists to amortize the system prompt. Prompt caching does that better. Per Anthropic's published rates (verify at anthropic.com/pricing — note the Sonnet 5 introductory rate ends 31 Aug 2026), with a \~2,500-token rubric and \~1,800-token capsule:
<table header-row="true">
<tr>
<td>Configuration</td>
<td>Effective input/lead</td>
<td>Cost/lead (intro rates)</td>
<td>Cross-lead anchoring</td>
</tr>
<tr>
<td>Today: 5/call, no cache</td>
<td>\~2,300</td>
<td>\~\$0.010</td>
<td>**yes**</td>
</tr>
<tr>
<td>1/call, no cache</td>
<td>\~4,300</td>
<td>\~\$0.014</td>
<td>no</td>
</tr>
<tr>
<td>1/call + prompt caching</td>
<td>\~2,050</td>
<td>\~\$0.009</td>
<td>no</td>
</tr>
<tr>
<td>1/call + caching + Batch API</td>
<td>\~2,050</td>
<td>\~\$0.005</td>
<td>no</td>
</tr>
</table>
*Assumptions: 500 output tokens/lead; cache reads at 10% of input; Batch API 50% off; rubric identical across calls. Attack any input and the conclusion holds — caching + batch beats naive batching at every plausible token count.*
Your pipeline is nightly and asynchronous. The Batch API's 24-hour turnaround is free money here.
**Fix:** one lead per call, `cache_control` on the rubric block, submit through the Batch API. Roughly half the cost and the anchoring problem disappears. Consider Haiku for the extraction pass with Sonnet reserved for copy — measure before committing.
---
### P1-8 · You do not know your false-positive rate on pain detection
**Finding:** missing-site, form, CTA, and tool detection are heuristic and affected by JavaScript, bot blocking, timeouts, and stale pages. Those same heuristics carry your heaviest weights — broken form 30, missing estimate form 20, no clear CTA 14.
**The missing number:** what percentage of "broken form" verdicts are actually working forms behind a JS framework?
You cannot fix what you haven't measured, and this number is cheap to get: sample 50 leads per pain category, open them by hand, record the truth. Two hours of work, once, and it tells you whether your highest-weight signal is trustworthy.
**Fix regardless of the number:**
- Any pain claim that will appear in outreach requires `render_status == ok`. No exceptions.
- Timeout, bot-block, or render failure → `UNKNOWN`, never `broken`. Your audit says this is *generally* the behavior; make it unconditional.
- Re-verify the single claim used in the opener at send time, not at crawl time.
**Rail:** if measured FP rate on any pain category exceeds 10%, that category is demoted to ranking-only and may not appear in outreach copy until fixed.
---
### P1-9 · Epoxy silently inherits hardscaping behavior
**Finding:** seven search ICPs, five canonical `VerticalProfile`s. Epoxy falls back to hardscaping.
A garage-floor coating company is not a patio builder. Different ticket, different buyer, different review volume, different seasonality, different pain. Silent fallback means every epoxy lead is scored against the wrong thresholds and nothing anywhere says so.
**Fix:** delete the fallback. A missing profile raises. Then remove the missing-profile condition entirely by adopting composition — `profile = niche_base ⊕ metro_overlay` means no cell can be missing. The attached JSON gives you all 14.
---
### P1-10 · No freshness contract
**Finding:** older enrichment files retain existing evidence rather than recomputing. No recrawl was performed. No TTL is described anywhere.
**Why it matters:** your entire pitch is "I looked at your website and noticed X." If X was true six weeks ago and they fixed it four weeks ago, the email doesn't just fail — it actively marks you as someone running an automated blast.
**Fix:**
<table header-row="true">
<tr>
<td>Fact</td>
<td>TTL</td>
<td>On expiry</td>
</tr>
<tr>
<td>Site DOM facts (form, CTA, platform)</td>
<td>14 days</td>
<td>recrawl</td>
</tr>
<tr>
<td>Rating / review count</td>
<td>30 days</td>
<td>refresh from Places</td>
</tr>
<tr>
<td>Review recency</td>
<td>7 days at send time</td>
<td>refresh</td>
</tr>
<tr>
<td>Screenshots</td>
<td>14 days</td>
<td>recapture</td>
</tr>
<tr>
<td>Suppression status</td>
<td>always</td>
<td>check at send</td>
</tr>
</table>
Plus a **pre-send verification step**: on the morning of the send, re-fetch the one page that supports the one claim in the email. If the claim no longer holds, pull the lead. This is \~1 second per lead and it is the difference between "he actually looked" and "this is a template."
---
### P1-11 · Scored rows don't record how they were scored
**Finding:** `.env` selects the model. Nothing stamps the row.
Model versions change. Prompts change. Six months from now you will look at a batch of low-performing leads and have no way to tell whether they were scored under the old rubric.
**Fix:** every scored row carries `model_id`, `prompt_hash`, `capsule_version`, `profile_version`, `scored_at`. Five columns. Do it now while the table is small.
**And a golden set:** freeze 100 hand-labeled leads. Any change to prompt, model, weights, or capsule reruns them and reports drift. Without this, you cannot safely change the prompt, which means in practice you will stop changing the prompt.
---
## P2 — Structural, next quarter
### P2-12 · Two overlapping gating systems
Score ≥ 65 **and** grade ∈ \{A,B\} **and** \~15 independent gates. The grade band is a function of the score, so `grade ∈ {A,B}` is `score >= 65` written twice. Meanwhile the fifteen gates already encode nearly everything the score encodes.
**Simplify to two concepts:**
- **Eligibility** — binary, deterministic, gates only. May I contact this business?
- **Priority** — continuous, scored. In what order do I contact the eligible ones?
Grades become display labels for priority. They stop being gates. This removes an entire class of "why did this pass one check and fail another" investigation.
---
### P2-13 · No outcome loop — the largest gap in the system
There is no table anywhere in this audit that records what happened after the send.
**Add it. It's one table:**
```plain text
outcomes(lead_id, sent_at, opened, replied, reply_sentiment,
         meeting_booked, closed, revenue, notes)
```
Then, weekly: regress `replied` against the features you already store — pain score, vendor fingerprint, review band, niche, metro, claim template ID, send month. Within about 300 sends you will know which of your seven dimensions predicts a reply and which are decoration. My prediction, stated so you can hold me to it: **vendor fingerprint and claim-template ID will predict reply rate better than the 100-point composite score does.**
This is the difference between a lead scorer and a lead scorer that improves.
---
### P2-14 · Funnel observability
You cannot currently answer "which gate is killing my volume?" Log a rejection counter per gate per batch. You will likely discover that one or two gates account for most of your attrition, and that at least one of them is stricter than you intended.
---
### P2-15 · Thresholds are hand-picked
Your review bands (10/40/125 etc.) are priors. You have the corpus to make them empirical: for each cell, compute p25/p50/p90 of `review_count` across everything you've scraped and set the bands to those percentiles. Same for rating floors — compute the actual median rating per niche rather than applying 4.3 everywhere. A 4.4 detailer is a below-median detailer; a 4.4 roofer is above median. One global floor is wrong in both directions simultaneously.
---
## Target architecture
```plain text
0. HARVEST          Places API → raw record
                    ↓
1. ADMIT            ★ single choke point. Suppression, hard ICP, chain footprint,
   ($0, no crawl)     rating floor, review ceiling, dormancy. Both entry points
                      call this function and only this function.
                    ↓  ~60-70% rejected here, before any compute is spent
2. ENRICH           crawl + render + screenshot. Only survivors.
                    ↓
3. EXTRACT          ★ LLM, 1 lead/call, cached rubric, Batch API.
                      Structured output only: PASS/FAIL/UNKNOWN + evidence IDs.
                      No scores. No prose. No visual claims without visual_audit.
                    ↓
4. SCORE            ★ deterministic v3, composed profile, authoritative.
                      Produces priority, not permission.
                    ↓
5. ELIGIBILITY      binary gates. Produces permission, not priority.
                    ↓
6. COMPOSE          claim template selection + rendering. LLM writes the personal
                      line only; every technical claim comes from a template.
                    ↓
7. PRE-SEND VERIFY  ★ re-fetch the supporting page. Claim still true? Suppression
                      still clear? Else pull.
                    ↓
8. SEND
                    ↓
9. OUTCOMES         ★ record everything. Feed step 4 weekly.
```
★ = does not exist today, or exists in a form that can't act.
---
## Delete list
<table header-row="true">
<tr>
<td>Delete</td>
<td>Why</td>
</tr>
<tr>
<td>Shadow mode for scoring_v3</td>
<td>A scorer that cannot act is a scorer that cannot be validated. Promote it or remove it.</td>
</tr>
<tr>
<td>Grade bands as gates</td>
<td>Redundant with the score threshold. Keep as labels.</td>
</tr>
<tr>
<td>Screenshot paths in the text capsule</td>
<td>All of the confabulation risk, none of the visual information.</td>
</tr>
<tr>
<td>Epoxy → hardscaping fallback</td>
<td>Silent wrong answers are worse than loud failures.</td>
</tr>
<tr>
<td>5-lead batching</td>
<td>Caching does the same job without cross-lead anchoring.</td>
</tr>
<tr>
<td>Free-text pain reasons from the model</td>
<td>Replaced by template selection.</td>
</tr>
<tr>
<td>The second scoring path</td>
<td>One capsule builder, parameterized.</td>
</tr>
<tr>
<td>`.env`-reading inside call paths</td>
<td>The cause of your three failing tests.</td>
</tr>
</table>
## Add list
<table header-row="true">
<tr>
<td>Add</td>
<td>Effort</td>
<td>Buys</td>
</tr>
<tr>
<td>`admit()` choke point + contract tests</td>
<td>1 day</td>
<td>Ends the hard-ICP leak permanently</td>
</tr>
<tr>
<td>Composed 14-cell profiles</td>
<td>½ day (JSON attached)</td>
<td>Kills `missing_exact_profile`</td>
</tr>
<tr>
<td>Claim template library</td>
<td>2 days</td>
<td>Bounded outreach risk, reviewable once</td>
</tr>
<tr>
<td>Provenance columns + golden set</td>
<td>2 days</td>
<td>Makes prompt changes safe</td>
</tr>
<tr>
<td>Prompt caching + Batch API</td>
<td>½ day</td>
<td>\~50% cost reduction</td>
</tr>
<tr>
<td>TTL + pre-send verification</td>
<td>2 days</td>
<td>The claim is true when it lands</td>
</tr>
<tr>
<td>`outcomes` table</td>
<td>½ day</td>
<td>The only path to a scorer that improves</td>
</tr>
<tr>
<td>Vendor fingerprint dimension</td>
<td>1 day</td>
<td>Highest-signal feature you're not using</td>
</tr>
<tr>
<td>Per-gate rejection counters</td>
<td>½ day</td>
<td>Answers "what's killing my volume"</td>
</tr>
</table>
Roughly two focused weeks.
---
## Sequence — triggers, not dates
1. **Now → next send.** All four P0s. No send job runs until the suppression module is green and the contract tests pass.
2. **Trigger: contract tests green.** Ship composed profiles, provenance columns, golden set. Turn on caching + Batch API.
3. **Trigger: golden set stable across two consecutive runs.** Ship claim templates and pre-send verification. Begin measuring pain-detection FP rate.
4. **Trigger: v3 vs LLM agreement measured on 200 labeled leads.** Promote v3 to authoritative. Demote the LLM score to ranking.
5. **Trigger: 300 sends logged in ****`outcomes`****.** First weight recalibration against actual reply rate. Replace assumed review bands with measured percentiles.
6. **Trigger: reply rate stable for 3 consecutive weeks.** Only then add metro #3. Adding metros before this multiplies unvalidated assumptions.
---
## Rails
- **CAN-SPAM applies to your outreach**: accurate headers and subject lines, a valid physical postal address, a working opt-out honored promptly. Ohio has no separate anti-spam statute that changes this, but if you're sending SMS at any point the rules are materially stricter. I'm not a lawyer; if you scale past a few thousand sends a month, have one read your sending setup once.
- Suppression check happens at send time, never only at score time.
- No email may contain a technical claim sourced from an `UNKNOWN` or unrendered signal.
- No lead scored under a `profile_version` older than the current one may be sent without rescoring.
---
## The one honest issue
The gating apparatus is doing emotional work, not commercial work.
Fifteen sequential gates, a seven-dimension rubric, four review bands, nine commercial-case dimensions, and a separate "Manual Review Packet" that can be packet-eligible while explicitly unsafe to contact — this is an enormous amount of machinery in service of feeling certain before pressing send. It is well-built machinery. But you have never measured reply rate against any of it, which means you cannot know whether a single one of those gates improves outcomes or whether the whole apparatus is just an elaborate way of sending fewer emails.
The binding constraint on a business like this is almost never lead quality at the margin. It's sends × reply rate × close rate, and you currently have a measurement for none of the three. The most valuable thing you could do this month is not any of the fixes above — it's send 300 emails from the leads that pass today's gates, log what comes back, and find out which half of this system is load-bearing.
Fix the four P0s, because those can send something false. Then go get the data that tells you what the rest of it is worth.
---
## Journal block
**Prediction (65% confidence):** once outcomes are logged for 300 sends, `vendor_fingerprint` — a feature not currently in your scoring model at all — will correlate more strongly with reply rate than the composite 100-point score does.
**Falsifier:** a fitted logistic regression on ≥300 outcomes in which the composite score's coefficient is significant and vendor fingerprint's is not. If that's the result, the LLM rubric is capturing something real that I've assumed away, and the P1-5 inversion should be reconsidered rather than shipped.

