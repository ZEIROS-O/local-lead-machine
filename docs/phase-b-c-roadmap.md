# Local Lead Machine — Phase B & C Roadmap

## Context

Phase A (G1–G4) is **fully complete** as of 2026-06-20. The pipeline runs without manual steps:
`places_search → enrich (buyer_intent) → score (api-llm, capped) → packets → Airtable` in one command.
Tests pass. The loop is closed and budgeted.

The gap: the pipeline produces A-grade sendable packets but **nothing sends them**. Phase B builds the outreach machine. Phase C schedules and closes the feedback loop. The ZEIROS `2026-06-20-synth-automated-outreach-loop-end-state-and-roadmap` doc imposes **Gate 0**: do not automate sending before the first close — offer is unvalidated and automating unvalidated outreach scales rejection.

---

## Phase A Status — DONE

| Group | Item | Status |
|---|---|---|
| G1 | `llm_client.py` + `--mode api-llm` + caps | ✓ |
| G2 | `verticals.py` (detailing + hardscaping profiles) + search configs | ✓ |
| G3 | `buyer_signals.py` + `score_buyer_intent()` + sendability money gate | ✓ |
| G4 | `run_pipeline.py` defaults api-llm, Airtable push, per-run summary | ✓ |

Live niche: **car detailing** (Cleveland metro). `detailing_cleveland_medium_001.json` and `detailing_cleveland_full_001.json` are the active configs. Hardscaping is DQ'd per field validation (supply-constrained, 10/10 owners booked out — no conversion problem).

---

## Gate 0 — Manual Outreach Before Automation (Do This Week)

**Why:** First close validates the offer. Building Phase B before a close automates an unvalidated pitch.

**Action:** Run Phase A pipeline on detailing, pull 8–10 A-grade leads/week, build mockup + walkthrough video manually per Sales Bible, send via personal email/DM. Target 3–5 closes before Phase B goes live.

Command to generate leads:
```bash
python run_pipeline.py \
  --batch detailing_cleveland_medium_001 \
  --vertical detailing \
  --skip-airtable  # or --live when ready
```

Packets land in `packets_detailing_cleveland_medium_001/`. Use them as the source for manual outreach asset building.

---

## Phase B — Automated Outreach Machine (Build in Order)

Each item is independently deployable. Build B1 → B2 → B3 → B4 → B5. Nothing sends without B4 approval gate.

### B0 — Exa.ai Discovery Adapter (Optional Enhancement, ~½ day)

**Should it be added?** Yes, but **after B3 is live**, not before. Exa.ai adds a semantic discovery layer (finds leads via "similar companies" / neural search, catches businesses with low Google review counts). It's complementary to Google Places, not a replacement.

**How it fits:** New `exa_search.py` alongside `places_search.py`, same output CSV schema (`name`, `place_id`, `address`, `rating`, `user_ratings_total`, `website`, `source`). Select via `--source exa` flag in `run_pipeline.py`. Config files add an `exa_query` field beside existing `queries`.

**When:** After B3 (email infra live), when you need to expand discovery quality/volume for a second metro or niche. Not blocking anything today.

---

### B1 — Outreach State Model (~1 day)

**What:** SQLite database (`outreach/state.db`) tracking per-lead contact state. Feeds from the sendable leads that Phase A produces.

**New files:** `outreach/__init__.py`, `outreach/state.py`

Schema:
```sql
contacts (
  lead_id TEXT PRIMARY KEY,  -- place_id from Phase A
  name TEXT, email TEXT, phone TEXT, channel TEXT,
  sequence_stage INTEGER DEFAULT 0,
  last_sent_at TEXT, next_due_at TEXT,
  status TEXT DEFAULT 'queued',  -- queued/approved/sent/replied/bounced/unsub/stop
  created_at TEXT
)
```

**Script:** `outreach/seed_contacts.py` reads `output/scored_*.csv` filtered to `Sendable=Yes`, inserts/updates rows (upsert on `lead_id`). Run after each pipeline batch.

**Done when:** `python outreach/seed_contacts.py --batch detailing_cleveland_medium_001` populates `state.db` with A/B sendable leads.

---

### B2 — Message Composer (~1 day)

**What:** `outreach/compose.py` — LLM drafts a 3–4 touch sequence per lead, grounded in existing packet evidence.

**Uses:** `make_packets.py` already produces `packets_*/` with `opener`, `evidence_refs`, `best_angle`, `safe_outreach_claims`. Compose reads those directly — no new evidence gathering.

**Output:** Sequence JSON per lead stored in `outreach/sequences/<lead_id>.json`:
```json
{
  "lead_id": "...",
  "touches": [
    {"day": 0, "channel": "email", "subject": "...", "body": "..."},
    {"day": 4, "channel": "email", "subject": "...", "body": "..."},
    {"day": 11, "channel": "phone", "script": "..."},
    {"day": 14, "channel": "email", "subject": "...", "body": "..."}
  ]
}
```

**Safety rules (inherit from `make_llm_batch`):**
- No fake money/result claims
- Must cite a real, named, observed issue from `evidence_refs`
- No banned phrases from Sales Bible (import as constant list)
- Touch 1 = asset/value only, no direct ask

**Done when:** Each seeded contact has a generated 4-touch sequence; spot-check 5 sequences for evidence grounding.

---

### B3 — Email Infrastructure (~1 day setup, then async warmup)

**What:** Sending domain, ESP integration, warmup. This is the make-or-break item.

