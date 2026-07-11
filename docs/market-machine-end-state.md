# End State — Full-Stack Market Intelligence + Lead Machine

*Written 2026-07-05. Companion to [outreach-loop-strategy.md](outreach-loop-strategy.md) (the why)
and [phase-b-c-roadmap.md](phase-b-c-roadmap.md) (the current build order). This doc is the
long-horizon product vision; it does NOT change the plan of record. Gate 0 still applies: nothing
here gets built ahead of first closes.*

---

## The vision

One machine, two faces, shared data:

1. **Market Research face** — input: `niche + geography`. Output: an industry report like the
   reference product (TrueMetrics-style): company/profile counts, market saturation per 100k
   population, avg CPC / LSA lead cost, paid-channel utilization, keyword volumes + rank
   difficulty per city, software/CRM distribution, review distributions, profile-health gaps
   (no website / unclaimed / no hours), growth leaders, seasonality. **Purpose: decide which
   niche × metro to enter before spending outreach effort.**

2. **Lead Machine face** (exists today) — input: chosen `niche + metro`. Output: scored,
   evidence-grounded, sendable lead packets exported to CRM, feeding Loops B/C/D (assets →
   sequencing → feedback).

The faces compound: market research picks the pond; the lead machine fishes it; outreach outcomes
(reply/close rates per niche × metro) flow back into which ponds to pick next. That feedback join
is something the reference product cannot have — it doesn't know what converts, only what exists.

## Differentiation vs. the reference product

Do **not** compete on breadth (their moat = a pre-scraped national index across every industry +
years of longitudinal snapshots). Compete on:

- **Depth on demand**: for one niche × metro, go deeper than they can — full site scrape, leak
  evidence, buyer-intent signals, LLM-graded fit — not just profile metadata.
- **Action, not dashboards**: their terminal state is a CSV export; ours is a sent, personalized
  asset and a closed deal.
- **Outcome-weighted market picks**: saturation/CPC weighted by *our own* reply and close rates.

## Target architecture

```
                     ┌────────────── shared data layer (Postgres/SQLite → Postgres) ──────────────┐
                     │ businesses, profiles, websites, tech_detections, keywords, snapshots(t),   │
                     │ scores, packets, outreach_state, outcomes                                  │
                     └──────────────────────────────────────────────────────────────────────────┘
 Collectors                          Analytics                       Action
 - places_search (exists)            - market report generator       - score_leads (exists)
 - site enricher (exists)            - saturation (census join)      - make_packets (exists)
 - tech fingerprinter (new)          - keyword/CPC tables            - CRM export (exists: Airtable)
 - keyword/CPC via DataForSEO (new)  - profile-health rollups        - outreach loops B/C (roadmap)
 - WHOIS/Wayback site-age (new)      - growth = snapshot deltas (new)
 - snapshot scheduler (new)
                     Frontend (new, last): niche+geo picker → report view → lead table → export
```

## Gap analysis (what exists → what's missing)

| Capability | Reference product | This repo today | Gap size |
|---|---|---|---|
| Business/profile discovery | National, pre-scraped | Per-config Places search w/ pagination | Small (works, just per-metro) |
| Enrichment (site scrape, signals) | Shallow (profile health) | Deep (evidence, buyer intent, email hygiene) | **We're ahead** |
| Lead scoring / fit | None (filters only) | LLM-scored, gated, tiered | **We're ahead** |
| CRM export | CSV export | Airtable upsert w/ dedupe | Parity |
| Keyword volume / CPC / rank difficulty | Yes, per city | None | Medium — buy via DataForSEO API |
| Paid channels (LSA / paid search detection) | Yes | None | Medium — SERP scrape per keyword×city |
| Software/CRM/widget detection | Yes (BuiltWith-style) | None | Small — Wappalyzer-fork fingerprints on pages we already fetch |
| Market saturation / per-100k | Yes | None | Small — Census API join |
| Time-series (review growth, new websites/yr) | Yes (their moat) | None | **Large — requires months of snapshots; start collecting early, cheap** |
| Website established date | Yes | None | Small — WHOIS + Wayback CDX |
| Storage | Warehouse | CSVs per batch | Medium — move to SQLite/Postgres |
| Frontend | Full SaaS UI | CLI | Medium — internal tool is quick; SaaS is not |
| Multi-tenant / auth / billing | Yes | No | Only needed if productizing — skip |

## Build tiers (realistic effort, with Claude Code doing the lifting)

**Tier 1 — Market Report CLI (internal), ~1–2 weeks part-time.**
`make_market_report.py --vertical X --metro Y` reusing the existing pipeline's discovery + enrich
output, adding: DataForSEO keyword/CPC pull, Wappalyzer-fork tech detection on already-fetched
pages, Census saturation, profile-health rollups, WHOIS/Wayback site age. Output: one HTML/MD
report per niche×metro. This answers the actual question ("which pond") at ~5% of the reference
product's build cost. **This is the only tier justified pre-close.**

**Tier 2 — Persistent machine + internal frontend, ~4–8 weeks part-time.**
SQLite→Postgres data layer, snapshot scheduler (weekly re-scrape of tracked niches×metros →
growth/trend charts become possible after 2–3 months of history), FastAPI + simple React/Lovable
frontend: niche+geo picker → report → lead table → CRM export button. Single-user, no auth/billing.

**Tier 3 — Productized SaaS (reference-product competitor), 4–6+ months + real money.**
National pre-scraped index (their 33k profiles/industry × ~100 industries), scraping infra beyond
the Places API (cost/ToS), years of longitudinal data you don't have, plus auth/billing/support.
This is a data-ops *business*, not a feature. **Default: don't.** If market research at breadth is
ever needed, subscribe to the incumbent or query DataForSEO Labs directly.

## Buy-vs-build per data source

| Data | Decision | Source |
|---|---|---|
| Keyword volume / CPC / difficulty | **Buy** | DataForSEO (cheap, per-call) or Semrush API |
| Maps/profile data at current scale | Keep Places API | existing |
| Maps at national scale (if ever) | Buy | Outscraper / SerpAPI (ToS gray — decide then) |
| Tech fingerprints | **Steal (OSS)** | Wappalyzer fingerprint DB forks (e.g. `enthec/webappanalyzer`) |
| Site age | Free | WHOIS + Wayback Machine CDX API |
| Population (saturation) | Free | US Census API |
| Review-growth time series | **Build (start now, cheap)** | our own snapshot table — value compounds with time |

## Sequencing rule

Gate 0 outranks all of this. Order: **closes → Phase B/C (outreach) → Tier 1 report → Tier 2**.
The one exception worth doing early because it's nearly free and time-gated: **begin snapshotting**
(append a dated row per business per pipeline run into a `snapshots` table) so trend data exists
by the time Tier 2 wants it.
