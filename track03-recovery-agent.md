# Failed Payment Recovery Agent
### Razorpay Buildathon — Track 03: AI Revenue Recovery
**Build window: 7 days**

---

## 1. Problem Statement

When a customer's payment fails, the merchant usually does one of two things: nothing, or a fixed automatic retry a day later. Both leak money.

The core issue is that **a fixed retry schedule applies the same action to failures that need opposite actions.**

Consider three payments that all "failed" and all get retried three times, 24 hours apart:

| Failure | What the fixed retry does | What was actually needed |
|---|---|---|
| Card expired | Fails 3 times. Guaranteed. | Ask the customer to add a new card |
| Bank was down for 2 hours | Retries too late, and sends an alarming SMS to a customer who did nothing wrong | Wait 2 hours, retry silently, send nothing |
| Customer had no balance | Retries at a random hour | Wait for payday, then retry |

Every one of those is a wrong action, and each wrong action costs money — either directly (unrecovered payment) or indirectly (annoyed customer, support ticket, opt-out, churn).

**The failure reason should determine the response. Almost no merchant system does this.**

---

## 2. What We Are Building

> A merchant-side agent that watches failed payments on Razorpay, diagnoses why each one failed, and runs a bounded recovery workflow — retrying autonomously where a mandate allows it, prompting the customer where it doesn't, and stopping when further attempts would waste money or annoy the customer. Every action is gated and logged, and we measure the money recovered against a no-intervention control group.

**Slide version:** *Failed payments, diagnosed and recovered — with a bounded agent and honest numbers.*

### What it is not

- Not a checkout optimiser. We do not reduce the failure rate; we recover money **after** a failure has already happened.
- Not an autonomous payer. The agent never invents offers, never bypasses authentication, and cannot move money outside an existing mandate.
- Not a chatbot. The LLM's job is choosing among a fixed set of actions and writing the customer-facing copy, nothing more.

---

## 3. The Critical Scope Decision: Mandate vs One-Time

This decision shapes everything, so make it on Day 1.

**Situation A — Recurring / mandate payments.** The customer has already authorised the merchant to charge them: UPI Autopay, e-mandate on a bank account, a card on file for a subscription. The customer is not present. **The merchant's system can trigger a retry on its own.** This is where autonomous action genuinely exists.

**Situation B — One-time checkout.** Customer is on the payment page, enters a wrong OTP, payment fails. **The system cannot retry this.** There is no way to re-submit an OTP on the customer's behalf. In India, RBI requires additional-factor authentication for these transactions, so a merchant cannot replay them. The only recovery lever is to ask the customer to complete the payment themselves.

### Decision: primary scope is **mandate / subscription failures**, with one-time failures included as a secondary class.

This gives the agent real autonomous actions rather than only messages. Real use cases: subscription SaaS, EMI collections, SIP debits, recurring B2B invoices, membership renewals.

---

## 4. Architecture

### 4.1 Pipeline

```
Failed payment (webhook / batch row)
        |
        v
[1] DIAGNOSE  --  map raw error reason -> failure class
        |
        v
[2] DECIDE    --  choose an action (rules, then ML propensity)
        |
[3] GATE      --  mandate exists? attempts left? opted out? quiet hours?
        |
        +----------------+----------------+
        |                                 |
        v                                 v
[4a] AUTONOMOUS ACTIONS          [4b] CUSTOMER-ACTION REQUESTS
     retry_debit                      send_payment_link
     schedule_for_payday              request_instrument_update
     wait_for_downtime_recovery       request_mandate_reauth
                                      send_reminder
        |                                 |
        +----------------+----------------+
                         |
                         v
                 [5] OUTCOME CHECK -- recovered?
                         |
        not recovered, attempts remain -> back to [2] with attempt_number + 1
                         |
                         v
                 [6] TERMINAL: recovered / uncollectible
                     + full audit log written
```

### 4.2 Components

