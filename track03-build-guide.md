# Recovery Agent — Build Guide
### What to do next, from a finished taxonomy to a working demo
**Razorpay Buildathon — Track 03 · companion to `track03-recovery-agent.md`**

---

## 0. Where You Are

**Done:**
- Product scope decided — mandate/subscription failures primary, one-time checkout secondary
- Failure taxonomy complete — 9 classes, 121 codes tagged, 85 in scope
- Bucket split settled — 5 autonomous classes, 4 customer-action
- Action space defined — 9 bounded actions
- Measurement design decided — control arm, incremental lift, per-class breakdown

**Remaining before you write code:** the causal-measurement reading and the counterfactual paragraph (§1).

**Then:** Days 1–7 (§2 onward).

### Decisions locked so far — do not relitigate mid-build

| Decision | Choice | Why |
|---|---|---|
| Primary scope | Mandate/subscription failures | Only place autonomous retry genuinely exists. One-time checkout cannot be replayed — RBI requires additional-factor auth, so the merchant cannot re-submit an OTP |
| Class naming | Name **actions**, not causes | A mistyped card number isn't an "authentication failure" but takes the identical action. Naming by cause produced ambiguity twice |
| `issuer_decline` split | Lockouts moved to `temporary_lockout` | Two playbooks in one class breaks the collapsing rule — 15 codes need a new instrument, 2 just need time |
| Further splits | Stopped; use `sub_rules` | The collapsing rule finds distinctions forever. Split only when it changes the **primary** routing |
| `mandate_failure` | **Still open** — decide on Day 1 | Setup failures, not debit failures. Keep as a class or fold to `out_of_scope`. Either is defensible; write down which |

### The taxonomy, for reference

| Class | Codes | Bucket | Retry viable | Retry delay | Ask delay | Base recovery |
|---|---|---|---|---|---|---|
| `technical_downtime` | 17 | autonomous | after delay | 2–6 hrs | **n/a — don't contact** | 0.60 |
| `temporary_lockout` | 2 | autonomous (if mandate) | after delay | backoff 30m/2h/6h | n/a unless exhausted | 0.50 |
| `limit_exceeded` | 8 | autonomous (if mandate) | after delay | after daily boundary | structural caps only | 0.45 |
| `insufficient_funds` | 2 | autonomous (if mandate) | after delay | next payday | 24 hrs | 0.35 |
| `session_expiry` | 8 | autonomous (if mandate) | immediately | 0 | under 5 min | 0.30 |
| `customer_input_error` | 15 | customer-action | immediately | n/a | under 5 min | 0.25 |
| `mandate_failure` | 5 | customer-action | never | n/a | immediate | 0.15 |
| `issuer_decline` | 15 | customer-action | never | n/a | immediate | 0.10 |
| `instrument_invalid` | 13 | customer-action | never | n/a | immediate | 0.05 |

**The three-sentence version** (use this in the demo):
- `never` = the instrument is dead.
- `immediately` = nothing is broken; a session lapsed or a human fumbled. Speed is everything.
- `after delay` = a real-world condition must change — money arrives, a rail heals, a cap resets, a lockout expires.

---

## 1. Day 0 — Finish Tonight (≈1 hour)

### Why this can't wait until after Day 1

Two things from this reading go into the **generator schema**, not into your analysis later:

1. **The control arm** must exist from the first line of generator code. Ground truth needs `would_have_recovered_naturally` per payment, and the batch needs an arm assignment. Bolting it on later means regenerating everything and rerunning every measurement.
2. **The do-not-disturb segment** — customers your contact makes *worse* — must be in the latents. Without annoyance thresholds and opt-out, the simulator has no downside to over-contacting, the optimal policy becomes "message everyone forever," and your stopping rules become decorative.

Both are Day 1 schema. Neither is retrofittable.

### Read, in this order

1. **The Data Lab — Understanding customer behaviour using uplift modelling**
   Frames the incentive problem: optimising response rate measures propensity; optimising incremental sales requires uplift; picking the wrong one puts teams in conflict.
   https://thedatalab.com/technical-skills/understanding-customer-behaviour-using-uplift-modelling/

