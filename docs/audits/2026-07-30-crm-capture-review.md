# local-lead-machine — CRM / capture review, ~2026-07-30

Written before the Verdict collapse merged (2026-07-31). It refers to `Packet Tier`,
`Commercial Fit State` and `Contact Eligibility` as live columns and calls the collapse
"work in flight" — read the **Verification** section at the end for what survived.

Reproduced as received, with one duplicated block removed (a copy-paste artifact that
repeated the "Sales rep process" and "Findings" sections verbatim).

---

## Verdict

The discovery half of this machine is production-grade and heavily over-built relative to the
half that would tell you whether any of it works. Everything from Places → enrich → score →
gate → packet → Airtable is real, tested (953 tests), and defensible. Everything after "packet
lands in Airtable" is built, unblocked, and has never been used once. That's the architecture
problem, and it's not a code problem.

Evidence, all pulled live just now:

| Surface | State |
|---|---|
| Prospects | 145 rows: 74 New, 57 Manual Review, 7 Not Fit, 7 Mockup Needed. Zero Sent / Follow-Up / Won / Lost |
| Last Contacted | set on 1 of 145 — but 32 rows carry Outreach Channel = Phone |
| Sales Activities | 1 row, and it's a stub (Note/Lost, no name, no date, no linked prospect) |
| Deals / Clients | 1 and 2 rows, all fields blank — creation stubs |
| STATE/outcomes.jsonl | 0 bytes, untouched since Jun 20 |
| sends-log.md | "(no sends logged yet)" |

The booked call on 2026-07-28 — the single highest-information event this business has produced
— exists in neither ledger. `STATE/README.md` mandates `catalyst`, `pre_handle_objection`,
`died_at_stage` on a `call_held` row and correctly says the catalyst pattern is the outreach
trigger Loop A should target, with no other way to discover it. It wasn't captured.

So: accept-rate is unmeasurable, Gate 0 can't open, `STRATEGY/kill-criteria.md` can't fire,
`playbook-deltas.md` stays empty, and Loop D has no input. Every downstream decision is
currently made on priors.

## The map, as it actually stands

**Loop A (discovery/scoring)** — local-lead-machine. 30 top-level scripts, ~16.5k lines in
`lead_machine/`, 7 vertical profiles, 15 metro configs. Solid. Real invariants with teeth:
`sendability.admit()` as the only sendable path, the `typecast` AST scan, left-boundary term
matching, maker≠checker. This is the best-engineered part.

**CRM (Airtable)** — 4 tables, 5 views, 0 automations, 0 interfaces (confirmed via API). Purely
a passive datastore read by CLI.

**Loop B/C (assets/outreach)** — operator-agent. `SKILLS/send-queue` drafts, `SKILLS/sales-call`
arms, verifier gates enforce, permanently draft-only per `CONSTITUTION/AUTONOMY.md`.
Well-specified. Effects lanes in `routes.yaml` are genuinely good design — `pipeline_live_sync`
reserved to `lpm.pipeline` is the right shape.

**Scheduling** — not running. No crontab, no user systemd timers. Hermes gateway is up (PID
808381 since Jul 30), last morning queue 2026-07-30, `topup ran: false` — topup disabled in
config. Last send-queue output was 2026-07-23, and that file's own header says "GTM QUEUE
FAILED: engine unreachable." The operator ledger's last two entries (2026-07-28) are
`verdict: BLOCK` / `delivery-held`.

**Sales rep process** — there is no rep. `Assigned Rep` and the `00 - Sales Assignment` view are
gone. It's you, armed by `SKILLS/sales-call`, two spines (cold vs. booked). The arming is well
built. The capture is absent.

## Findings, ranked