| Component | What it does | Tech |
|---|---|---|
| **Ingest** | Reads failed payments from a batch file (or mock webhook) | Python |
| **Diagnosis layer** | Maps Razorpay `reason` codes to one of ~7 failure classes | Lookup table, no ML |
| **Decision layer** | Picks the action. Day 3: rules. Day 5: ML propensity / uplift | LightGBM + policy |
| **Guardrail gate** | Validates the chosen action against hard constraints | Python validator |
| **Execution layer** | Runs the action against mock Razorpay APIs / mock message sender | Python |
| **Message generator** | Writes customer-facing copy, grounded in merchant policy via RAG | LLM + FAISS/Chroma |
| **Reply parser** | Extracts promise-to-pay intent + date from free-text replies (incl. Hinglish) | LLM |
| **Outcome tracker** | Polls for recovery, handles timeouts, increments attempts | Python |
| **Audit log** | Every decision with inputs, gate results, action, outcome | SQLite |
| **Dashboard** | Batch results, per-case timeline, lift charts | Streamlit |

### 4.3 Action Space (bounded, ~9 actions)

The agent may **only** emit one of these. Nothing free-form. This is the mechanism that satisfies the track's "bounded and gated" bar.

**Autonomous — money can move without the customer (requires active mandate):**
- `retry_debit(delay_hours)` — re-attempt the mandate debit
- `schedule_for_payday(target_date)` — queue a retry for the 1st / 7th
- `wait_for_downtime_recovery(recheck_hours)` — deliberately do nothing, re-check later

**Customer-action — agent can only ask:**
- `send_payment_link(channel)` — one-tap link to complete payment
- `request_instrument_update(channel)` — new card / new VPA
- `request_mandate_reauthorization(channel)` — mandate expired, needs fresh approval
- `send_reminder(template_id, channel)` — nudge, no instrument change needed

**Terminal:**
- `escalate(reason)` — hand to a human
- `mark_uncollectible(reason)` — give up, stop all contact

```python
ALLOWED_ACTIONS = {
    "retry_debit":                 {"delay_hours": int},
    "schedule_for_payday":         {"target_date": "date"},
    "wait_for_downtime_recovery":  {"recheck_hours": int},
    "send_payment_link":           {"channel": ["sms", "whatsapp", "email"]},
    "request_instrument_update":   {"channel": ["sms", "whatsapp", "email"]},
    "request_mandate_reauth":      {"channel": ["sms", "whatsapp", "email"]},
    "send_reminder":               {"template_id": str, "channel": [...]},
    "escalate":                    {"reason": str},
    "mark_uncollectible":          {"reason": str},
}
```

Agent returns `{"action": "...", "args": {...}}`. Validate against schema → check guardrails → execute → log.

**Two actions that win points:** `wait_for_downtime_recovery` and `mark_uncollectible`. Both are the agent choosing *restraint*. Being able to show a judge "here are 40 payments where the agent decided to stop, and why" proves you understood the brief. An agent that only ever escalates effort has no policy.

### 4.4 Guardrails (the gate)

Every action passes through these before execution:

| Guardrail | Rule |
|---|---|
| **Mandate gate** | If `has_active_mandate == False`, all autonomous actions are rejected |
| **Attempt budget** | Reject if `attempt_number >= max_attempts` for this failure class |
| **Opt-out** | If customer opted out, only terminal actions allowed |
| **Quiet hours** | No messages outside permitted windows |
| **Contact frequency** | Max N messages per customer per week |
| **Cooling-off** | If a promise-to-pay is on record, no contact until that date passes |
| **Value threshold** | Above ₹X, escalate to human instead of auto-acting |

**When the gate rejects an action, the agent does not execute — it logs the rejection and falls back to a safe default.** This rejection path is your "one failure handled gracefully" demo.

---

## 5. Failure Taxonomy

### 5.1 How to build it (~90 minutes)

Do **not** go code by code. Do it in this order:

1. **(10 min)** Open a spreadsheet. Write your ~7 class names down column A. The rows exist before you look at a single code.
2. **(30 min)** Open the Razorpay error list. Go down it once, tagging each code with one of your 7 classes. This is mechanical.
   - Codes that don't fit (API errors, auth errors, payout errors) go in `out_of_scope`. You only care about codes where a customer tried to pay and it didn't work.
   - If a code suggests a class you missed, add the class. Seven is a target, not a rule.
3. **(20 min)** Group your tags back into the table. That fills the `maps_from` column.
4. **(30 min)** Fill the remaining columns per class. Only 7 rows, so this is quick.

**The collapsing rule:** two codes belong to the same class if and only if **the right next action is the same.** How differently the bank words it is irrelevant.

### 5.2 Table schema