2. **Customer Science — Propensity models vs uplift models**
   The decision rule, plus the line you'll reuse: a high-propensity customer may buy anyway, a low-propensity one won't buy regardless, so uplift targets the persuadable middle. Names the do-not-disturb segment.
   https://customerscience.com.au/customer-experience-2/propensity-models-vs-uplift-models-when-to-use-each/

3. **arXiv 1811.10158 — introduction only** *(optional)*
   Clearest one-paragraph statement, via a coupon example: some redeemers were always coming, some non-redeemers were actively annoyed away.
   https://arxiv.org/pdf/1811.10158

4. **Gutiérrez & Gérardy, PMLR — first sections only** *(optional)*
   Standard survey. Covers the two-model approach. Skip the estimator comparisons.
   https://proceedings.mlr.press/v67/gutierrez17a/gutierrez17a.pdf

### Then write the paragraph

Answer: **"How do you know they wouldn't have paid anyway?"**

Rough shape:

> Some customers recover on their own — the rail heals, salary lands, they retry unprompted. If we count those as recoveries, our number is inflated. So we hold back X% of failed payments as a control arm that receives no intervention at all, and report the difference between the two arms rather than our raw rate.

Then add the honest caveat: a one-week synthetic batch gives a small control arm, so the lift estimate carries real uncertainty. Say so rather than quoting three decimals.

**If you can't write it in plain sentences, you don't have it yet.** This is the question that decides whether your headline number survives contact with a judge.

---

## 2. Day 1 — The Generator

**Budget ~25% of the week here.** This component determines whether every number you report means anything.

### 2.1 The circularity trap

You write the agent *and* the world it's tested in. If both come from the same head with the same assumptions, the test is rigged.

Concretely: you believe insufficient-funds failures recover best on payday. You put that in the agent (`if class == insufficient_funds: schedule_for_payday()`). Then you write the simulator and encode the same belief (`if action == retry_on_payday: P(pay) = 0.7 else 0.2`). Agent scores 70%, baseline scores 20%.

**You have proven nothing.** You wrote a rule, wrote a world where that rule wins, and reported that the rule won. Swap in a wrong belief — "retry at 3am works best" — put it in both files, and you get the same triumphant 70%.

It hides well: the numbers look great, the code runs, nothing errors. Circularity produces no symptom except suspiciously good results.

### 2.2 How to keep the simulator independent

**Different causal vocabulary.** The simulator reasons about things the agent cannot see. Not "did the agent retry on payday" but "did money exist in the account at the moment of the retry." The customer has a `liquidity_date`; the simulator checks whether the retry timestamp falls after it. The agent never sees `liquidity_date` — it sees the failure class and must *infer* that payday timing might help.

Now the payday heuristic is a genuine bet. Wrong guess, or a customer paid on the 7th rather than the 1st, and it loses. The simulator rewards **being right about a hidden fact**, not using a strategy.

**Physical rules, not strategic ones.** Encode constraints of reality, not opinions about tactics:
- Expired card → the debit fails. Full stop.
- Downtime window → debits inside it fail, outside it succeed.
- No balance until `liquidity_date` → debit fails.
- Contacted past `annoyance_threshold` → responsiveness drops.

**Write it from the customer's side, ideally first.** Think only "what would make this person pay?" — never "what should my agent do?" If you can write the whole simulator without once thinking about your action space, it's clean.

**The tell:** could a strategy you haven't thought of beat your agent in this simulator? If yes, it has independent structure. If the only way to score well is to do exactly what your agent does, you've built a mirror.

**Practical check:** implement one deliberately weird strategy — always WhatsApp, or always retry at exactly 48 hours — and see if it ever wins on some subset. If every alternative loses uniformly, look harder.

### 2.3 Schema

**Payment record (visible to agent):**
```
payment_id, customer_id, amount, currency, method,
failed_at, error_code, error_reason, error_source, error_step,
failure_class, has_active_mandate, mandate_expiry,
attempt_number, invoice_due_date, arm  (control | treatment)
```

