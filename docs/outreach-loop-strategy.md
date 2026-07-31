# The Automated Outreach Loop — strategic spec (Phases B/C north-star)

> **What this is.** The strategic reference the whole pipeline serves. `docs/phase-a-backlog.md` is the
> *executable* plan for the input loop; this doc is the *why* and the *end-state* for the full
> machine, including the outreach (Phase B) and feedback (Phase C) loops that aren't built yet.
> **Load-bearing business decision — AI/strategy output is hypothesis, not ground truth. Verify
> against the code, the sales process, and a first real close before scaling.**

## The one-line answer

**The highest-quality automated outreach loop does NOT maximize message volume.** It automates
discovery, qualification, and the *personalized deliver-first asset* at near-zero marginal cost behind
an independent quality verifier; sequences warmth-tiered follow-up under a fail-closed compliance gate;
and routes every positive reply to a human within minutes. The two things it **never** automates: the
asset's judgment and the live close. Everything that makes outreach high-quality is a **gate, not a
throughput knob.**

## The reframe (the correction everything depends on)

Three "bottleneck" framings must be reconciled before designing the loop:

1. Stated constraint: **output volume / throughput.**
2. Real constraint (per the strategy record): **market/offer selection** — the offer is *unvalidated*
   (zero closes), and a field test killed the original niche.
3. The market is **Stage-4/5 sophisticated** — every generic SMMA opener is burned; only an
   un-fakeable mechanism survives.

**Reconciliation:** throughput is the right lens for *quality output units* (mockups, qualified
touches, assets), **not** raw message count. In a small local market (a repeated game) running a
Stage-5 offer, naive volume is *value-destroying* — it burns a finite list and risks reputation on one
spam-tell. So the loop raises throughput of high-signal units while a verifier keeps volume from
degrading to slop. Resolve the volume-vs-signal tradeoff **toward signal**: high-ticket offer,
documented buyer pain (their own broken site), finite list, one close pays for the stack.

## Canonical architecture — the five loop contracts applied to outreach

A loop is *cron + a decision-maker in the body*, closed by a verifier, capped by hard stops. Mapped:

| Contract | Outreach instance | In this repo? |
|---|---|---|
| **Trigger / heartbeat** | scheduled run; a `$0` change-detector so the model spends tokens only on new/changed leads | Phase C (not built) |
| **Persistent state** | `output/*.jsonl|*.csv` + scores dir + Airtable as system-of-record ("the agent forgets, the repo doesn't") | yes |
| **Decision-maker** | LLM scoring of fit/pain/buyer-intent + warmth-tier routing + next-touch selection | Phase A G1 (being automated onto the Anthropic API) |
| **Verifier (decisive edge)** | maker≠checker on an *objective* gate: `lead_machine/sendability.py` — **no send without a real, named, verifiable leak**. G3's planned no-money tightener did not ship; only the money-signal relaxation is live. | yes — `sendability.py`, `lead_machine/feedback.py` |
| **Halt** | `MAX_LLM_CALLS/TOKENS/USD_PER_RUN` + Places caps + opt-out suppression + reply-stops-sequence | Phase A G1 caps (being added) |

The verifier is why this is a loop and not a spam cannon. `sendability.py` already enforces
*evidence-not-claim* (`OPENER_BLOCKER`, `NO_SAFE_CLAIM_BLOCKER`, `INVALID_REF_BLOCKER`); `feedback.py`
already computes the KPI — A-lead approval-rate + false-skip rate, i.e. **cost-per-accepted-change.**
Rule: **below ~50% accept-rate the loop is losing.** Maker ≠ checker, always.

## One loop *shape*, four nested sub-loops (build in dependency order; each independently revertible)

### Loop A — Discovery & Scoring (input loop) · ~80% built · *this repo's Phase A*
Find demand-constrained, money-signal, growth-oriented prospects; grade A/B/C/Skip.
- Stack: Google Places → `enrich_leads.py` → `lead_machine/buyer_signals.py` (CRM-stack +
  review-velocity + best-effort ads = `money_signal`) → LLM scoring on the Anthropic API →
  `sendability.py` gate → `make_packets.py` → Airtable.
- Quality levers: score on *live current gaps*, not contact data; an ICP that **rejects ~90%** of TAM
  (DQ booked-out / supply-constrained owners — they have no conversion problem to sell against).
- Status: Phase A **G1** automates scoring (kills the clipboard), **G2** switches the live vertical to
  high-ticket **car detailing** (vertical-as-data), **G3** adds the buyer-money signal, **G4** closes
  it into one capped command. This is the first real loop and the prerequisite to everything below.