| Field | What goes in it |
|---|---|
| `class_id` | short name, e.g. `insufficient_funds` |
| `maps_from` | Razorpay reason strings across card/UPI/netbanking that collapse here |
| `bucket` | autonomous / customer-action / either (depends on mandate) |
| `retry_viable` | never / after delay / immediately |
| `optimal_delay` | hours, or a rule like "next payday window" |
| `customer_action_required` | none / update instrument / re-authorize / contact bank |
| `method_switch_helps` | yes / no |
| `message_needed` | none / informational / action-request |
| `base_recovery_prob` | simulator prior: recovery rate with **no** intervention |
| `max_attempts` | retry budget before marking uncollectible |

### 5.3 Starter classes

1. **`insufficient_funds`** — retry viable, timing is everything. Payday-anchored. Message helps.
2. **`instrument_invalid`** — card expired, VPA dead. Retry is pure waste. Message is mandatory.
3. **`authentication_failure`** — wrong OTP, MPIN error, 3DS drop-off. Customer is often still in session → speed matters. **Cannot be silently retried** — send a payment link fast.
4. **`issuer_decline`** — bank refused, possibly flagged risky. Same instrument usually fails again. Method switch is the lever.
5. **`technical_downtime`** — gateway timeout, partner bank down. Not the customer's fault. Retry after downtime clears; **messaging is counterproductive.**
6. **`session_expiry`** — payment window timed out, UPI collect expired, abandoned at bank page. Re-initiate rather than retry.
7. **`limit_exceeded`** — transaction or velocity caps. Retry with lower amount or different method.

### 5.4 Worked example rows

| | `insufficient_funds` | `technical_downtime` |
|---|---|---|
| `bucket` | autonomous (if mandate) | autonomous |
| `retry_viable` | after delay | after delay |
| `optimal_delay` | next payday window (1st / 7th) | 2–6 hrs, until downtime clears |
| `customer_action_required` | none | none |
| `method_switch_helps` | no — no money anywhere | **yes** — switch rail entirely |
| `message_needed` | action-request, gentle | **none** — don't alarm them |
| `base_recovery_prob` | 0.35 | 0.60 |
| `max_attempts` | 3 | 4 |

### 5.5 About `base_recovery_prob`

**This is not looked up. You invent it.** It is synthetic data; you are the author of reality. It means: *if we do nothing at all, what fraction recover on their own?*

- Downtime self-heals often → high (0.60)
- Expired cards essentially never self-heal → low (0.05)
- Insufficient funds recovers when salary lands → middling (0.35)

Write one line of reasoning per class. Nobody will challenge 0.35 vs 0.40 — they *will* challenge a nonsensical **ordering** (e.g. expired cards recovering more than downtime).

**Free sanity check:** your control arm result should roughly reproduce the weighted average of these values. If it doesn't, your simulator has a bug.

### 5.6 Per-method insight worth exploiting

- **Netbanking** failures skew toward bank downtime and abandonment at the bank's page — willing customer, broken rail.
- **UPI** failures skew toward collect-request expiry and PSP issues.
- **Card** failures carry the most instrument-level problems (expiry, issuer decline).

**Therefore method switching is a first-class intervention** and most teams won't build it. If a bank's netbanking is down, the right action isn't a retry or a dunning message — it's "pay via UPI instead," immediately. Razorpay publishes downtime signals for exactly this reason.

---

## 6. Measurement Design

**Lock this before writing the agent. It cannot be retrofitted on Day 6.**

### 6.1 Control vs treatment

```
              Failed payment batch
                     |
        +------------+------------+
        |                         |
   CONTROL ARM (15-20%)      TREATMENT ARM (80-85%)
   No contact at all         Agent runs the full loop
        |                         |
        +------------+------------+
                     |
          Recovery rates compared
                     |
              INCREMENTAL LIFT
```

**Headline metric = treatment recovery rate − control recovery rate.**

Not raw recovery rate. If control recovers 19% on its own and treatment recovers 34%, the agent's contribution is **15 points**. Anyone senior will trust a 15-point lift with a stated control far more than a bare 34%.

This is the answer to the killer question: *"How do you know they wouldn't have paid anyway?"*

Note: published vendor recovery figures (45–70% is commonly quoted) almost never state a control group. Worth saying out loud in your pitch — it's a sharp, true observation.

### 6.2 Secondary metrics