1. **Real queue depth is 12, not 53.** `Packet Tier = Sendable Packet` on 53 rows, but readiness
   ANDs three fields. Live: 114/145 are `Commercial Fit State = unverified`, 121/145 are
   `Contact Eligibility = hold-commercial-case`. Full gate + `Status ∈ {New, Mockup Needed}` =
   12 records. That's what the send-queue engine actually sees. ~83% of your CRM is inert
   inventory that will never re-enter the queue without a re-score pass. The argument right now
   is for backfilling what you have, not discovering more.
2. **The newest scoring output never reached the base.** `ICP Lane` and `Opportunity Score` were
   created 2026-07-30 and are blank on all 145 rows. `upload_airtable.py` writes both (:88-89,
   :213-214) — no batch has been uploaded since the fields existed. The Verdict-collapse work in
   flight is being built on a signal with zero live data behind it. Worth a backfill run before
   that refactor lands, or you'll ship it blind.
3. **Dual-write with no enforcement means neither gets written.** `STATE/README.md` deliberately
   keeps Airtable and `outcomes.jsonl` independent ("neither reads the other. Log both"). The
   reasoning is sound; the result is 0% compliance on both. Two manual writes at the exact step
   with the least discipline available is a design that loses to reality. Make one derive the
   other.
4. **Breadth is a pre-fit tax.** 5 verticals live in the base (detailing 56, epoxy 31,
   water_restoration 26, plumbing 19, garage_doors 13), 7 registered, 15 metros configured. The
   one booked call came from garage doors — your smallest segment. Every vertical costs a niche
   file, terms, calibration and regression coverage, and splits already-nonexistent outcome data
   five ways so none ever accumulates enough signal to judge. This is the clearest place where
   the machine's capability outran its evidence.
5. **Doc drift against the live base.** `docs/airtable_schema_required.md` is declared as the
   code's authority and is wrong in four verified ways: it describes a `Sales — *` block on
   Prospects (gone — those fields live on Sales Activities now); it says nine views (live:
   five); it says `Tasks` awaits hand-step H3 (the table is gone, base has four tables); and it
   omits two live fields, `Lead Source` (98 Google Maps / 47 blank) and `Notes`. Separately
   `queue_state.py:47-52` still comments "The base still offers Closed and neither new choice" —
   Won/Lost went live 2026-07-30. And cross-repo, operator-agent `SKILLS/send-queue` step 3
   documents the query as `Sendable=Yes + Packet Eligible=Yes + Status not in {Sent, Closed}` —
   none of which is what `query_ready_to_send.py` does now. That last one matters most: it's the
   contract text a different repo operates on.
6. **Known, already-documented:** `run_pipeline.py` stage 1 still shells to the LEGACY
   `places_search.py` CLI; `score_leads.py` is a 143KB single file. Neither is urgent. Flagging
   so they don't get rediscovered as surprises.

## Airtable automations & interfaces

You're right that this is cheap, and it's the highest-leverage thing available — because the
binding constraint is capture, and capture fails on a laptop CLI and succeeds on a phone. Ranked
by value per minute of setup:

1. **Add a `Queue State` formula field** — first, because it makes everything below simpler.
   Don't hand-write it. `lead_machine/queue_state.py` already generates it:
   `.venv/bin/python -m lead_machine.queue_state --formula`. That's the sanctioned pattern and it
   means the base and the code can't fork the readiness rule. Then your interface and views
   filter on one field instead of three, and "Blocked: Contact Eligibility must be contact-now"
   becomes visible instead of invisible.
2. **Interface: a "Today" console over the 12 ready rows.** Record-list page filtered on
   `Queue State = Ready to Dial`, detail pane showing Specific Observation, Safe Outreach Claims,
   Opener, Mockup URL, Phone — with Status, Outreach Channel, Last Contacted and Notes editable
   inline. No new logic, works on mobile, and it's the difference between logging a send in 4
   seconds and not logging it. This alone fixes finding #1's root cause.