**Non-negotiables:**
1. Dedicated sending domain (not your brand domain — use a variant like `lpmmails.com`)
2. SPF + DKIM + DMARC configured
3. Mailbox warmup (Instantly or Smartlead built-in warmup — 2–3 weeks before live send)
4. Daily send cap ramp: start 20/mailbox, +5/week
5. Send jitter (randomized delays between sends)
6. CAN-SPAM: unsubscribe link + physical address in every email
7. Bounce/complaint handling: auto-mark bounced leads as `status=bounced`

**ESP recommendation:** Instantly.ai or Smartlead (both have warmup built in, simple API). Alternative: Resend.com + manual warmup via Mailreach.

**New file:** `outreach/send_email.py` — thin adapter over ESP API. Takes `(contact, touch)` → sends → updates `status` in `state.db`.

**Done when:** Test batch of 5 sends from warming domain with auth passing (mail-tester.com score ≥ 9), caps enforced, unsubscribe functional.

---

### B4 — Approval Gate + Autonomy Ladder (~½ day)

**What:** `outreach/queue.py` — drafts land in an approval queue; nothing sends without approval by default.

**Queue CLI:**
```bash
python outreach/queue.py list              # show touches due today with preview
python outreach/queue.py approve <lead_id> # mark approved for send
python outreach/queue.py approve-all       # batch approve (earned after consistent accept rate)
python outreach/queue.py edit <lead_id>    # edit sequence JSON before approval
```

**Autonomy config flag** in `.env`:
```
OUTREACH_AUTONOMY_LEVEL=1  # 1=human-approves-all, 2=auto-send-touch1-only, 3=auto-send-sequence
```

Start at `OUTREACH_AUTONOMY_LEVEL=1`. Graduate to 2 after 20+ approved touch-1s with ≥50% accept rate. Replies always route to human regardless of autonomy level.

**Done when:** Nothing sends without approval by default; queue review takes < 5 min/day.

---

### B5 — Reply Detection + Sequence Stop (~½ day)

**What:** `outreach/inbox.py` — polls mailbox, detects replies/bounces/unsubs, halts sequences, routes to human.

**Implementation:**
- Poll IMAP or use ESP webhook (Instantly/Smartlead both emit `reply` events)
- On reply: set `status=replied`, `sequence_stage=STOPPED`, push notification
- On bounce: `status=bounced`
- On unsub: `status=unsub`, add to global suppression list

**Done when:** A test reply to a sent email stops the sequence within one poll cycle and sends a notification.

---

## Phase C — Compounding

### C1 — Feedback Loop (~½ day, after first replies/closes)

**What:** Extend existing `feedback.py` / `feedback_report.py` to consume outreach outcomes. Map `replied/call/deposit` outcomes from `state.db` back to score weights.

**New file:** `outreach/outcomes.py` — reads `state.db`, joins to `output/scored_*.csv` by `lead_id`, groups by grade/angle, computes reply-rate per bucket, proposes adjustments to `SCORE_WEIGHTS` in `score_leads.py`.

**Done when:** Report shows reply-rate by grade (A/B/C) and angle, with proposed weight changes.

---

### C2 — Scheduled Heartbeat (~1 day)

**What:** Cron/Routine wakes the full loop on cadence with a $0 change-detector (only spends tokens when new leads exist).

**Two schedules:**
- **Lead loop** (nightly or every 2 days): `run_pipeline.py → seed_contacts.py → compose.py` behind `MAX_LLM_USD_PER_RUN` cap
- **Outreach cycle** (daily): `queue.py list` + send due touches + `inbox.py` poll

**Gate:** Skip lead loop if no new Places results (compare row count to last run's `run_summary_*.json`).

**Done when:** Both schedules run for 3 days without manual intervention, within daily budget, emitting a daily summary.

---

## Exa.ai Decision

| Question | Answer |
|---|---|
| Should it be added? | Yes — useful semantic discovery complement |
| Priority vs. Phase B? | After B3 (email live), before C2 (scheduling) |
| Effort | ~½ day: `exa_search.py` + output schema normalization |
| What it adds | Neural search finds leads missing from Google Places; "similar companies" mode finds near-twins to existing A-leads |
| Blocking anything? | No. Google Places is sufficient for Gate 0 and Phase B. |

---

## Build Sequence

```
NOW       Gate 0 — manual outreach on Phase A output (detailing, Cleveland)
          Goal: 3–5 closes to validate offer before automating B

PHASE B   B1 → B2 → B3 (setup domain/warmup) → B4 → B5
          Domain warmup runs async during B1–B4 build (~2–3 weeks)
          Nothing sends until B4 approval gate is live

B0 (Exa) After B3 live, before C2 scheduling

PHASE C   C1 feedback (after first replies) → C2 scheduler
```

---

## Verification

| Step | Verification |
|---|---|
| B1 | `python outreach/seed_contacts.py --dry-run` shows expected contact count |
| B2 | `python outreach/compose.py --lead-id <id> --preview` shows 4-touch sequence with evidence cited |
| B3 | mail-tester.com ≥ 9, SPF/DKIM/DMARC pass, unsubscribe link works |
| B4 | Programmatic send without approval → blocked; approve in queue → fires |
| B5 | Reply to test email → `state.db` row shows `status=replied`, sequence halted, notification received |
| C2 | `--dry-run` on both schedules for 3 days; change-detector skips runs with no new Places data |