- **Net value** = recovered rupees − intervention costs − churn cost of opt-outs. This is what makes stopping rules matter.
- **Messages sent per recovery** — lower is better; shows restraint.
- **Opt-out rate** — treatment vs control.
- **Attempts wasted** — retries on structurally-futile classes. Baseline will be terrible here.

### 6.3 Per-class breakdown (the convincing table)

Do not report only the aggregate. Split by failure class:

| Failure class | Baseline recovery | Agent recovery | Lift |
|---|---|---|---|
| `authentication_failure` | 20% | 50% | **+30** |
| `instrument_invalid` | 5% | 25% | **+20** |
| `technical_downtime` | 55% | 65% | +10 |
| `insufficient_funds` | 30% | 32% | +2 |

This shows **where** the value comes from. Huge on auth failures (we act in minutes), big on expired instruments (we ask instead of pointlessly retrying), barely anything on insufficient funds (no money is no money). That honesty is more persuasive than one big number.

**Design consequence:** balance your synthetic batch across classes. If 80% of failures are `insufficient_funds`, agent and baseline converge and the lift disappears.

### 6.4 Baselines (build these on Day 2, before the agent)

- **Baseline A:** fixed retry at 24h, no messaging.
- **Baseline B:** retry 3× at fixed intervals + one generic SMS.

Without these, your recovery number is unanchored and means nothing. **Your baseline matters more than your agent.**

---

## 7. Synthetic Data Generator

### 7.1 Why this is the highest-risk component

You don't have real failed-payment data, so you invent it. The generator determines whether every number you report means anything. Budget ~25% of the week here.

**The trap:** if the agent's decision logic and the simulator's response logic share assumptions, you've built a machine that proves itself. The simulator must be a **separate, hidden ground-truth process.**

### 7.2 Customer latents (hidden from the agent)

Each synthetic customer gets traits the agent can never see directly:

- `true_intent_to_pay` (0–1) — do they actually want to pay?
- `liquidity_date` — when salary lands (1st, 7th, etc.)
- `channel_responsiveness` — {sms: 0.3, whatsapp: 0.6, email: 0.1}
- `annoyance_threshold` — how many contacts before they disengage
- `tech_savviness` — affects instrument-update completion rate

The agent only observes **proxies**: past payment history, tenure, order value, failure class, attempt number.

### 7.3 The hidden response function

```
P(pay | customer, failure_class, action, timing, attempt_number)
```

Two properties are non-negotiable:

1. **Some customers pay with no contact at all.** Driven by `base_recovery_prob`. This creates the counterfactual problem and is the entire reason a control arm exists.
2. **Over-contacting hurts.** Past `annoyance_threshold`, additional messages *reduce* payment probability and can trigger opt-out.

Without property 2, the optimal policy is "spam everyone forever," the agent learns exactly that, and the project has no interesting decision in it. Property 2 is what makes stopping rules meaningful rather than decorative.

### 7.4 Batch schema

**Payment record:**
```
payment_id, customer_id, amount, currency, method (card/upi/netbanking),
failed_at, error_code, error_reason, error_source, error_step,
has_active_mandate, mandate_expiry, attempt_number, invoice_due_date
```

**Customer record (visible):**
```
customer_id, tenure_months, past_payment_count, past_failure_count,
preferred_channel, opted_out, last_contacted_at, lifetime_value
```

**Customer record (hidden — simulator only):**
```
true_intent_to_pay, liquidity_date, channel_responsiveness,
annoyance_threshold, tech_savviness
```

**Ground truth (written once, never touched during modelling):**
```
would_have_recovered_naturally, natural_recovery_date
```

### 7.5 Size

200+ failed payments minimum for the headline batch. More is better for stable per-class numbers — aim for 500 if generation is cheap.

---

## 8. What to Learn

Only four things are genuinely new. Two must be learned **before Day 1** because they shape the data model and the metrics.

```
BEFORE DAY 1
  [ Failure taxonomy ]        [ Causal measurement ]
  Error codes, dunning        Control arms, uplift
           |                          |
           v                          v
DURING BUILD
  [ India comms rules ]       [ Agent guardrails ]
  DLT, DND, e-mandate         Stopping rules, audit
           |                          |
           +------------+-------------+
                        v
            [ Your existing stack ]
             ML, NLP, RAG, agents
                        |
                        v
                [ Recovery agent ]
```