3. **Automation: Sales Activities row created → stamp the linked Prospect** (Status, Last
   Contacted, Next Follow-Up = today + 4). Makes the immutable event ledger the single entry
   point and derives CRM state from it, which is exactly the design
   `docs/airtable_schema_required.md` already describes. One caveat you should decide on
   knowingly: this becomes a second implementation of `FOLLOW_UP_CADENCE_DAYS`, which
   `lead_machine/follow_up.py` exists specifically to keep single-source. Either park the number
   in the base and have both read it, or accept the duplication and record it in the schema doc
   so the next drift audit catches it.
4. **Automation: "queue is dry" alert.** Fire when Ready to Dial count drops below a threshold.
   The observed failure mode is exactly this — 2026-07-22 and 07-23 both returned zero sendable
   and said so into a file nobody opened. Cheapest possible fix for the loop going quiet.
5. **Automation: `Next Follow-Up = today` → notify.** The 📅 Follow-Ups Due view exists but is
   pull-only. Currently moot (0 rows have the field set); becomes real the moment #2/#3 land.

Pick #3 **or** the inverse (Status→Sent creates an Activity row) — never both. They'd loop.

**Don't build in Airtable:** anything that scores, gates, or decides sendability.
`sendability.admit()` is the only place a lead may be declared sendable and a formula field
recomputing that would be a fourth copy of the gate. Also note the pipeline dashboard question is
already answered on the batch side by `dashboard-ui/` (`make dashboard`) — an Airtable interface
should cover the funnel (Status/vertical/accept-rate), not re-render the batch pipeline. And that
funnel dashboard is worth ~nothing until outcomes are actually being logged, so build it third,
not first.

## What I'd actually do next

1. Reconstruct the 32 phone touches and the 2026-07-28 call from memory, into both ledgers.
   Retroactive and lossy, but the catalyst from that one call is worth more than another 500
   scored leads.
2. Build the `Queue State` field + Today interface (an hour, maybe less with Omni).
3. Backfill `ICP Lane` / `Opportunity Score` before the Verdict-collapse refactor lands on top of
   empty columns.
4. Re-score the 114 unverified rows or explicitly write them off — decide, don't let them sit.
5. Truth up `airtable_schema_required.md` and the operator send-queue step 3 against what I found
   above.
6. Cut verticals to two until one produces three closes.

Discovery is not your constraint. You have 15 metros of config feeding a 12-record queue into a
funnel that records nothing. Every hour spent on the Verdict collapse, more metros, or Phase B
compounds a system whose feedback channel is a zero-byte file.

---

# Verification, 2026-08-02

Re-checked against the live base (`appLbCDdUDSZf7qaK`, read-only GETs), both repos, and the
current code. Three days later, after the Verdict collapse merged.

**Nothing in the base has moved.** Every row-level number below is an exact match to what the
review reported.