### Loop B — Asset Generation (the actual moat) · new · highest leverage
Per A-lead, generate the deliver-first artifact bundle at near-zero marginal cost, behind a verifier.
- Artifacts: the **mockup** (live branded homepage rebuild) + a **customer-POV walkthrough video**
  (15–40s screen-record of *their* live site failing, then the fix) + a **scored audit** ("what's
  broken and what it costs you"). The video is the highest-converting asset; the mockup collapses
  interest+trust+fit into one decision. `make_packets.py` already has `Mockup URL` / `Video URL` columns.
- Why this is the moat, not the message: in a Stage-5 market the only durable edge is the un-fakeable
  mechanism — a real rebuild of *his* site. Competitors are humans at human cost; an AI operator
  generating personalized assets at ~$60/afternoon is the asymmetric advantage. **Automate the asset;
  keep the message human-grade.**
- Verifier (non-negotiable): maker ≠ checker. A read-only cheap-model checker confirms the asset names
  a *real, verifiable* leak on the live site and the opener references it — same evidence-unit
  discipline as `sendability.py`. The external bar is what *licenses bolder* asset generation.

### Loop C — Sequencing & Reply-Routing (send loop) · *Phase B, not built*
Deliver the asset, run warmth-tiered multi-touch, hand warm replies to the human fast.
- Motion: Touch 1 = deliver the asset with **no ask**; follow-ups ~Day 0/4/11/14, *new value +
  switched channel each touch*, never "just bumping"; warmth-tiered escalation (cold→value-first;
  warm→calibrated question; hot→permission pitch; very-hot→call ask).
- Autonomy boundary: the loop runs to the edge of the *positive reply*, then halts and pages the human
  within minutes. **Speed-to-lead is the highest-variance multiplier** — the within-minutes handoff is
  structural, not nice-to-have.
- Compliance is a **fail-closed gate, not a footer**: opt-out on every channel, suppression handling,
  accurate sender identity, human-volume sending. **Channel legal-risk gradient sets the safe
  automation order:** cold **email** (CAN-SPAM, lowest risk) < **DM** (platform-ToS / ban risk) <
  cold **SMS** (TCPA per-message statutory damages — highest risk; never auto-blast without consent;
  needs A2P 10DLC). Verify TCPA/CAN-SPAM/10DLC against primary FTC/FCC sources before any volume.

### Loop D — Feedback / Self-Improvement (learning loop) · *Phase C, partly instrumented*
Capture outcomes (sent → opened → replied → call → deposit), feed back to re-weight scoring, kill dead
angles, sharpen copy.
- Stack: Airtable outcomes → `lead_machine/feedback.py` / `feedback_report.py` → adjust `SCORE_WEIGHTS`
  + retire low-reply angles. `feedback.py` already computes approval-rate + false-skip; the missing
  edge is feeding *reply/close* outcomes back, not just human grades.

### Platform variants (pick by trade)
- **Email-first** — lowest legal risk, best for autonomy; needs deliverability/domain-warmup discipline.
- **IG/FB DM** — no legal framework but rate-limit/ban risk; the mockup screenshot shines here.
- **Cold SMS** — highest intent + highest legal risk; human-sent or strictly consented only.
- **Cold call + voicemail-drop + text** — highest converting, least automatable; the loop *feeds* it.

### Business-agnostic generalization
Swap three slots, keep the contracts: **discovery signal** (weak site + money signal → hiring/funding
triggers → ad-spend signal), **deliver-first artifact** (mockup/video → custom audit → free
diagnostic), **channel** (per the risk gradient). Invariants: ICP rejects ~90%, maker≠checker verifier
on an objective send-gate, warmth-tiered cadence, fail-closed compliance, positive-reply→human handoff,
outcome feedback. *That reusable engine is the point — this repo is its first instance.*

## Roadmap (gated — sequencing is the whole game)

> **Gate 0 (the honest one): do NOT build the full autonomous outreach loop yet.** The offer is
> unvalidated (zero closes). Automating an unvalidated motion *scales rejection*. Manual outreach is
> the product backtest. **The dominant EV of the loop before offer-market fit is learning rate, not
> revenue.**

1. **Now → first close (manual outreach, automated *inputs* only).** Finish Phase A (Loop A) so good
   leads + packets arrive without you. Build Loop B's asset bundle *by hand* for ~8–10 A-leads/week.
   Send manually. Goal: **3–5 real closes** to learn which module/niche/angle converts.
2. **First close → semi-auto (autonomy ladder L1→L2).** Encode the proven-manually parts: Loop B
   behind the verifier (draft-only, human approves each); Loop C in *suggest* mode (drafts queued).
   Autonomy is *earned on a demonstrated accept-rate*, never assumed.
3. **Validated funnel → full loop (L3 inside the rails).** Auto-send *cold value-first* touches only,
   behind compliance + cap gates; wire the heartbeat (Phase C scheduler + change-detector gate); close
   Loop D. The positive-reply→human handoff and the close stay manual forever.
4. **MRR expansion.** The same machinery becomes the *deliverable* — run the client's distribution as
   an owned asset (retainer band).

## End-state — a day in the life

A scheduled run wakes; a `$0` change-detector finds new businesses in the active metro+vertical;
enrichment + LLM grade them; `sendability` blocks anything without a real verifiable leak; the asset
loop builds a branded mockup + customer-POV video + scored audit (the checker rejects any that don't
name a real leak); sequencing fires Touch 1 (asset, no ask) under the compliance gate; follow-ups add
new value per touch — and the instant someone replies positively, the sequence halts and **pages the
human within minutes** to close. Outcomes flow back and sharpen scoring. Hard stops (token/$ cap,
opt-out, reply-stop, accept-rate floor) bound it. **Net: stranger-searching → booked call on the
human's calendar, where the human does only the two highest-judgment acts.**

## Expected results (UNVALIDATED flag loud)

- **Costs (measured/estimated):** scoring ~$5/mo (Anthropic, Sonnet 4.6); sending infra ~$40/mo;
  asset generation ~$60/afternoon per premium mockup. Trivial vs a VA at $500–2,000/mo.
- **Funnel target (targets, NOT measured):** ~35 leads → 8–10 mockups → 4–6 replies → 2–3 calls →
  **1 close/week.** First ~10 real cycles replace these guesses.
- **Per-close economics (favorable IF it converts):** Growth offer $3.5–5k, 50% deposit ≈ $2k cash
  that funds the next round; low COGS → very high available CAC. Bottleneck is conversion, not unit economics.
- **Honest expectation: results are bimodal.** If offer-market fit exists, near-zero-marginal-cost cash
  machine. If not, it produces faster/cheaper *proof of that* — still the highest-value pre-fit output.

## EV scan
- Downside bounded/cheap (~$45/mo + time + the real tail: reputation/legal if compliance is sloppy) —
  asymmetric **only if** the compliance gate is fail-closed.
- Dominant EV term shifts with validation state: pre-close ≈ *learning rate*; post-close ≈ *revenue
  throughput at near-zero marginal cost*. Building for the second before the first is the build-over-sell trap.
- Biggest EV leak: hours on infra displacing the manual reps that produce the validation the infra needs → Gate 0.

## Game-theory run
- **Repeated game:** small local market, every interaction observed; one detectable lie is ruin →
  honest, evidence-first, low-volume-high-signal is the dominant strategy. The ethics gate is the moat.
- **Stage-5 arms race:** generic openers are burned; only the un-fakeable personalized mechanism is
  Nash-stable. Track reply-rate as a sophistication signal; assume your best opener depreciates.
- **Speed-to-lead is a race:** first responder usually wins → within-minutes handoff is structural.
- **Regulator/platform is a third player** with unbounded punish-payoff on cold SMS → prune that branch
  (email-first, consent before SMS, fail-closed).
- **The verifier is the Nash-stable quality mechanism** — adherence lives in deterministic structure,
  not willpower.

## What breaks it (tail-first)
1. **Building the full loop before a single close** — scales rejection, burns the finite list. *→ Gate 0.*
2. **Compliance sloppiness on SMS** — one TCPA chain dwarfs the revenue line. *→ fail-closed, email-first.*
3. **Asset homogenization** — generic mockups *invert* into the worst signal. *→ verifier requires a real named leak.*
4. **Comprehension debt** — adopting the loop to avoid understanding the sale is the skill-erosion trade; review bandwidth is the ceiling.
5. **Reply-handoff latency** — sending perfectly but catching the warm reply slowly wastes the scarcest moment.

## What would verify / change this
- **First-close data:** which niche/module/angle/channel closes, at what fulfillment hours — sets every UNVALIDATED ratio.
- **Real reply/mockup-open rates per channel** — picks SMS vs email vs DM on evidence.
- **Whether the asset verifier holds quality at volume** — measure accept-rate; below ~50% put a human back in the maker seat.
- **Whether the human can run the within-minutes handoff** — if not, the autonomy boundary moves (AI books, hands a booked call to the human).
