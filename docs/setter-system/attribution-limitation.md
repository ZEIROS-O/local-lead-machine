# Known limitation: commission attribution follows current assignment, not history

**Status: known and accepted at three reps.** T5.3 of the production-ready plan. This is a
compensation-correctness note, not a bug report — the behaviour is understood, the blast radius is
bounded, and the fix is small but not free.

---

## The defect

`Sales Activities` carries a **`Setter at Activity`** field. Verified 2026-08-08: a grep for that
string across every `.py` in this repository returns **zero hits**. Nothing writes it. It is an
empty column.

With no per-activity setter stamp, the only record of who owns a prospect is
`Prospects.Assigned Setter` — which is *current* state, not history. So the 5% revenue share follows
whoever is assigned **at the moment the deal is read**, not whoever actually held the appointment
that produced it.

## How it pays the wrong rep

1. Setter A is assigned a prospect and holds a qualified appointment. A has earned the 5%.
2. The prospect is reassigned to Setter B — a coverage gap, a territory change, A leaves, anything.
3. The deal closes and cash is collected.
4. `Assigned Setter` now reads **B**. The 5% is calculated against B.

Nothing in the data disagrees. There is no surviving record that A ever held the appointment in an
attribution-bearing form, so the error is undetectable after the fact from the CRM alone — someone
has to remember.

Note this interacts directly with two decision-sheet rows: **B5 (post-departure attribution)** is
the case where this misfires most predictably, because a departing rep's prospects get reassigned by
definition. Honouring B5 while attribution follows current assignment means the promise and the
mechanism disagree.

## Why it is acceptable right now

The defect requires a **reassignment between the held appointment and the close**. Today:

- Three reps, no reassignments have happened.
- Uploads never PATCH `Assigned Setter` on an existing record, so the pipeline cannot silently
  reassign — only a human can, in the Airtable UI (`docs/setter-assignment-sop.md`).
- Assignment is deterministic and create-only, so re-running an upload does not churn ownership.

So the trigger is a manual action that nobody has taken. It is a real defect with a currently-empty
blast radius.

## The fix

Stamp the setter onto the activity row at the moment the activity is written: set
`Sales Activities.Setter at Activity` from the prospect's `Assigned Setter` as it reads *then*, and
calculate commission from that stamp rather than from the prospect's current assignment.

Once stamped, the record is immutable history and a later reassignment cannot rewrite it. This is
also the only version of the fix that works retroactively-safely: existing activity rows cannot be
back-stamped accurately, so the stamp starts being trustworthy from the day it ships, and anything
before that stays a manual determination.

## Do this before it bites

**Trigger to fix:** the first time a prospect with a held appointment is reassigned. Not "when
convenient" — that event is the moment the data stops being able to answer the question, and it will
not announce itself.

Until then, the operational mitigation is procedural and worth stating to reps directly: **if you
reassign a prospect that already has a held appointment, note it.** A one-line record beats
reconstructing it from memory during a comp dispute.

## Related gap

The same audit found that nothing reads **`Deals.Cash Collected`** either — zero `.py` hits. So the
5% has no automated calculation path at all today, only a manual read. That is a separate issue from
*which rep* gets paid, but the two compound: a manual figure paid to a rep chosen by stale state has
no audit trail on either side. See [`compensation-draft.md`](compensation-draft.md) → *Open
dependencies*.
