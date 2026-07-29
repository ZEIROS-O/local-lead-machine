# LPM Suggested ICP Specification

> Local snapshot copied from Notion on 2026-07-28T11:22:05.848Z.  
> Source: [LPM Suggested ICP Specification](https://app.notion.com/p/3abc57f0654f8060b4f8ca95f5efe21f)

**Scope:** 7 niches × 2 metros = 14 cells. Cleveland and Columbus, Ohio.  
**Constraint honored throughout:** every predicate below is decidable from public information only — the Google Business Profile, the rendered website DOM, and public social profiles. Nothing requires a phone call, a data purchase, or a judgment call about "vibe."
---
## 0. How this document is meant to be used
### 0.1 The composition rule
```plain text
profile(niche, metro) = niche_base[niche] ⊕ metro_overlay[metro]
```
Niche base carries: identity, money-service terms, exclusions, pain weights, offer angle.  
Metro overlay carries: geography, review thresholds, seasonality, language, housing-stock modifiers.
**There is no such thing as a missing cell.** Today your system has 5 hand-authored exact cells out of 14 and silently falls back to hardscaping for epoxy. Under composition, all 14 exist by construction and a new metro costs one overlay file, not seven.
### 0.2 Three questions, in order
Every lead is evaluated in this sequence. A `FAIL` at any stage stops the pipeline — no crawl, no LLM, no cost.
<table header-row="true">
<tr>
<td>Stage</td>
<td>Question</td>
<td>Cost</td>
<td>Data needed</td>
</tr>
<tr>
<td>**1. Identity**</td>
<td>Is this the right *kind* of business?</td>
<td>\$0</td>
<td>GBP only</td>
</tr>
<tr>
<td>**2. Size**</td>
<td>Is it in the money window — big enough to pay, small enough to care?</td>
<td>\$0</td>
<td>GBP only</td>
</tr>
<tr>
<td>**3. Pain**</td>
<td>Is there a specific, nameable, fixable conversion problem?</td>
<td>crawl</td>
<td>Site DOM</td>
</tr>
</table>
Stage 1 and 2 are pure GBP arithmetic. Run them *before* you crawl. Right now you crawl first and gate later, which is the single largest source of wasted compute in the pipeline.
### 0.3 Ternary logic, always
Every predicate returns `PASS` / `FAIL` / `UNKNOWN`.
- `UNKNOWN` never counts as `PASS`.
- `UNKNOWN` on a *pain* signal means that pain may not appear in the outreach copy. Ever.
- A render failure, a timeout, or a bot-block produces `UNKNOWN`, not `FAIL`. "Their form is broken" is the most damaging false positive you can send, because the prospect can trivially disprove it and will never reply again.
---
## 1. Universal layer — applies to all 14 cells
### 1.1 Field sources and extraction rules
<table header-row="true">
<tr>
<td>Field</td>
<td>Source</td>
<td>Extraction rule</td>
</tr>
<tr>
<td>`gbp_primary_category`</td>
<td>Places `types` / category string</td>
<td>Exact string. Do not infer from name.</td>
</tr>
<tr>
<td>`gbp_name`</td>
<td>Places `displayName`</td>
<td>Used for chain pattern matching</td>
</tr>
<tr>
<td>`rating`</td>
<td>Places `rating`</td>
<td>Float, 1 decimal</td>
</tr>
<tr>
<td>`review_count`</td>
<td>Places `userRatingCount`</td>
<td>Integer</td>
</tr>
<tr>
<td>`review_recency_days`</td>
<td>Reviews API, newest review</td>
<td>Days since most recent review</td>
</tr>
<tr>
<td>`review_velocity_90d`</td>
<td>Review history snapshots</td>
<td>Reviews added in trailing 90 days</td>
</tr>
<tr>
<td>`address_type`</td>
<td>Places `formattedAddress` + `businessStatus`</td>
<td>`storefront` \| `service_area` \| `residential`</td>
</tr>
<tr>
<td>`location_count`</td>
<td>Places brand query + site location selector</td>
<td>Integer</td>
</tr>
<tr>
<td>`website_status`</td>
<td>HTTP + rendered DOM</td>
<td>`live` \| `broken` \| `parked` \| `social_only` \| `none`</td>
</tr>
<tr>
<td>`site_platform`</td>
<td>Generator meta, script hosts, footer</td>
<td>Wix / Squarespace / WP / Duda / GoDaddy / custom / agency</td>
</tr>
<tr>
<td>`vendor_fingerprint`</td>
<td>Script hosts + footer attribution</td>
<td>See §1.4</td>
</tr>
<tr>
<td>`money_terms_hit`</td>
<td>Rendered text, lowercased</td>
<td>Count of niche money-service terms present</td>
</tr>
<tr>
<td>`form_state`</td>
<td>Rendered DOM `<form>` + JS widgets</td>
<td>`absent` \| `present_weak` \| `present_strong` \| `broken` \| `unknown`</td>
</tr>
<tr>
<td>`mobile_cta_state`</td>
<td>Mobile-viewport render, above the fold</td>
<td>`tap_to_call_present` \| `absent` \| `unknown`</td>
</tr>
<tr>
<td>`phone_consistency`</td>
<td>GBP phone vs site phone(s)</td>
<td>`match` \| `mismatch` \| `tracking_number`</td>
</tr>
</table>
### 1.2 Universal hard disqualifiers
Any one of these is an immediate, non-overridable `FAIL`:
1. `businessStatus != OPERATIONAL`
2. On the suppression / DNC / prior-contact list
3. **Chain or franchise** — see the footprint test, §1.3
4. `location_count >= 3` within the metro, or any location outside the state
5. `rating < niche_rating_floor` (§3 — this is niche-specific, not a global 4.3)
6. `review_count > niche_review_ceiling` (§3 — they're past you)
7. GBP primary category is on the niche's `excluded_categories` list
8. `money_terms_hit == 0` **and** `website_status == live` — a live site that never names a high-ticket service is either the wrong business or a business with no ticket worth fixing
9. Zero reviews in the trailing 365 days (dormant, regardless of lifetime count)
### 1.3 The franchise / chain footprint test
Do not maintain a brand blacklist as your primary mechanism — it goes stale and it's where you'll get embarrassing false negatives. Use structural signals, which an LLM can verify from the page in one pass:
<table header-row="true">
<tr>
<td>Signal</td>
<td>Weight</td>
</tr>
<tr>
<td>Site has a location selector: "Find a location", "Select your area", ZIP-gate</td>
<td>2</td>
</tr>
<tr>
<td>Nav or footer contains "Franchise Opportunities" / "Own a Franchise" / "Become an Owner"</td>
<td>2</td>
</tr>
<tr>
<td>GBP name matches `^<Brand> of .+$` or `^<Brand> — <City>$`</td>
<td>1</td>
</tr>
<tr>
<td>Domain is `brand.com/oh/cleveland` rather than an owned domain</td>
<td>2</td>
</tr>
<tr>
<td>Footer copyright holder ≠ local business name</td>
<td>1</td>
</tr>
<tr>
<td>Site has a corporate 1-800 number alongside the local number</td>
<td>1</td>
</tr>
<tr>
<td>"Independently owned and operated" disclaimer</td>
<td>2</td>
</tr>
</table>
**Score ≥ 2 → chain, hard fail. Score 1 → flag ****`chain_uncertain`****, route to manual review, never auto-send.**
Seed brand list per niche is in §2 — treat it as a *fast path*, not the definition. A brand match short-circuits to fail; a brand miss proves nothing.
### 1.4 The vendor fingerprint — your most under-used signal
Who currently handles their marketing changes the entire economics of the lead. This is fully detectable from script hosts and footer text, and it is currently absent from your scoring model.
<table header-row="true">
<tr>
<td>Fingerprint</td>
<td>Detection</td>
<td>Verdict</td>
</tr>
<tr>
<td>**Premium vertical agency** — Scorpion, Blue Corona, Rival Digital, Hook Agency, Footbridge</td>
<td>Footer attribution, agency-hosted assets, distinctive URL structures</td>
<td>**Exclude.** Under contract, professionally optimized, you are the fourth call this month.</td>
</tr>
<tr>
<td>**Budget reseller** — Hibu, Thryv, Townsquare Interactive, Yelp-built, Vivial, LocaliQ</td>
<td>Platform fingerprints, templated layouts, `?utm_source=` on their own links</td>
<td>**Highest-value target.** Paying \$300–600/mo for a template they hate and can cancel.</td>
</tr>
<tr>
<td>**DIY builder** — Wix, GoDaddy Website Builder, Squarespace defaults, Google Sites</td>
<td>`<meta name="generator">`</td>
<td>**High-value target.** Owner built it, knows it's weak, no contract to break.</td>
</tr>
<tr>
<td>**Social-only** — Facebook/Instagram page, no domain</td>
<td>No resolving domain in GBP</td>
<td>**Highest-value target** in detailing and hardscaping. Lowest in plumbing.</td>
</tr>
<tr>
<td>**Active PPC**</td>
<td>CallRail / CallTrackingMetrics / Invoca dynamic number insertion; `gclid` landing pages</td>
<td>**Qualifying signal, not disqualifying.** They already spend money on leads. Their landing page is the pitch.</td>
</tr>
<tr>
<td>**Custom / modern stack** — Next.js, well-structured schema markup, Core Web Vitals passing</td>
<td>Framework fingerprints</td>
<td>**Exclude.** Someone competent owns this.</td>
</tr>
</table>
`vendor_fingerprint` should be a first-class scored dimension worth \~10 points, not a footnote.
### 1.5 The scale window
Too small and they can't pay. Too big and they have a marketing person.
<table header-row="true">
<tr>
<td>Signal</td>
<td>In-window</td>
<td>Out</td>
</tr>
<tr>
<td>Careers page open roles</td>
<td>0–4</td>
<td>10+ → too big</td>
</tr>
<tr>
<td>"Our team" headshots</td>
<td>1–8</td>
<td>15+ → too big</td>
</tr>
<tr>
<td>Fleet photos / truck count</td>
<td>1–6</td>
<td>10+ → too big</td>
</tr>
<tr>
<td>Named marketing/office manager role on site</td>
<td>absent</td>
<td>present → too big</td>
</tr>
<tr>
<td>Service-area page count</td>
<td>0–15</td>
<td>40+ programmatic pages → agency-managed</td>
</tr>
<tr>
<td>Blog with 2024+ cadence, 2+ posts/mo</td>
<td>absent</td>
<td>present → agency-managed</td>
</tr>
</table>
---
## 2. Niche bases
Each niche declares: what it is, what the money job is, who is excluded, what "conversion pain" means *for this trade specifically*, and what you sell.
---
### 2.1 Auto detailing (`detailing`)
**In-ICP:** Owner-operated or ≤6-person detail shops and mobile detailers whose revenue centers on paint correction, ceramic/graphene coating, or paint protection film — not volume washing.
**Money-service terms** (≥1 required, ≥2 preferred):  
`ceramic coating`, `graphene coating`, `paint correction`, `paint protection film`, `PPF`, `clear bra`, `two-step correction`, `one-step polish`, `Xpel`, `SunTek`, `Ceramic Pro`, `Gtechniq`, `Modesta`, `System X`, `Feynlab`, `ozone treatment`, `headliner`, `paint enhancement`
**Ticket anchor:** coating \$800–3,500; PPF \$1,500–7,000. Interior-only and wash packages are \$150–300 and do not qualify the lead alone.
**Excluded GBP categories:** `car wash`, `self service car wash`, `oil change service`, `auto body shop` (unless detailing is a named standalone service line), `tire shop`, `window tinting service` (unless detailing terms also present)
**Excluded patterns:** express tunnel washes, "unlimited wash club", "\$XX full-service wash", touchless/self-serve bays, dealership recon-only operations, franchise networks (Ziebart, Tint World, DetailXPerts, Spiffy, Zips, Take 5 — verify via §1.3).
**Pain weights** (this niche buys *booking*, not brochures):
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>Social-only, no site</td>
<td>40</td>
</tr>
<tr>
<td>No online booking of any kind (no Calendly/Squire/Urable/Booksy/Housecall widget)</td>
<td>30</td>
</tr>
<tr>
<td>Instagram is the real business, site is a dead brochure (IG posting \< 14 days, site copyright ≤ last year)</td>
<td>28</td>
</tr>
<tr>
<td>No before/after gallery, or gallery is stock imagery</td>
<td>22</td>
</tr>
<tr>
<td>No price ranges or packages published anywhere</td>
<td>20</td>
</tr>
<tr>
<td>No mobile tap-to-call above fold</td>
<td>16</td>
</tr>
<tr>
<td>Contact = DM only</td>
<td>25</td>
</tr>
<tr>
<td>Broken/absent quote form</td>
<td>18</td>
</tr>
</table>
**Offer angle:** a booking page that takes a deposit. Detailers lose money to no-shows and quote-chasing; the mockup should show a package selector → date → card on file.
**Detailing-specific rating note:** the median detailer sits at 4.8–4.9. A 4.4 detailer is a *below-median* operator. Rating floor is 4.6, not 4.3.
---
### 2.2 Epoxy / concrete coatings (`epoxy`)
**In-ICP:** Residential garage, basement, and patio coating installers running 1–3 crews. Polyaspartic/polyurea one-day systems or full epoxy flake builds.
**Money-service terms:** `polyaspartic`, `polyurea`, `epoxy garage floor`, `garage floor coating`, `concrete coating`, `flake floor`, `full flake`, `one day garage floor`, `1-day install`, `diamond grinding`, `moisture test`, `basement floor coating`, `pool deck coating`, `metallic epoxy`, `lifetime warranty`, `chip system`
**Ticket anchor:** 2-car garage \$2,500–5,000; 3-car \$4,000–8,000; basement/commercial higher.
**Excluded categories:** `flooring store`, `carpet store`, `tile contractor`, `general contractor` (unless coatings are a named service line)
**Critical exclusion — search contamination:** the query "epoxy" returns resin art, epoxy river tables, countertop resin, and marine epoxy suppliers. Require at least one *floor* term (`floor`, `garage`, `basement`, `concrete`, `slab`, `patio`) co-occurring with the epoxy term. Without this rule your epoxy searches carry meaningful junk.
**Excluded brands (verify via §1.3):** Garage Force, GarageExperts, PremierGarage, Garage Living, Concrete Craft, Slide-Lok, national Penntek/Graniteworx dealer networks. Large local incumbents with 10+ crews (Nature Stone in Cleveland is the obvious one) fail the scale window, not the franchise test.
**Pain weights** (this niche buys *proof and quoting*):
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>No site / social-only</td>
<td>38</td>
</tr>
<tr>
<td>No project gallery, or gallery is manufacturer stock photos</td>
<td>30</td>
</tr>
<tr>
<td>No quote form — phone only</td>
<td>26</td>
</tr>
<tr>
<td>No square-footage or "starting at" pricing anywhere</td>
<td>22</td>
</tr>
<tr>
<td>No financing mention (ticket is \$4k+; financing lifts close rate)</td>
<td>18</td>
</tr>
<tr>
<td>No warranty terms stated</td>
<td>14</td>
</tr>
<tr>
<td>Gallery exists but no local landmarks/addresses — unverifiable</td>
<td>12</td>
</tr>
</table>
**Offer angle:** a quote flow that captures garage size + condition photos, and a real local gallery. Epoxy buyers are comparison shoppers with three quotes on the counter; the one with a visualized quote wins.
---
### 2.3 Garage doors (`garage_doors`)
**In-ICP:** Independent residential garage door service and install companies, 1–8 trucks. Repair volume funds the business; door replacement is the margin.
**Money-service terms:** `garage door installation`, `new garage door`, `door replacement`, `Clopay`, `Amarr`, `C.H.I.`, `Wayne Dalton`, `Haas`, `Raynor`, `insulated door`, `carriage house door`, `torsion spring`, `broken spring`, `opener installation`, `LiftMaster`, `Chamberlain`, `same day service`, `emergency garage door`
**Ticket anchor:** repair \$200–600; opener \$500–900; single door install \$1,200–2,500; double/insulated \$2,500–4,500.
**Excluded categories:** `door supplier` (retail only), `general contractor`, `storage`, `garage builder`, `gate operator` commercial-only
**Excluded brands (verify via §1.3):** Precision Door Service, "Overhead Door Company of ___" distributors, 1-800-GARAGE-DOOR, Neighborly-family brands, and any operator running a national call center.
**Pain weights** (this niche buys *speed of response*):
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>No site / social-only</td>
<td>36</td>
</tr>
<tr>
<td>No mobile tap-to-call above the fold</td>
<td>30</td>
</tr>
<tr>
<td>No same-day / emergency language on the homepage</td>
<td>26</td>
</tr>
<tr>
<td>No online scheduling</td>
<td>22</td>
</tr>
<tr>
<td>No door product visualizer or style gallery</td>
<td>18</td>
</tr>
<tr>
<td>Phone mismatch between GBP and site</td>
<td>16</td>
</tr>
<tr>
<td>No service-area page for the suburbs they actually serve</td>
<td>14</td>
</tr>
</table>
**Offer angle:** a mobile-first "broken spring? we're there today" page with a two-tap booking flow. 70%+ of this niche's searches happen on a phone, in a driveway, next to a door that won't open.
---
### 2.4 Hardscaping (`hardscaping`)
**In-ICP:** Design-and-build hardscape contractors — paver patios, retaining walls, outdoor living. **Split this from landscaping.** Mow-and-blow maintenance is a different business with a different ticket and does not belong in the same profile.
**Money-service terms:** `paver patio`, `retaining wall`, `outdoor living`, `outdoor kitchen`, `fire pit`, `fire feature`, `pergola`, `Unilock`, `Techo-Bloc`, `Belgard`, `EP Henry`, `Rosetta`, `segmental retaining wall`, `natural stone`, `flagstone`, `travertine`, `pool deck`, `3D design`, `paver sealing`, `outdoor lighting`
**Ticket anchor:** patio \$12,000–35,000; retaining wall \$8,000–40,000; full outdoor living \$45,000–120,000.
**Excluded categories:** `lawn care service`, `tree service`, `snow removal service`, `irrigation`, `landscape architect` (design-only, no install revenue), `garden center`
**Excluded patterns:** mowing/fertilization only, TruGreen / Weed Man / Lawn Doctor / The Grounds Guys / U.S. Lawns, commercial grounds maintenance (BrightView), snow-only seasonal operators.
**Pain weights** (this niche buys *credibility at a \$25k price point*):
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>No site / social-only despite \$20k+ tickets</td>
<td>40</td>
</tr>
<tr>
<td>No project portfolio with real completed jobs</td>
<td>34</td>
</tr>
<tr>
<td>No design process explained (consult → 3D → install)</td>
<td>24</td>
</tr>
<tr>
<td>No lead form — phone/email only at a \$25k ticket</td>
<td>26</td>
</tr>
<tr>
<td>No financing option</td>
<td>18</td>
</tr>
<tr>
<td>No booking-window language ("now booking spring 2027")</td>
<td>16</td>
</tr>
<tr>
<td>No material brand affiliations (Unilock/Techo authorized)</td>
<td>14</td>
</tr>
</table>
**Offer angle:** a portfolio-led consultation request that pre-qualifies budget. Hardscapers waste their summer driving to \$6k tire-kickers; the mockup should show budget-band selection before the calendar opens.
**Volume note:** hardscapers do 15–40 jobs a year. Their review counts are structurally low — do not apply plumbing thresholds. §3 handles this.
---
### 2.5 Plumbing (`plumbing`)
**In-ICP:** Independent residential service plumbers, 1–8 trucks, doing both demand service and planned replacement work.
**Money-service terms:** `sewer line replacement`, `trenchless`, `pipe lining`, `hydro jetting`, `camera inspection`, `sewer scope`, `repipe`, `water heater installation`, `tankless`, `sump pump`, `battery backup sump`, `backflow`, `main water line`, `excavation`, `basement waterproofing`, `24/7 emergency`
**Ticket anchor:** service call \$200–500; water heater \$1,800–4,500; sump + backup \$1,200–3,000; sewer line \$4,000–15,000.
**Excluded categories:** `hvac contractor` where plumbing is a sub-line, `general contractor`, `handyman`, `plumbing supply store`, `septic` rural-only, new-construction-only plumbers (they have zero consumer demand-gen need — check for "builders" / "multifamily" / "new construction" as the dominant framing)
**Excluded brands (verify via §1.3):** Roto-Rooter, Mr. Rooter, Benjamin Franklin Plumbing, Rescue Rooter, ARS, Zoom Drain, bluefrog, Len The Plumber, and PE-rollup multi-trade platforms.
**Pain weights** (this niche buys *after-hours capture*):
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>No site / social-only</td>
<td>34</td>
</tr>
<tr>
<td>No online scheduling</td>
<td>28</td>
</tr>
<tr>
<td>No emergency/after-hours banner or number</td>
<td>26</td>
</tr>
<tr>
<td>No mobile tap-to-call above fold</td>
<td>24</td>
</tr>
<tr>
<td>No service-area pages for their real suburbs</td>
<td>18</td>
</tr>
<tr>
<td>No financing on \$5k+ jobs (sewer/repipe)</td>
<td>16</td>
</tr>
<tr>
<td>No membership/maintenance plan offer</td>
<td>14</td>
</tr>
<tr>
<td>Broken or weak contact form</td>
<td>18</td>
</tr>
</table>
**Offer angle:** a page that converts the 9pm burst-pipe search. Plumbers know their after-hours calls go to voicemail; a booking flow that captures the job and the address while they sleep is a number they can price.
---
### 2.6 Roofing (`roofing`)
**In-ICP:** Independent residential roofing contractors, locally owned, 1–5 crews, replacement-focused.
**Money-service terms:** `roof replacement`, `tear off`, `full roof`, `GAF Master Elite`, `Owens Corning Preferred`, `CertainTeed SELECT`, `storm damage`, `hail damage`, `insurance claim`, `free inspection`, `ice dam`, `standing seam`, `metal roof`, `financing available`, `architectural shingle`
**Ticket anchor:** replacement \$12,000–30,000; metal \$25,000–60,000.
**Excluded categories:** `roofing supply store`, `gutter cleaning service` only, `general contractor` where roofing isn't named, commercial/TPO-only, `siding contractor` only
**Excluded patterns — storm chasers:** GBP created \< 12 months ago **and** address is a virtual office / UPS Store / coworking suite **and** no photos of local completed work → hard fail. This is the highest-fraud niche in your set and the one where a bad send damages your name locally.
**Excluded brands:** LeafFilter / Leaf Home, Erie Home, West Shore Home, Power Home Remodeling, Renewal by Andersen, and any operator advertising in 5+ states.
**Manufacturer certifications are a *positive* signal, not a chain marker.** GAF Master Elite and OC Platinum indicate ticket size and durability. Do not confuse certification with franchise.
**Pain weights:**
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>No site / social-only</td>
<td>36</td>
</tr>
<tr>
<td>No inspection/estimate request form</td>
<td>30</td>
</tr>
<tr>
<td>No financing mention at a \$18k ticket</td>
<td>24</td>
</tr>
<tr>
<td>No insurance-claim assistance language</td>
<td>22</td>
</tr>
<tr>
<td>No completed-project gallery with local homes</td>
<td>22</td>
</tr>
<tr>
<td>No license/insurance/bonding proof</td>
<td>18</td>
</tr>
<tr>
<td>No storm-response page or seasonal urgency</td>
<td>14</td>
</tr>
</table>
**Offer angle:** a free-inspection booking flow with address capture and a photo upload. Roofers live and die on inspection volume in the 6 weeks after a hail event.
---
### 2.7 Water damage restoration (`water_restoration`)
**In-ICP:** Independent, non-franchised mitigation contractors doing water, sewage, and mold work with IICRC-certified techs and insurance billing.
**Money-service terms:** `water damage restoration`, `flood cleanup`, `basement flooding`, `sewage cleanup`, `mold remediation`, `structural drying`, `IICRC`, `24/7 emergency response`, `we bill your insurance`, `direct insurance billing`, `Xactimate`, `fire and smoke damage`, `dehumidification`, `moisture mapping`, `emergency water extraction`
**Ticket anchor:** mitigation \$3,000–25,000, largely insurance-paid.
**Excluded categories:** `carpet cleaning service` with incidental water work, mold **testing/inspection** only (inspectors have no remediation revenue and a conflict-of-interest rule against doing both), `plumber` doing incidental cleanup, `general contractor` reconstruction-only
**Excluded brands — this niche is the most franchised in your set:** SERVPRO, ServiceMaster Restore, Paul Davis, PuroClean, Restoration 1, 911 Restoration, Rainbow International, BELFOR, AdvantaClean, Steamatic, DKI, Delta Restoration. Individually-owned SERVPRO franchises still fail — corporate owns the marketing.
**Pain weights** (this niche does **not** buy forms — it buys the 2am phone call):
<table header-row="true">
<tr>
<td>Signal</td>
<td>Points</td>
</tr>
<tr>
<td>No site / social-only</td>
<td>34</td>
</tr>
<tr>
<td>No 24/7 language above the fold</td>
<td>32</td>
</tr>
<tr>
<td>No mobile tap-to-call above the fold</td>
<td>30</td>
</tr>
<tr>
<td>No insurance-billing language</td>
<td>26</td>
</tr>
<tr>
<td>No response-time promise ("on site in 60 minutes")</td>
<td>24</td>
</tr>
<tr>
<td>No IICRC/certification proof</td>
<td>18</td>
</tr>
<tr>
<td>Contact form as the *primary* CTA — actively wrong for this trade</td>
<td>20</td>
</tr>
<tr>
<td>No emergency-vs-scheduled path distinction</td>
<td>14</td>
</tr>
</table>
**Offer angle:** a mobile emergency page that ranks and converts panic traffic. A form-first restoration site is losing the job to whoever answers the phone. That's an easy, provable, non-insulting thing to say in an opener.
**Note on your current config:** water restoration is the one niche where "missing estimate form = 20 points" is *backwards*. Your generic evidence weights penalize the right behavior here. This is exactly why pain weights must live in the niche profile, not in shared `evidence.py`.
---
## 3. The 14-cell matrix
### 3.1 Metro overlays
**Cleveland (****`cle`****)**
- Counties: Cuyahoga, Lake, Geauga, Lorain, Medina, Summit, Portage
- Tier-A suburbs (highest ticket): Beachwood, Pepper Pike, Hunting Valley, Gates Mills, Moreland Hills, Chagrin Falls, Shaker Heights, Bratenahl, Bay Village, Rocky River, Westlake, Avon Lake, Solon, Hudson, Bath, Aurora, Brecksville
- Tier-B: Strongsville, Broadview Heights, North Royalton, Mentor, Willoughby, Medina, Avon, North Olmsted, Twinsburg, Parma Heights
- Language markers: "East Side", "West Side", "Chagrin Valley", "Northeast Ohio", area codes 216 / 440 / 330
- **Housing-stock modifier:** older stock, deep basements, mature trees, hillside lots. Structurally raises demand for plumbing (cast iron / clay laterals), water restoration (basement flooding), retaining walls, ice-dam roofing. Lowers epoxy demand (smaller detached garages).
- **Market maturity:** slower growth, longer-tenured businesses, lower review velocity. Thresholds run lower.
**Columbus (****`cmh`****)**
- Counties: Franklin, Delaware, Union, Licking, Fairfield, Madison, Pickaway
- Tier-A suburbs: Dublin, New Albany, Powell, Upper Arlington, Bexley, Worthington, Granville, Lewis Center, Grandview Heights, Muirfield
- Tier-B: Westerville, Hilliard, Gahanna, Delaware, Pickerington, Canal Winchester, Marysville, Johnstown, Sunbury
- Language markers: "Central Ohio", area codes 614 / 380, "north side", "the 270 loop"
- **Housing-stock modifier:** newer stock, 3-car attached garages, new subdivisions. Structurally raises epoxy, new-build hardscape, and luxury detailing demand. Lowers ice-dam and clay-lateral work.
- **Market maturity:** faster growth, higher business formation, higher review velocity. Thresholds run 20–25% higher than Cleveland — which is what your existing five cells already encode, correctly.
### 3.2 Thresholds
Review bands are `some / established / mature`. Rating floors are niche-specific because rating distributions differ by trade — a 4.5 roofer is above median; a 4.5 detailer is below it.
<table header-row="true">
<tr>
<td>Cell</td>
<td>some / established / mature</td>
<td>Rating floor</td>
<td>Review ceiling</td>
<td>Pain recency (days)</td>
<td>Peak outreach window (OH)</td>
</tr>
<tr>
<td>Detailing × CLE</td>
<td>10 / 40 / 125</td>
<td>4.6</td>
<td>400</td>
<td>180</td>
<td>Feb–May, Sep–Oct</td>
</tr>
<tr>
<td>Detailing × CMH</td>
<td>12 / 50 / 150</td>
<td>4.6</td>
<td>450</td>
<td>180</td>
<td>Feb–May, Sep–Oct</td>
</tr>
<tr>
<td>Epoxy × CLE</td>
<td>6 / 25 / 80</td>
<td>4.5</td>
<td>300</td>
<td>240</td>
<td>Feb–Apr, Aug–Oct</td>
</tr>
<tr>
<td>Epoxy × CMH</td>
<td>8 / 30 / 100</td>
<td>4.5</td>
<td>350</td>
<td>240</td>
<td>Feb–Apr, Aug–Oct</td>
</tr>
<tr>
<td>Garage doors × CLE</td>
<td>10 / 40 / 150</td>
<td>4.4</td>
<td>800</td>
<td>150</td>
<td>Aug–Oct, Mar–Apr</td>
</tr>
<tr>
<td>Garage doors × CMH</td>
<td>12 / 50 / 175</td>
<td>4.4</td>
<td>900</td>
<td>150</td>
<td>Aug–Oct, Mar–Apr</td>
</tr>
<tr>
<td>Hardscaping × CLE</td>
<td>5 / 20 / 70</td>
<td>4.5</td>
<td>250</td>
<td>270</td>
<td>**Dec–Feb**, Jul–Aug</td>
</tr>
<tr>
<td>Hardscaping × CMH</td>
<td>6 / 25 / 85</td>
<td>4.5</td>
<td>300</td>
<td>270</td>
<td>**Dec–Feb**, Jul–Aug</td>
</tr>
<tr>
<td>Plumbing × CLE</td>
<td>5 / 30 / 100</td>
<td>4.4</td>
<td>600</td>
<td>150</td>
<td>Sep–Nov, Feb–Mar</td>
</tr>
<tr>
<td>Plumbing × CMH</td>
<td>6 / 35 / 120</td>
<td>4.4</td>
<td>700</td>
<td>150</td>
<td>Sep–Nov, Feb–Mar</td>
</tr>
<tr>
<td>Roofing × CLE</td>
<td>8 / 30 / 110</td>
<td>4.4</td>
<td>500</td>
<td>180</td>
<td>Jan–Mar, post-storm</td>
</tr>
<tr>
<td>Roofing × CMH</td>
<td>10 / 40 / 135</td>
<td>4.4</td>
<td>550</td>
<td>180</td>
<td>Jan–Mar, post-storm</td>
</tr>
<tr>
<td>Water restoration × CLE</td>
<td>8 / 30 / 100</td>
<td>4.3</td>
<td>400</td>
<td>120</td>
<td>Sep–Nov, Feb–Mar</td>
</tr>
<tr>
<td>Water restoration × CMH</td>
<td>10 / 35 / 120</td>
<td>4.3</td>
<td>450</td>
<td>120</td>
<td>Sep–Nov, Feb–Mar</td>
</tr>
</table>
**The three preserved cells** (Detailing×CLE, Detailing×CMH, Plumbing×CLE, WR×CLE, WR×CMH) match your existing v3 config exactly. The nine new cells are derived, not measured — see the warning below.
> **Label these as ****`assumed`****, not ****`measured`****.** Every threshold in the nine new rows is a prior derived from ticket size, job frequency, and metro velocity. Replace them within 60 days: pull every business you've already scraped in a cell, compute the p25/p50/p90 of `review_count`, and set the bands to those percentiles. You are sitting on the data that makes this empirical; hand-picked thresholds are the thing you should be embarrassed about in Q4, not the thing you defend.
**Hardscaping's outreach window is the counterintuitive one.** Do not sell a hardscaper a website in June — they're buried and their phone already rings. Sell in December and January, when the crews are idle, the owner is doing paperwork, and the spring calendar is empty. Same logic drives roofing into Jan–Mar.
### 3.3 Visible-pain minimums by review band
Replaces the global 10 / 16 / 30 / 40 ladder. Larger businesses must show *more* pain to justify contact, because they have more ways to be fine without you.
<table header-row="true">
<tr>
<td>Band</td>
<td>Minimum pain score</td>
<td>Rationale</td>
</tr>
<tr>
<td>Below `some`</td>
<td>—</td>
<td>Fail. Insufficient demand proof.</td>
</tr>
<tr>
<td>`some` → `established`</td>
<td>12</td>
<td>Small operator; even modest friction is worth fixing</td>
</tr>
<tr>
<td>`established` → `mature`</td>
<td>20</td>
<td>Working business; needs a real gap</td>
</tr>
<tr>
<td>`mature` and above</td>
<td>34 (30 if `vendor_fingerprint ∈ {budget_reseller, diy_builder, social_only}`)</td>
<td>Established; only a serious gap or a beatable incumbent justifies the send</td>
</tr>
</table>
---
## 4. The LLM-facing checklist
This is the form the model actually receives — one lead, one call, structured output only.
```plain text
Given ONLY the Google Business Profile fields and rendered website text below,
answer each item. Use exactly PASS, FAIL, or UNKNOWN. Cite the evidence_id
that supports each non-UNKNOWN answer. Do not infer. Do not be charitable.

IDENTITY
  I1. GBP primary category is in {allowed_categories}?
  I2. At least one money_service_term appears in the site text?  [list which]
  I3. Chain footprint score < 2?  [list which signals fired]
  I4. Single location within the metro?
  I5. Not in {excluded_patterns}?

SIZE
  S1. rating >= {rating_floor}?
  S2. {some} <= review_count <= {review_ceiling}?
  S3. Most recent review within {pain_recency_days} days?
  S4. Scale-window signals all in range?  [§1.5]
  S5. vendor_fingerprint = ?  [one of: premium_agency, budget_reseller,
      diy_builder, social_only, custom, none_detected]

PAIN — answer only from the RENDERED DOM. If render_status != ok, answer UNKNOWN.
  P1. form_state = ?
  P2. mobile_cta_state = ?
  P3. [niche-specific pain checks, injected from the profile]

OUTPUT
  A single named conversion problem, in plain language, that (a) is supported by
  >= 2 evidence_ids, (b) uses no visual claim unless a visual_audit field is
  present, and (c) would still be true if the owner opened their own site right now.
  If no such problem exists, output: NO_DEFENSIBLE_PAIN.
```
**Two rules that matter more than the checklist:**
1. **One lead per call.** You currently batch five. Leads in the same call anchor on each other — the model calibrates the third lead against the first two rather than against the rubric. Any batching gain is paid for in score drift you can't see.
2. **The model never writes the claim; it selects the claim.** Give it a numbered library of pre-approved claim templates per pain type and have it return an index plus the evidence IDs. Free-text pain reasons are how a heuristic false positive becomes an email that says something demonstrably untrue about someone's business.

