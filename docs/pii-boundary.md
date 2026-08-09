# PII boundary — what leaves this machine, and to whom

**Status:** current as of 2026-08-08. Written for T4.11 of the production-ready plan, which found
that a grep for `PII`, `data processing`, `third-party processor` and `data residency` across every
`.md` in this repo returned **zero hits** — while scoring was already shipping small-business
contact data to a third-party API by default.

This document is descriptive, not aspirational. It records what the code does today so the choice
of scoring provider is made with the boundary in view. It is not a legal opinion.

---

## The one place data crosses a boundary

Exactly one function builds the payload that leaves this machine for a language model:
`build_llm_scoring_record` in [`score_leads.py:2711`](../score_leads.py). Everything else — search,
enrichment, sendability, packets — runs locally against Google Places and the prospect's own site.

Nothing else in the pipeline makes an outbound call to a model provider. Gate 0 still holds: no
email or SMS SDK, no webhook, no scheduler. The scoring call is the boundary.

## What the payload contains

| Field | Source | Personal data? |
|---|---|---|
| `lead.business_name` | Google Places | Often a person's name in these verticals ("Dave's Mobile Detailing") |
| `lead.address` | Google Places (`google.address`) | **Street address.** For a sole trader this is frequently a home address |
| `lead.phone` | Google Places | **Yes.** Frequently a personal mobile |
| `lead.website_url`, `lead.google_maps_url` | Google Places | Identifiers, not directly personal |
| `screenshots.desktop` / `.mobile` | local enrichment | Path strings only — **image bytes are never sent** |
| `selected_evidence_snippets` | the prospect's own site | **The uncontrolled one — see below** |

### The snippets are the part to actually think about

`selected_evidence_snippets` carries up to `MAX_LLM_SCORING_SNIPPETS = 8` windows of raw page text
(`score_leads.py:379`), each a sentence-scoped window capped at `MAX_LLM_SCORING_SNIPPET_CHARS = 300`
(`:378`, extracted by `_snippet_around` at `:2379`).

Windows are selected by matching `LLM_SCORING_SNIPPET_TERMS` (`:381`). That list is mostly
commercial intent — `ceramic coating`, `booking`, `estimate`, `quote` — but it also includes
**`contact`**, **`call`**, **`text`**, **`review`** and **`testimonial`**. A 300-character sentence
window around the word "contact" on a small-business site routinely contains an owner's full name,
a direct email address, and a mobile number. Around "review" or "testimonial" it routinely contains
a *customer's* name.

So the honest statement is: **the payload contains an unbounded-in-kind sample of whatever personal
data the prospect published on their own site**, not a curated field list. It is drawn from public
web pages, but "public" is not the same as "not personal data" under GDPR or CCPA, and the data
subject may be a third party (a named reviewer) rather than the prospect.

## Which provider receives it

Set by `LLM_SCORING_PROVIDER_CHAIN`; resolution and fallback live in `lead_machine/llm_client.py`.

| Provider | Processor | Where the data goes |
|---|---|---|
| `kimi` | Moonshot AI | Moonshot's API. **This is the repo default and the first entry in `DEFAULT_PROVIDER_CHAIN`** (`llm_client.py:32`) |
| `codex` | OpenAI, via the ChatGPT subscription CLI | OpenAI, under the ChatGPT subscription's terms |
| `claude` | Anthropic, via the Claude subscription CLI | Anthropic, under the subscription's terms |
| `anthropic` | Anthropic | Anthropic Messages API, under the API terms |

**The default chain is kimi-first.** Out of the box, an unmodified run sends every scored lead's
business name, street address, phone number, and up to eight raw snippets of their site text to
Moonshot. That is a deliberate-looking choice that was never actually decided — it is the default
because kimi was the cheapest per-token provider when the chain was written, and the PII question
was not asked at the time.

### Consequence for Phase 3

The Phase 3 decision to score the 1,621-lead backlog through `codex` was taken partly on this basis:
it keeps identifiable small-business data off Moonshot for the bulk run, and costs $0 against an
existing subscription. That reasoning is sound and this document is the record of it.

It does **not** change the default for every other run. Anyone running `run-lead-batch` without
setting `LLM_SCORING_PROVIDER_CHAIN` is still on kimi-first.

## Recommendations, in priority order

1. **Decide the default chain deliberately.** Either reorder `DEFAULT_PROVIDER_CHAIN` away from
   kimi-first, or record here that kimi-first is the accepted choice. Leaving it as an unexamined
   default is the actual problem; either explicit answer is defensible.
2. **Consider dropping `contact` from `LLM_SCORING_SNIPPET_TERMS`.** It is the single highest-yield
   term for personal data and among the lowest-value for scoring — the structured
   `contact_form_detected` / `form_strength` fields already carry the signal that matters, and they
   are booleans rather than raw text. This is a scoring-output change, so it belongs ahead of a
   backlog run, not during one.
3. **Do not add screenshot bytes to the payload.** Only paths are sent today. Sending the images
   would put storefront photos, staff, and vehicle plates across the boundary.
4. **Revisit if a rep-facing or customer-facing surface is ever added.** This document covers the
   scoring path only.

## Explicitly out of scope here

Retention and deletion at each provider (governed by their terms, not by this repo), the Airtable
boundary (a first-party CRM holding the same contact data by design), and the suppression ledger
(local, append-only, and the mechanism that *honours* an opt-out rather than a place data leaks).