### 8.1 Failure taxonomy — half a day, before Day 1

Start with Razorpay's **error structure**, not the code list. The error object carries `code`, `description`, `field`, `source`, `step`, `reason`, `metadata`. The design intent is exactly what you need: identify the **source** (customer action vs. Razorpay / gateway / bank / network), the **step** where it occurred, and the **reason**. That triple — who, where, why — *is* your taxonomy schema. Mirror theirs and the project looks native to the hackathon.

- Overview and reasoning behind the fields: https://razorpay.com/docs/errors/
- Full payment error list with next steps: https://razorpay.com/docs/errors/payments/list/
- Card-specific codes (richest source): https://razorpay.com/docs/errors/payments/cards/
- Source/step values per payment method: https://razorpay.com/docs/errors/payments/payment-methods-error-parameters/

Also skim Razorpay's subscriptions / e-mandate docs for mandate lifecycle states — that's where the "expired mandate needs re-authorisation" class comes from.

### 8.2 Dunning — same half day

Practitioner literature on failed-payment recovery. Skip vendor pitch paragraphs; read for mechanics.

- **Baremetrics dunning guide** — best structural overview. Three phases (prevention / recovery / escalation), cadence norms (~6–7 messages over ~30 days, weighted to the first two weeks, hard cancellation no sooner than day 30). This is your stopping-rule skeleton, ready-made.
  https://baremetrics.com/blog/ultimate-dunning-management-guide
- **Alguna** — best on retry timing by decline code: wait days after insufficient funds, retry quickly after a network timeout, and note that **excessive retries can lower approval rates** (justifies a retry budget).
  https://blog.alguna.com/dunning-management/
- **Chargebee (2026)** — the payday principle: retry into windows where a charge is most likely to clear, not on a rigid timer.
  https://www.chargebee.com/blog/dunning-management-for-saas-business/
- **Solidgate** — metrics vocabulary: involuntary churn rate, recovery rate, retry success rate, authorisation rate at renewal.
  https://solidgate.com/blog/dunning-management/

### 8.3 Causal measurement — one evening, before Day 1

Read in this order:

1. **The Data Lab** — frames the exact incentive problem: optimising response rate measures propensity; optimising incremental sales requires uplift. Optimising the wrong one creates competing incentives.
   https://thedatalab.com/technical-skills/understanding-customer-behaviour-using-uplift-modelling/
2. **Customer Science** — crisp decision rule for when each applies. Key line for your defence: a high-propensity customer may buy anyway; a low-propensity customer won't buy even with an offer; uplift targets the persuadable middle. Also names the group you must simulate — the **do-not-disturb segment that churns when pushed.**
   https://customerscience.com.au/customer-experience-2/propensity-models-vs-uplift-models-when-to-use-each/
3. **arXiv RL-for-uplift, intro section only** — clearest one-paragraph statement: some coupon redeemers were already coming; some non-redeemers were actively annoyed away.
   https://arxiv.org/pdf/1811.10158
4. **Gutiérrez & Gérardy (PMLR), first sections only** — the standard survey. Covers the two-model approach: fit separate models on treated and control groups using off-the-shelf learners. Skip the estimator comparisons.
   https://proceedings.mlr.press/v67/gutierrez17a/gutierrez17a.pdf

**Libraries (20 min):** `causalml` (Uber), `scikit-uplift`. Both have uplift-curve and Qini-coefficient implementations — those are the plots that make an uplift result presentable. Don't reimplement.

### 8.4 India comms compliance — one hour, around Day 5

Cheap to add, disproportionately credible. Relevant surface:

- **TRAI DLT framework** — commercial SMS uses pre-registered templates. Design consequence: your LLM **fills slots in approved templates** rather than free-generating. This is a better architecture anyway.
- **DND registry** suppression
- **Quiet hours** restrictions on commercial communication
- **RBI e-mandate rules** — pre-debit notification before auto-debit, AFA requirements and exemption thresholds

**Verify current thresholds and timing windows yourself** — these have changed repeatedly and any writeup (including this one) may be stale. Building the *shape* of these constraints into the policy layer takes an hour.

### 8.5 Agent guardrails — learn by building, Day 6

Bounded tool design, stopping rules, opt-out handling, audit logging. This is the part the rubric grades most directly and the part you'll be asked about in interviews later.

### 8.6 Deliverables from the learning phase