**Customer record (visible):**
```
customer_id, tenure_months, past_payment_count, past_failure_count,
preferred_channel, opted_out, last_contacted_at, lifetime_value
```

**Customer latents (simulator only — never loaded by the agent):**
```
true_intent_to_pay        0-1
liquidity_date            day of month salary lands
channel_responsiveness    {sms: .., whatsapp: .., email: ..}
annoyance_threshold       contacts before disengagement
tech_savviness            affects instrument-update completion
```

**Ground truth (written once, never read during modelling):**
```
would_have_recovered_naturally, natural_recovery_date
```

> **Enforce the separation in code.** Put latents in a separate file the agent module never imports. If the agent can't physically read them, you can't leak them by accident.

### 2.4 Config from the taxonomy

Export tab 2 of the worksheet to CSV and load it directly. Same file serves as generator config *and* Day 3 diagnosis lookup.

**The weighting trap:** generation weights come from **expected real-world frequency**, not `code_count`. `insufficient_funds` has 2 codes and will be one of your highest-volume classes; `issuer_decline` has 15 largely because cards fail in many specific ways. Those columns measure unrelated things.

Suggested starting weights — adjust to taste, but keep all classes represented:

| Class | Weight |
|---|---|
| `insufficient_funds` | 25% |
| `technical_downtime` | 15% |
| `customer_input_error` | 15% |
| `issuer_decline` | 15% |
| `instrument_invalid` | 12% |
| `session_expiry` | 8% |
| `limit_exceeded` | 5% |
| `temporary_lockout` | 3% |
| `mandate_failure` | 2% |

**Keep classes reasonably balanced.** If 80% of failures are `insufficient_funds`, agent and baseline converge and your lift disappears. The three classes where you beat the baseline hardest — `customer_input_error`, `instrument_invalid`, `technical_downtime` — must be well represented or the demo has nothing to show.

### 2.5 Size and mandate mix

- 200 failed payments minimum; 500 if generation is cheap (stabilises per-class numbers)
- ~70% with active mandate, ~30% without — gives the mandate gate real work to do
- Spread `failed_at` across a month so payday effects and downtime windows are visible

### 2.6 Day 1 deliverables

- [ ] CSVs on disk: payments, customers_visible, customers_latent, ground_truth
- [ ] Taxonomy CSV loading as config
- [ ] One-page README: class priors, generation weights, and *why*
- [ ] `mandate_failure` decision written down

---

## 3. Day 2 — Simulator and Baselines

**Goal: a number to beat.** Without baselines, your recovery figure is unanchored.

### 3.1 The response function

```
P(pay | customer, failure_class, action, timing, attempt_number)
```

Two non-negotiable properties:

1. **Some customers pay with no contact at all**, driven by `base_recovery_prob`. This creates the counterfactual problem and is the entire reason a control arm exists.
2. **Over-contacting hurts.** Past `annoyance_threshold`, additional messages *reduce* payment probability and can trigger opt-out.

Without property 2, the optimal policy is "spam everyone forever," the agent learns exactly that, and the project has no interesting decision in it. **Property 2 is what makes stopping rules meaningful rather than decorative.**

### 3.2 Baselines

- **Baseline A** — fixed retry at 24h, no messaging
- **Baseline B** — retry 3× at fixed intervals + one generic SMS

Run both. Record recovery rate overall and per class.

### 3.3 Gate before proceeding

- Control-arm recovery should ≈ the weighted average of your `base_recovery_prob` values. If not, the simulator has a bug. **Free sanity check — use it.**
- If baseline recovery is 0% or 95%, something is broken. Fix before Day 3.

---

## 4. Day 3 — Agent Skeleton, End to End, Ugly

**Governing rule of the week: a working pipeline by end of Day 3.** Ugly generator → ugly agent → a printed recovery number. Once it exists, every remaining day makes it better instead of praying it comes together. Day 5 with no number = trouble. Day 3 with a bad number = fine.

### 4.1 Build

Implement the 9 actions as real functions:

