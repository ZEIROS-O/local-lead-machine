# Setter assignment SOP

Setter assignment is create-only. The MVP outreach-channel decision is phone-only; this code does
not send or dial anything.

## Before enabling it

1. In Airtable, add `Prospects.Assigned Setter` as a single select with one exact choice per active
   rep.
2. Add `Do Not Contact` and `Booked` to `Prospects.Status`, add `Held` to
   `Sales Activities.Outcome`, and add `Deals.Cash Collected` as a currency field.
3. Set the ordered roster in `.env`, for example:

   ```dotenv
   LPM_SETTER_ROSTER=Michael,Setter Two,Setter Three
   ```

4. Run `.venv/bin/python airtable_required_choices.py check`. It must exit 0 before any live
   upload or backfill.

The roster order is part of the assignment function. New prospects use
`sha256(Place Key) % len(roster)`. A record with no Place Key is hashed on `phone:<normalized
phone>` instead — the same key the uploader already upserts it on — so the phone-keyed rows the
pipeline has always accepted get an assignment rather than aborting the run. A record with neither
identifier cannot be upserted at all and fails earlier, by business name.

Re-running a CSV or JSONL upload produces the same assignment, and uploads never PATCH
`Assigned Setter` on an existing record. Changing the roster affects only new blank assignments;
existing rows are not rebalanced and manual reassignment always wins.

SHA-256 modulo is stable, not an exact balancing algorithm. Always inspect the reported per-rep
counts for the real cohort instead of assuming they differ by at most one.

## Backfill existing blank rows

Dry-run first:

```bash
.venv/bin/python backfill_setter_assignments.py --dry-run
```

The command refuses the entire plan if duplicate Place Keys exist — two rows claiming one identity
means the base is wrong and either assignment would be a guess. An unassigned row with neither a
Place Key nor a phone is instead listed as `[SKIP]` and counted under `unidentifiable`; assign those
by hand. Every row whose `Assigned Setter` is already non-blank is omitted.

After reviewing the JSON plan and completing the schema steps above, live execution is explicit:

```bash
.venv/bin/python backfill_setter_assignments.py --live
```

There is deliberately no receipt-based resume mode. Every live attempt re-reads each planned row
immediately before its PATCH and skips it if a rep assigned it after the dry-run. This current live
state is the idempotency authority, so a stale receipt or changed roster cannot leave a blank row
silently skipped. Re-run the dry-run afterwards; `blank_assignments` should be zero.

## Per-rep views

Create one filtered grid view per rep:

- `Verdict = send`
- `Assigned Setter = <rep>`
- `Status` is not `Do Not Contact`, `Not Fit`, `Won`, or `Lost`

Share only that rep's view. Keep the full `🔥 Call Queue` private to the owner/operator. These are
manual Airtable permissions and views; this repository does not create or verify them.