Don't just read. Produce two artifacts:

1. **The failure-class table** — ~7 rows, all columns filled. Becomes your generator config and diagnosis layer in one go.
2. **One paragraph** answering *"How do you know they wouldn't have paid anyway?"* If you can't write it without hedging, reread the Data Lab piece.

---

## 9. Seven-Day Roadmap

> **Governing rule: end-to-end thin slice by end of Day 3.** Ugly generator → ugly agent → a printed recovery number. Once the pipeline exists, every remaining day makes it better instead of praying it comes together. Day 5 with no number = trouble. Day 3 with a bad number = fine.

### Day 0 (prep — the evening before)
- Read the causal measurement links (§8.3)
- Read Razorpay error docs + dunning links (§8.1, §8.2)
- Write the failure-class table
- Write the counterfactual paragraph

### Day 1 — Data model and generator v1
**Goal:** fake data on disk.

- Lock the entity schema (§7.4): payments, customers (visible + hidden), ground truth
- Implement the failure taxonomy as config (from the Day 0 table)
- Generate ~200–500 failed payments, balanced across classes
- Write ground-truth `would_have_recovered_naturally` to a file you never read during modelling

**Deliverable:** CSVs, plus a one-page README explaining the class priors and why.

### Day 2 — Simulator and baselines
**Goal:** a number to beat.

- Write the hidden response function (§7.3) — separate module, no shared logic with the agent
- Implement Baseline A (fixed retry 24h, no messaging)
- Implement Baseline B (3 retries + one generic SMS)
- Run both, record recovery rates overall and per class

**Deliverable:** baseline results table. Every subsequent number now has an anchor.

**Gate:** if baseline recovery is 0% or 95%, the simulator is broken. Fix before proceeding.

### Day 3 — Agent skeleton, end to end, ugly
**Goal:** the safety net.

- Implement the 9 actions as real functions (§4.3)
- Implement the guardrail gate (§4.4)
- Rule-based policy only — no ML, no LLM
- Wire diagnose → decide → gate → execute → outcome → loop
- Run the full batch with control/treatment split

**Deliverable:** a recovery number that beats baseline. **This is submittable.** Write it down; the delta from here to Day 7 is itself a good slide.

### Day 4 — Control arm, lift, and the honest numbers
**Goal:** the measurement is real.

- Formalise the 15–20% control holdout
- Compute incremental lift, per-class breakdown, net value, messages-per-recovery, opt-out rate
- Sanity check: does control recovery ≈ weighted average of `base_recovery_prob`?
- Add the "wasted attempts" metric (retries on futile classes) — baseline should look bad here

**Deliverable:** the metrics module, and the per-class lift table.

### Day 5 — ML decision layer + NLP/RAG
**Goal:** your differentiator.

Morning — **decision layer:**
- Train recovery-propensity model on simulated history (LightGBM)
- Policy chooses action with best expected value **net of cost** — including choosing *not to contact* when EV is negative
- Retry-timing model (payday awareness)
- Stretch: uplift model instead of propensity, predicting incremental effect of contacting vs not