| Claim | Live 2026-08-02 | Verdict |
|---|---|---|
| 145 rows: 74 New / 57 Manual Review / 7 Mockup Needed / 7 Not Fit | Exact match | Still true |
| Zero Sent / Follow-Up / Won / Lost | Confirmed, base-wide | Still true |
| Last Contacted 1/145; 32 rows Outreach Channel=Phone | Exact match | Still true |
| Sales Activities 1 stub (`Note`/`Lost`); Deals 1 blank; Clients 2 blank | Exact match | Still true |
| `outcomes.jsonl` 0 bytes since Jun 20 | 0 bytes, Jun 20 21:42 | Still true |
| `sends-log.md` "(no sends logged yet)" | **No such file in either repo** | Unverifiable |
| 4 tables, 5 views, 0 automations, 0 interfaces | Exact match | Still true |
| No crontab, no systemd timers | Confirmed | Still true |
| #1 queue depth 12 | Still exactly 12 | True — but see below |
| #1 mechanism: three ANDed fields, 114 unverified / 121 hold-commercial-case | **Obsolete.** `Packet Tier`, `Commercial Fit State`, `Contact Eligibility` are all absent from the base. Filter is now one field: `AND({Verdict}='send',OR({Status}='New',{Status}='Mockup Needed'))` | Overtaken by the collapse |
| #1 ~83% inert inventory | Worse: Verdict `review` 85 / `reject` 25 / blank 21 / `watch` 2 = **133/145** | Still true |
| #2 ICP Lane + Opportunity Score blank on all 145 | **0/145 both** | Still true — and the refactor shipped anyway |
| #3 dual-write, 0% compliance | 32 phone touches, 1 Last Contacted, 0 outcome rows | Still true |
| #4 5 verticals live | Exact match (detailing 56, epoxy 31, water_restoration 26, plumbing 19, garage_doors 13) | Still true |
| #5a doc describes `Sales — *` on Prospects | Doc:71 says so; live Prospects has none — they're on Sales Activities | Still true, unfixed |
| #5b doc says nine views, live five | Doc:19 names nine; none of those names exist live | Still true, unfixed |
| #5c doc says `Tasks` awaits hand-step H3 | Doc:19/191-193 say so; **no `Tasks` table exists** | Still true, unfixed |
| #5d doc omits `Lead Source` and `Notes` | Both live (`Lead Source` 98 Google Maps / 47 blank — exact; `Notes` 17/145); both absent from the doc's field table | Still true, unfixed |
| #5e `queue_state.py:47-52` stale "Closed" comment | **Gone** — file rewritten by the collapse | Already fixed |
| #5f operator-agent send-queue step 3 | `SKILLS/send-queue/SKILL.md:48` still reads `Sendable=Yes + Packet Eligible=Yes + Status not in {Sent, Closed}` — all three wrong; those columns don't exist. Line 69 carries a Verdict table, so the file was half-updated | Still true, **worst one** |
| #6 run_pipeline stage 1 legacy | Confirmed | True — fixed in Track A Batch 7 |
| #6 score_leads.py 143KB | 144K | True, deprioritized |
| Automation rec #1 `Queue State` field | Not present in base; `python -m lead_machine.queue_state --formula` exists and now emits the *better* post-collapse formula | Valid, undone |
| Automation recs #2–#5 | 0 interfaces, 0 automations; `Next Follow-Up` 0/145 so #5 still moot | Valid, undone |

Minor inaccuracies, for fairness: `lead_machine/` is **14,588** lines, not ~16.5k. There are
**34** search configs, not 15. Neither changes an argument.

## The consequence that matters

`evals/accept_rate.py` **cannot return a number**, and fixing the deleted-field bug will not
change that. It counts decisions over `ACCEPTED_STATUSES = {Sent, Follow-Up, Won, Lost}` and
`REJECTED_STATUSES = {Not Fit}`, restricted to rows the machine proposed. Live: 12 rows carry
`Verdict=send` and **every one is `Status=New`** — undecided. Zero decided proposals against a
`MINIMUM_DECIDED` of 20. It reports `insufficient-sample`.

The 3.8% figure quoted in the send-readiness audit was fixture-shaped and never described this
base. The KPI is not below floor; it does not exist. This review is the reason why.

## Where it conflicts with the fix plan

Automation rec **#3** (Sales Activities row → stamp the linked Prospect's Status / Last Contacted
/ Next Follow-Up) collides with **Track A Batch 1**, which establishes `mark_sent` / `mark_outcome`
as the *only* writers of `Status`. Adding an Airtable-side writer makes three. The review's own
"pick #3 or the inverse, never both" warning now has a third party in it.

The "don't build scoring/gating in Airtable" rule also gets stronger under **Track A Batch 6**,
which makes `readiness_flags` fail closed — a formula recomputing sendability would be a fourth
copy of the gate.

## One useful dependency

**Batch 1 unblocks the backfill.** Re-uploading a scored CSV currently PATCHes human-owned
`Status` back to `New` — which is precisely why no batch has been re-uploaded since `ICP Lane`
and `Opportunity Score` were created. Fix Batch 1 and finding #2 becomes safe to close.