```python
ALLOWED_ACTIONS = {
    # autonomous — require has_active_mandate
    "retry_debit":                {"delay_hours": int},
    "schedule_for_payday":        {"target_date": "date"},
    "wait_for_downtime_recovery": {"recheck_hours": int},
    # customer-action
    "send_payment_link":          {"channel": ["sms","whatsapp","email"]},
    "request_instrument_update":  {"channel": [...]},
    "request_mandate_reauth":     {"channel": [...]},
    "send_reminder":              {"template_id": str, "channel": [...]},
    # terminal
    "escalate":                   {"reason": str},
    "mark_uncollectible":         {"reason": str},
}
```

Agent returns `{"action": ..., "args": {...}}` → validate against schema → check guardrails → execute → log.

**Rule-based policy only today.** No ML, no LLM. Read the action straight off the taxonomy row.

### 4.2 The gate

| Guardrail | Rule |
|---|---|
| Mandate gate | `has_active_mandate == False` → all autonomous actions rejected |
| Attempt budget | Reject if `attempt_number >= max_attempts` for the class |
| Opt-out | Opted out → only terminal actions allowed |
| Quiet hours | No messages outside permitted windows |
| Contact frequency | Max N messages per customer per week |
| Cooling-off | Promise-to-pay on record → no contact until that date |
| Value threshold | Above ₹X → escalate instead of auto-acting |

**On rejection the agent does not execute — it logs the rejection and falls back to a safe default.** This is your "one failure handled gracefully" demo. Build it today, not on Day 6.

### 4.3 Deliverable

A recovery number that beats baseline, with the control/treatment split in place. **This is submittable.** Write it down — the delta from here to Day 7 is itself a good slide.

---

## 5. Day 4 — Measurement, Honestly

### 5.1 Metrics

**Headline = treatment recovery rate − control recovery rate.** Not raw recovery rate.

If control recovers 19% on its own and treatment recovers 34%, the agent contributed **15 points**. A 15-point lift with a stated control is more credible than a bare 34%.

**Secondary:**
- **Net value** = recovered ₹ − intervention costs − churn cost of opt-outs. This is what makes stopping rules matter.
- **Messages per recovery** — lower is better; demonstrates restraint
- **Opt-out rate**, treatment vs control
- **Wasted attempts** — retries on structurally futile classes. Baseline should look terrible here; this metric exists to make that visible.

### 5.2 The per-class table

Never report only the aggregate:

| Failure class | Baseline | Agent | Lift |
|---|---|---|---|
| `customer_input_error` | 20% | 50% | **+30** |
| `instrument_invalid` | 5% | 25% | **+20** |
| `technical_downtime` | 55% | 65% | +10 |
| `insufficient_funds` | 30% | 32% | +2 |

This shows **where** value comes from — huge on input errors (you act in minutes), big on dead instruments (you ask instead of pointlessly retrying), barely anything on insufficient funds (no money is no money). That honesty is more persuasive than one big number, and it proves you understand your own system.

### 5.3 Where your wins come from

Three demo-backbone cases, all consequences of the taxonomy:

- **Restraint** — `technical_downtime`. Baseline sends a dunning SMS to someone whose payment failed because a bank was down. They did nothing wrong, get alarmed, maybe call support. Your agent waits, sends nothing, payment clears itself. *Fewer messages, same or better recovery.*
- **Wasted effort** — `instrument_invalid`. Baseline burns 3 retries on a card that will never authorise. Your agent skips retrying entirely and asks for a new instrument. Clearest "diagnosis changes the action" example.
- **Speed** — `customer_input_error`. Baseline waits 24 hours; the customer has left. Your agent asks within minutes while they still have the phone in hand.

---

## 6. Day 5 — ML Decision Layer + NLP/RAG

### Morning: decision layer

- Train recovery-propensity model on simulated history (LightGBM)
- Policy chooses the action with best **expected value net of cost** — including choosing *not to contact* when EV is negative
- Retry-timing model with payday awareness
- **Stretch:** uplift model instead of propensity — predicting the *incremental* effect of contacting vs not. Technically strongest version, maps directly onto your control design.

### Afternoon: NLP + RAG