Afternoon — **NLP / RAG:**
- Message generation grounded in retrieved merchant policy (what discounts are permitted, tone, claims that can't be made)
- Slot-filling into DLT-style approved templates
- Hinglish variants for SMS/WhatsApp
- Reply parsing: extract promise-to-pay intent + date (`"bhai 5 tarikh ko kar dunga"` → `{intent: promise, date: 5th}`)
- Promise tracking: kept vs broken

**The RAG is not decoration** — it's what keeps offers *bounded*. An agent that can invent a 30% discount is a liability; one that can only offer what policy retrieval returns is a product.

**Hard stop at end of day.** If the ML layer isn't beating the Day 3 rules, keep rules as the headline and present the ML honestly as "didn't beat the baseline." That's a credible result, not a failure.

### Day 6 — Guardrails, audit trail, dashboard
**Goal:** the thing judges actually look at.

- Complete stopping rules: max attempts, immediate opt-out honour, quiet hours, cooling-off after promise-to-pay
- Audit log: every decision with inputs, retrieved policy chunk, gate results, chosen action, cost, outcome
- Escalation path for high-value / ambiguous cases
- Streamlit dashboard:
  - batch results table
  - per-case timeline (*failed 2pm → diagnosed downtime → waited → retried 6pm → recovered ₹2,400*)
  - lift chart + per-class table
  - the stopped/uncollectible list with reasons

### Day 7 — Freeze, rehearse, buffer
- **Code freeze by midday**
- Finalise numbers: aggregate lift, per-class lift, net value, honest exception list
- "What we'd do next" slide: real webhook integration, live Razorpay downtime signals, uplift model in production, multi-merchant policy tenancy
- Rehearse the demo 3× against a clock

---

## 10. Tech Stack

| Layer | Choice | Note |
|---|---|---|
| Language | Python | |
| Data | pandas / Polars | |
| Agent orchestration | Hand-rolled state machine, or LangGraph | Hand-rolled is fine and more explainable |
| LLM | Any API | For message generation + reply parsing only |
| RAG | FAISS or Chroma | Small policy corpus, no need for anything heavier |
| ML | LightGBM | Explainable, handles mixed features, no preprocessing |
| Uplift (stretch) | `causalml` or `scikit-uplift` | For Qini / uplift curves |
| Audit log | SQLite | |
| Dashboard | Streamlit | 3 hours, not a day. Judges don't grade CSS in this track |

**Deliberately skip:** real SMS/WhatsApp integration (mock it), live Razorpay API calls beyond test mode, multi-agent architectures, a graph DB, any frontend framework.

---

## 11. Demo Script (5 minutes)

1. **The problem, in one case.** Show a failed payment: expired card. Show what a fixed-retry system does — 3 wasted retries, no recovery. (30s)
2. **The agent on the same case.** Diagnosed as `instrument_invalid` → skips retry entirely → sends instrument-update request → recovered ₹2,400. Show the timeline. (60s)
3. **The restraint case.** A downtime failure where the agent chose `wait_for_downtime_recovery`, sent zero messages, and the payment cleared on its own. (45s)
4. **The gate firing.** A payment with no mandate where the agent's retry was rejected by the guardrail and it fell back to a payment link. (45s)
5. **The numbers.** Aggregate lift vs control, then the per-class table showing where the value comes from. (60s)
6. **The stopping list.** N payments marked uncollectible, with reasons. "The agent knows when to stop." (30s)

---

## 12. Pitfalls

| Pitfall | Consequence | Guard |
|---|---|---|
| No control group | Headline number is meaningless; first question kills you | Lock control arm on Day 4 at latest |
| Simulator shares logic with agent | Agent proves itself; numbers are fiction | Separate module, hidden latents |
| No over-contact penalty | Optimal policy is "spam forever"; no interesting decision | Annoyance threshold + opt-out in simulator |
| Class imbalance in batch | Agent and baseline converge; lift disappears | Balance failure classes deliberately |
| Building agent before measurement | Retrofitting metrics on Day 6 fails | Day 2 baselines, Day 4 metrics |
| Free-form LLM actions | Cannot audit, cannot bound, fails the track bar | Fixed action schema + validator |
| Suspiciously good numbers | Something leaked or the simulator is generous | Investigate; reporting the bug is a better story |
| Over-investing in the LLM layer | Pretty messages, no measured recovery | Cut agent sophistication before cutting measurement |

---

## 13. Definition of Done

- [ ] Failure-class table complete, ~7 classes, all columns
- [ ] Generator produces 200+ balanced failed payments with hidden ground truth
- [ ] Two baselines implemented and measured
- [ ] Agent runs end to end with 9 bounded actions
- [ ] Guardrail gate rejects invalid actions and falls back safely
- [ ] Control arm holdout, incremental lift computed
- [ ] Per-class lift table
- [ ] Net value including intervention cost and opt-out churn
- [ ] Full audit log queryable per payment
- [ ] Stopping rules demonstrated with an uncollectible list
- [ ] Dashboard with batch view + per-case timeline
- [ ] One failure handled gracefully, on camera
- [ ] Demo rehearsed 3×

---

## 14. Résumé Framing (regardless of result)

The claim this project earns you:

> *"Built a payment-recovery agent that lifted recovery from 19% to 34% on a 200-payment batch, measured against a no-intervention control group, with bounded actions and a full audit trail."*

Every clause invites a good follow-up question you can answer. The parts that make it rare: **a measured outcome**, **a control group**, and **bounded autonomy**. Most AI projects on most résumés have none of these.

**Lead the README with the numbers table and the control-group design, not the architecture diagram.**
