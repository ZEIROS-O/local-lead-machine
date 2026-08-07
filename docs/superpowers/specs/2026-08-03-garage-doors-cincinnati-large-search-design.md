# Garage Doors Cincinnati Large Search Design

## Objective

Run a fresh, large discovery-and-enrichment batch for garage-door businesses
across Greater Cincinnati and Northern Kentucky, while controlling franchise
noise, redundant search intent, and paid API volume.

## Search profile

The batch uses the existing `garage_doors` vertical and canonical `cincinnati`
market. The query set is limited to eight distinct, high-intent services:

1. garage door repair
2. garage door replacement
3. garage door installation
4. garage door opener repair
5. garage door spring repair
6. emergency garage door repair
7. overhead door repair
8. commercial overhead door

The full Cincinnati market supplies all fifteen configured geo slices, including
the Cincinnati core, Ohio suburbs, Hamilton and Lebanon, and the Northern
Kentucky cities Florence, Covington, and Newport.

## ICP and noise controls

The existing search-time floors remain rating 4.3 and eight reviews, with
missing websites allowed and no review ceiling. Search-time exclusions include
the existing retail, storage, floor-coating, and builder noise plus the named
garage-door networks Precision Garage Door, Precision Door, A1 Garage Door
Service, Overhead Door Company, and Wayne Dalton.

The new replacement term is also added to the canonical garage-door term set so
profile inference, service evidence, boost matching, and later scoring agree
with the generated search query.

## Run budget and outputs

The generated full config is adjusted to two result pages per query/geo. Eight
queries across fifteen geos produce 120 expanded searches and at most 240 Text
Search pages before early termination or API result exhaustion. The live command
passes explicit caps of 120 expanded searches, 150 Place Details calls, and 150
enrichment rows.

The run stops after discovery and enrichment. It writes the normal raw,
deduplicated, enriched, and search-report artifacts. It does not score leads,
generate packets, write Airtable, or send outreach.

## Verification

Tests must demonstrate that generated garage-door configs contain exactly the
eight intended query phrases, include garage door replacement in canonical boost
terms, include every named network in search-time exclusions, retain the 4.3/8
floors, and use all fifteen Cincinnati geos for a full config. After generation,
the config is loaded through `make_search_batch.SearchConfig`, the focused search
and ICP tests run, and the completed batch report is checked against the raw,
deduplicated, and enriched artifact counts.