- Message generation grounded in retrieved merchant policy (permitted discounts, tone, claims that can't be made)
- Slot-filling into DLT-style approved templates
- Hinglish variants for SMS/WhatsApp
- Reply parsing: promise-to-pay intent + date (`"bhai 5 tarikh ko kar dunga"` → `{intent: promise, date: 5th}`)
- Promise tracking: kept vs broken

**RAG is not decoration** — it's what keeps offers bounded. An agent that can invent a 30% discount is a liability; one that can only offer what policy retrieval returns is a product.

### Hard stop at end of day

If the ML layer isn't beating Day 3's rules, **keep the rules as your headline** and present the ML honestly as "didn't beat the baseline." That's a credible result demonstrating judgment, not a failure.

---

## 7. Day 6 — Guardrails, Audit Trail, Dashboard

- Complete stopping rules: max attempts, immediate opt-out honour, quiet hours, cooling-off after promise-to-pay
- **Audit log** — every decision with inputs, retrieved policy chunk, gate results, chosen action, cost, outcome
- Escalation path for high-value / ambiguous cases
- Streamlit dashboard:
  - batch results table
  - per-case timeline (*failed 2pm → diagnosed downtime → waited → retried 6pm → recovered ₹2,400*)
  - lift chart + per-class table
  - the stopped/uncollectible list with reasons

### India comms compliance (≈1 hour, high credibility per minute)

- **TRAI DLT** — commercial SMS uses pre-registered templates. Design consequence: the LLM **fills slots in approved templates** rather than free-generating. Better architecture anyway.
- **DND registry** suppression
- **Quiet hours** on commercial communication
- **RBI e-mandate** — pre-debit notification before auto-debit, AFA requirements and exemption thresholds

**Verify current thresholds yourself** — these have changed repeatedly and any writeup (including this one) may be stale. Building the *shape* of the constraints is what counts.

---

## 8. Day 7 — Freeze and Rehearse

- **Code freeze by midday**
- Finalise: aggregate lift, per-class lift, net value, honest exception list
- "What we'd do next" slide: real webhook integration, live Razorpay downtime signals, uplift in production, multi-merchant policy tenancy
- Rehearse 3× against a clock

### Demo script (5 min)

1. **The problem, one case.** Expired card. Fixed-retry burns 3 attempts, recovers nothing. *(30s)*
2. **Same case, your agent.** Diagnosed `instrument_invalid` → skips retry → instrument-update request → ₹2,400 recovered. Show the timeline. *(60s)*
3. **Restraint.** Downtime failure, agent chose to wait, zero messages, payment cleared itself. *(45s)*
4. **The gate firing.** No mandate → retry rejected → fell back to a payment link. *(45s)*
5. **The numbers.** Aggregate lift vs control, then the per-class table. *(60s)*
6. **The stopping list.** N marked uncollectible, with reasons. "The agent knows when to stop." *(30s)*

---

## 9. Repo Structure

```
recovery-agent/
├── config/
│   └── failure_classes.csv        # tab 2 export — config AND diagnosis lookup
├── generator/
│   ├── entities.py                # customers, payments
│   ├── latents.py                 # hidden traits — agent NEVER imports this
│   └── generate.py
├── simulator/
│   └── response.py                # hidden ground truth — agent NEVER imports this
├── agent/
│   ├── diagnose.py                # error code → failure class
│   ├── policy.py                  # rules (D3) → ML (D5)
│   ├── actions.py                 # the 9 bounded actions
│   ├── guardrails.py              # the gate
│   └── messaging.py               # LLM + RAG (D5)
├── baselines/
│   ├── fixed_retry.py
│   └── retry_plus_sms.py
├── eval/
│   ├── metrics.py                 # lift, per-class, net value
│   └── run_batch.py
├── audit/
│   └── log.db
└── dashboard/
    └── app.py
```

The import boundary between `agent/` and `simulator/`+`generator/latents.py` is the single most important structural decision in the repo. Enforce it.

### Stack

Python · pandas/Polars · hand-rolled state machine (more explainable than a framework) · any LLM API · FAISS or Chroma · LightGBM · `causalml`/`scikit-uplift` if you attempt uplift · SQLite · Streamlit.

**Skip:** real SMS/WhatsApp integration (mock it), live Razorpay calls beyond test mode, multi-agent architectures, graph DBs, any frontend framework.

---

## 10. Pitfalls

| Pitfall | Consequence | Guard |
|---|---|---|
| No control group | Headline is meaningless; first question kills you | Control arm exists from Day 1 schema |
| Simulator shares logic with agent | Agent proves itself; numbers are fiction | Separate modules, hidden latents, enforced imports |
| No over-contact penalty | Optimal policy is "spam forever"; no real decision | Annoyance threshold + opt-out in latents |
| Class imbalance | Agent and baseline converge; lift vanishes | Balance generation weights |
| Weighting by `code_count` | `insufficient_funds` under-represented at 2 codes | Weight by expected real-world frequency |
| Agent before measurement | Retrofitting metrics on Day 6 fails | Day 2 baselines, Day 4 metrics |
| Free-form LLM actions | Cannot audit or bound; fails the track bar | Fixed action schema + validator |
| Suspiciously good numbers | Something leaked or the simulator is generous | Investigate. Reporting the bug is the better story |
| Over-investing in the LLM layer | Pretty messages, no measured recovery | **Cut agent sophistication before cutting measurement** |

---

## 11. Reading List

### Razorpay
- Error structure and field reasoning — https://razorpay.com/docs/errors/
- Full payment error list — https://razorpay.com/docs/errors/payments/list/
- Card-specific codes (richest) — https://razorpay.com/docs/errors/payments/cards/
- Source/step per method — https://razorpay.com/docs/errors/payments/payment-methods-error-parameters/

### Causal measurement
- The Data Lab, uplift modelling — https://thedatalab.com/technical-skills/understanding-customer-behaviour-using-uplift-modelling/
- Customer Science, propensity vs uplift — https://customerscience.com.au/customer-experience-2/propensity-models-vs-uplift-models-when-to-use-each/
- arXiv 1811.10158, intro only — https://arxiv.org/pdf/1811.10158
- Gutiérrez & Gérardy, PMLR — https://proceedings.mlr.press/v67/gutierrez17a/gutierrez17a.pdf

### Dunning
- Baremetrics — best structural overview; three phases, cadence norms (~6–7 messages over ~30 days, weighted to the first fortnight). Your stopping-rule skeleton.
  https://baremetrics.com/blog/ultimate-dunning-management-guide
- Alguna — retry timing by decline code; excessive retries can lower approval rates.
  https://blog.alguna.com/dunning-management/
- Chargebee — the payday principle.
  https://www.chargebee.com/blog/dunning-management-for-saas-business/
- Solidgate — metrics vocabulary.
  https://solidgate.com/blog/dunning-management/

> Treat published recovery figures with suspicion. Vendors quote 45–70%, almost none state a control group — precisely the flaw your project avoids. Worth saying in the pitch; it's sharp and it's true.

---

## 12. Definition of Done

- [ ] Counterfactual paragraph written
- [ ] Generator produces 200+ balanced payments with hidden latents and ground truth
- [ ] Import boundary enforced between agent and simulator
- [ ] Two baselines measured
- [ ] Agent runs end to end with 9 bounded actions
- [ ] Gate rejects invalid actions and falls back safely
- [ ] Control arm holdout; incremental lift computed
- [ ] Per-class lift table
- [ ] Net value including intervention cost and opt-out churn
- [ ] Audit log queryable per payment
- [ ] Uncollectible list with reasons
- [ ] Dashboard: batch view + per-case timeline
- [ ] One failure handled gracefully, on camera
- [ ] Demo rehearsed 3×

---

## 13. Résumé Framing

> *"Built a payment-recovery agent that lifted recovery from 19% to 34% on a 200-payment batch, measured against a no-intervention control group, with bounded actions and a full audit trail."*

Every clause invites a follow-up you can answer. The rare parts: **a measured outcome**, **a control group**, **bounded autonomy**. Most AI projects have none of these.

**Lead the README with the numbers table and the control-group design, not the architecture diagram.**
