# Setter system — decision sheet

**Status: DRAFT for Lou's sign-off.** T5.2 of the production-ready plan: the ~15 `[TO CONFIRM]`
markers scattered across the setter onboarding material, collected into one sheet so they can be
decided in a single sitting instead of one at a time as each is hit.

Every row has a **recommendation**, because a decision sheet that only asks questions moves nothing.
Where the recommendation is weak or the trade-off is real, that is said outright. Strike through and
overwrite anything you disagree with — the point is one pass, not agreement with the defaults.

---

## A. Tools

| # | Question | Recommendation | Why / trade-off |
|---|---|---|---|
| A1 | **Dialer** | Decide before certification; no recommendation | Genuinely open — depends on whether call recording is browser-based or carrier-based, which drives the consent language in A5. This is the one row that should not be defaulted. |
| A2 | **Booking link** | One shared link per rep, rep-owned calendar | Per-rep links make `Assigned Setter` and the calendar agree without a mapping step. A single shared link needs round-robin logic that duplicates what upload-time assignment already does. |
| A3 | **Team channel** | One channel, all reps + Lou | At three reps, per-rep DMs cost more in context-switching than they save in noise. Revisit at ~6. |
| A4 | **Payment tracker** | Airtable, not a spreadsheet | The comp inputs (`Sales Activities.Outcome`, `Deals.Cash Collected`) already live there. A spreadsheet means a manual copy step between the source of truth and the thing that pays people. |
| A5 | **E-sign** | Any provider; the requirement is the *content* | Contractor agreement must carry **written notice that calls are recorded** before any dial. That is the compliance-relevant part, not the vendor. |

## B. Compensation mechanics

These pair with [`compensation-draft.md`](compensation-draft.md).

| # | Question | Recommendation | Why / trade-off |
|---|---|---|---|
| B1 | **Weekly cutoff** | Sunday 23:59 local, paid the following Friday | A weekend cutoff means the boundary never lands mid-selling-day. The 5-day lag leaves room to reconcile collected cash before it is paid on. |
| B2 | **Payment method** | Whatever clears reliably to a contractor; record it per rep | Not consequential enough to standardize at three reps. |
| B3 | **Clawback window** | 60 days from collection, limited to refunds and chargebacks | A clawback should cover money that reversed, not deals that merely went badly. Unbounded clawback windows are a recruiting liability and are rarely enforced anyway. |
| B4 | **Attribution window** | **90 days** from the held appointment | The plan's own recommendation and a reasonable fit for this sales cycle. Shorter under-pays a setter for a deal they genuinely sourced; much longer creates ambiguity about which of two touches earned it. |
| B5 | **Post-departure attribution** | Honour the 90-day window after departure, for deals already held before the last day | The alternative — attribution stops at departure — quietly encourages a departing rep to stop booking in their final weeks. Cheap to honour at this volume. |
| B6 | **Same business, different location** | One attribution per legal entity, to the first setter who held a qualified appointment with it | Matches how the pipeline already dedupes: `Place Key` is per location, but a multi-location owner is one buyer. Without this, two reps can both legitimately claim the same customer. |

## C. Escalation owners

| # | Question | Recommendation |
|---|---|---|
| C1 | Compliance question (opt-out, recording consent, DNC) | **Lou.** Escalate immediately, do not improvise. |
| C2 | Comp dispute | **Lou** |
| C3 | Technical / data problem | **Lou** |
| C4 | Prospect complaint | **Lou** |

All Lou for now, and that is the correct answer at three reps — but it is the row that stops being
true first. Revisit when a rep is doing it well enough to own C3 and C4.

## D. Runbook

| # | Question | Recommendation | Why / trade-off |
|---|---|---|---|
| D1 | **Minimum weekly capacity** | 15 hours, stated as a commitment rather than a schedule | Contractors set their own hours; a minimum commitment is enforceable, a fixed timetable risks the classification question. |
| D2 | **Fixed voice-sample paragraph** | One fixed paragraph, identical for every candidate | The point of a voice sample is comparison. A candidate-chosen passage measures their material selection, not their delivery. Any neutral paragraph works — fix it once and never change it, or the samples stop being comparable. |
| D3 | **Onboarding site URL** | `https://lpm-onboarding.lovable.app/` | Read-only reference page. Its mobile rendering fix is explicitly **not** on the critical path. |

---

## What this sheet does not decide

- **The eight-point qualification checklist itself.** It lives in the onboarding material and needs
  pasting into [`compensation-draft.md`](compensation-draft.md) before signing — "maintained
  quality" is defined against it.
- **The quality thresholds (80% / 30%).** Placeholders until Michael's first 20 appointments give a
  real baseline. See the compensation draft.
- **Anything requiring a human to sign or provision.** C3 provisioning and C4 certification stay
  human by design: contractor agreement → compensation agreement → W-9 → *then* provision Notion,
  the Airtable view, dialer, booking link, and channel. Nothing is issued before documents execute.
