# Setter compensation — draft for review

**Status: DRAFT. Not signed, not issued, not published to Notion.** T5.1 of the production-ready
plan. This replaces the earlier drafted `$200 held / $500 close` structure. Binding numbers for any
individual rep live in that rep's separately signed compensation agreement, which governs wherever
it differs from this page.

---

## The structure

**1. Per qualified held appointment**

- **$75** per qualified held appointment.
- **$100** per qualified held appointment from the 11th onward, provided quality is maintained
  (defined below).
- The rate step is permanent once earned, not a rolling window. A rep who reaches 10 qualified held
  appointments stays at $100 unless the quality standard stops being met — see *Rate step-down*.

**2. Revenue share**

- **5% of initial cash collected**, read from `Deals.Cash Collected`.
- Paid **on cash collected, never on signature.** A signed agreement with nothing collected pays
  nothing. If collection happens in instalments, 5% is paid on each instalment as it lands, against
  the *initial* engagement only — not on renewals, expansions, or subsequent projects.
- If a deal is refunded or charged back, the corresponding share is recovered — see *Clawback* in
  the [decision sheet](decision-sheet.md).

## What "qualified held appointment" means

Both halves are required. An appointment that is held but not qualified pays nothing, and so does a
qualified appointment that no-shows.

- **Held** — `Sales Activities.Outcome = Held`. The prospect attended and the call took place.
- **Qualified** — the appointment satisfies the eight-point qualification checklist in force at the
  time of booking.

> ⚠️ **Gap to close before this page is issued.** The eight-point qualification checklist is not in
> this repository — it lives in the setter onboarding material. This draft references it rather than
> restating it, deliberately, so the two cannot drift. **Paste the eight points in here before
> signing**, because "maintained quality" below is defined against them and a rep cannot be held to
> a standard that is only described by reference.

## What "maintained quality" means

The plan's instruction was to make this concrete rather than leave it to judgment, because "quality"
as a bare word is where comp disputes start. Proposed definition:

> Across the rep's **most recent 20 booked appointments**, at least **80% were qualified** (16 of
> 20) and the **no-show rate was at or below 30%**.

Both thresholds are measured on booked appointments, not on dials — a rep is accountable for who
they put on the calendar, not for how many people declined to book.

Rationale for these numbers, so they can be argued with rather than merely accepted:

- **A trailing window, not lifetime.** Lifetime averages let a strong first month carry a weak third
  one, which is the opposite of what a quality standard is for.
- **20 appointments** is long enough that one bad prospect does not move it and short enough that a
  rep can correct within a couple of weeks.
- **80% / 30%** are placeholders pending real data. There is currently no baseline: no rep has run
  the full funnel yet. **Set these from Michael's first 20 before applying them to reps #2 and #3** —
  imposing an unmeasured threshold on the first rep is unfair, and imposing the first rep's numbers
  on later reps without checking they are achievable is worse.

### Rate step-down

If a rep at the $100 rate drops below either threshold, the rate returns to $75 for appointments
booked after written notice, and returns to $100 after the next 20 booked appointments clear both
thresholds. Notice is given before the rate changes, never retroactively — a rep should never
discover a rate change on a payment statement.

## Why Michael's arrangement needs no change here

Michael's 50/50 arrangement differs from this structure and does not need this page rewritten. The
page already states that binding numbers live in each rep's separately signed agreement, and that
agreement governs where it differs. That is precisely what makes this page scale to rep #4 without
forking it per rep.

## Open dependencies — read before issuing

Two mechanical gaps, both verified against the code on 2026-08-08:

1. **Nothing reads `Deals.Cash Collected`.** A grep for it across every `.py` in the repo returns
   zero hits. The 5% is a manual calculation today. That is workable at three reps, but it means the
   figure a rep is paid on is whatever a human read off the record, with no audit trail.
2. **Nothing writes `Sales Activities.Setter at Activity`.** Also zero hits. This is the attribution
   defect — see [`attribution-limitation.md`](attribution-limitation.md). It determines *which rep*
   the 5% is paid to, so it is a compensation issue, not just a data-hygiene one.

Neither blocks signing. Both should be stated to the rep as current limitations rather than
discovered later.
