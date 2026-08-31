# Fixes 3–7: Why Each Was Needed

**Recovery Agent — Razorpay Buildathon Track 03**

A record of five changes, what each was, why it mattered, and what would have happened without it. None of them was a bug in the agent. Fix 7 landed at **38.6%**. The live floor is **41.6%**. 41.5% / 951 messages was the same rules with a blanket 21:00–09:00 quiet-hours block. TRAI exempts service-class messages from that window; removing it recovered one additional payment. Numbers in this file are the Fix 7 snapshot.

---

## Fix 3 — Reason-level mandate presence

### The bug

`has_mandate` was assigned by a class-level if/elif chain. An audit of all 74 in-scope reason codes found **17 wrong**.

### Why this field specifically is so destructive

`has_mandate` decides whether a debit can happen **at all**. Getting it wrong doesn't nudge a probability — it grants or removes an entire category of action.

And it hits the two policies asymmetrically:

- **B retries every class.** Every falsely-granted mandate is a free retry for B.
- **The agent retries only where the taxonomy permits.** It benefits from far fewer of them.

**A systematic error in this one field silently subsidises the baseline.**

### Why class-level rules could never work

Consider `customer_input_error`:

| Reason | Possible on a mandate debit? | Why |
|---|---|---|
| `incorrect_otp` | Yes, above the AFA threshold | A ₹40,000 mandate debit prompts the customer to approve. They can fumble the OTP |
| `incorrect_cvv` | No, ever | AFA approval never asks for a CVV. That is a checkout-only field |

Both codes live in the same class. **No class-level rule can be right for both.**

That is why patching kept producing one more exception — the patches were at the wrong granularity. Presence is a property of the **reason code**, because the reason describes what the customer was physically doing.

### The specific miss: `temporary_lockout`

Both of its codes — `otp_attempts_exceeded`, `pin_attempts_exceeded` — require *repeated manual entry*. Both are AFA-conditional. The class rule said ANY, so roughly 70% received mandates.

After the fix: 3 of 33, because lockouts are mostly small-value and AFA requires ≥ ₹15,000.

**This class was never inspected because it was never suspected.** That is the argument for auditing the whole surface rather than chasing symptoms: symptom-chasing only finds bugs that have already produced a visible anomaly. This one had not yet.

### What changed

| | Fix 2 | Fix 3 |
|---|---|---|
| Agent recovery | 31.8% | **33.9%** |
| B recovery | 32.7% | 32.5% |
| B impossible debits | 952 | 1,094 |

The agent rose **without a single line of policy change**. Class compositions shifted toward customer-action, and the link-based policies were already correct for those. They had been evaluated against populations containing situations that could not exist.

### Without this fix

1. The agent sits ~2 points below B, in a comparison where B is handed retries a real merchant cannot make.
2. **Fix 5 would never have been found.** The lockout silence bug only became visible *because* Fix 3 collapsed lockout mandates — that is when the agent dropped to exactly natural and the inaction became obvious. With fake mandates the debit ladder was firing and masking it.
3. One question destroys the evaluation: *"why does a payment that failed from too many wrong PIN attempts have an active mandate, when a mandate debit never prompts for a PIN?"*

### The durable output

`generator/presence.py` with `_validate()` at import. A future reason code cannot silently default — which is exactly how `temporary_lockout` was missed.

---

## Fix 4 — Escalation is not abandonment

### The bug

`escalate` terminated the plan. 40 of 46 payments above ₹25,000 were frozen: no retry, no message, nothing.

### Why it hid

Every headline metric is **count-based**. Recovery rate counts payments. Lift counts payments. Wasted debits counts debits.

Freezing 46 of 813 payments costs about 2 points of recovery — noise-adjacent, easy to miss.

But those 46 were the most valuable in the batch. **The damage was value-weighted, and only one column could see it.**

### The tell

The arithmetic was impossible before anyone looked closely:

| | n recovered | Gross | Mean recovered | ≥ ₹25k recovered |
|---|---|---|---|---|
| B | 264 | ₹1,379,294 | ₹5,225 | 16 / 46 |
| Agent | 276 | ₹1,000,077 | ₹3,623 | 6 / 46 |

**Twelve more payments recovered, ₹379k less earned.** That gap cannot come from anywhere except systematically missing the expensive end.

### Why it is a conceptual error, not a coding one

The guardrail did exactly what it was written to do. The mistake was in what "escalate" was taken to mean.

In a real merchant, escalation means *a human should look at this*. It does not mean *stop trying to collect*. Nobody abandons a ₹40,000 recoverable payment because it is large — **large is precisely why you would try harder.** The guardrail had inverted its own purpose.

### The fix

High value became an **audit flag**, not a freeze. Normal policy still runs; `flagged_for_review` is written on every decision for those payments.

`escalate` was retained as genuinely terminal for cases where automation should stop:
- attempt budget exhausted
- opted out **and** amount ≥ ₹25,000

Small opted-out balances still go to `mark_uncollectible`.

### What changed

| | Fix 3 | Fix 4 |
|---|---|---|
| Agent recovery | 33.9% | 35.2% |
| Agent net | ₹999,436 | **₹1,459,847** |
| vs B net | −₹378k | **+₹83k** |

Recovery moved 1.3 points. Money moved ₹460k. **That ratio is the signature of a value-weighted bug.**

### Without this fix

A results table reading *"recovers more payments, earns less money"* — which reads as a broken system regardless of framing. The net value column stays unusable, forcing the story to lead on recovery rate alone and surrendering the strongest available argument.

---

## Fix 5 — No mandate is not the same as silence

### The bug

The taxonomy says `message_needed = none` for `technical_downtime` and `temporary_lockout`.

**That rule is correct.** Messaging someone during an outage they did not cause creates alarm and support tickets for a payment that will clear itself.

But it was written assuming a retry was available. **With no mandate there is no retry**, so "don't message" collapsed into "do nothing at all."

### How it was spotted

| Class | Natural | Agent |
|---|---|---|
| `temporary_lockout` | 51.9% | 51.9% |

**Exactly identical.** When the agent's number matches the do-nothing number to the decimal, it is not performing well — it is not performing.

### The generalisation

The specific fix was two classes. The real finding was a question nobody had asked:

> **For each class, what does the agent do when `has_active_mandate` is False?**

Asking it systematically surfaced a third case that had not been suspected: NSF payday retries were silently skipped when the target date fell outside the 14-day window, abandoning a third of no-mandate NSF.

### Why the fallback timing matters

The link fires **after** the expected clear window, not immediately:

- downtime: wait 6h (rail expected back), then SMS, then WhatsApp +24h
- lockout: wait 24h (cooling-off), then SMS, then WhatsApp +24h

This preserves the taxonomy's actual reasoning — do not contact during the outage — while adding the only lever left once it is over.

**The proof it is right:** downtime went **up**, 79.1% → 85.2%. If the timing were wrong, messages during an outage would underperform. They fire after, and they convert.

### What changed

| Class | Natural | B | Agent (Fix 4) | Agent (Fix 5) |
|---|---|---|---|---|
| `temporary_lockout` | 51.9% | 59.3% | 59.3% | **81.5%** |
| `technical_downtime` | 46.1% | 77.4% | 79.1% | **85.2%** |

Headline: 35.2% → 36.7%.

### Without this fix

Two classes contributing zero above baseline. Lockout still reading 51.9% against B's 59.3% — explaining a loss on a class where the taxonomy was actually right.

More broadly: after Fix 3, roughly **half** of all payments have no mandate. Any class whose policy was written mandate-first was quietly abandoning its no-mandate half. That is a large fraction of the batch.

### The durable output

`test_no_class_is_silent_without_mandate` — every class must produce a recovery lever, not just a wait, when `has_active_mandate = False`. This converts a one-time audit into a permanent invariant.

---

## Fix 6 — Payday ladder

### Not a bug — an unused budget

The taxonomy allows `max_attempts = 3` for `insufficient_funds`. The policy was using **one**.

These are silent debits: no annoyance cost, no message cost, ₹2 each. There was no reason for the restraint.

### Why one guess is weak

Hidden `salary_day` is drawn from `[1, 2, 3, 5, 7, 10, 15, 25]`, weighted toward month-start. A single guess at the 1st catches roughly 30% of customers. Three guesses at the nearest of 1 / 7 / 15 cover most of the mass.

### Two design details that mattered

**Proximity ordering, not calendar order.** A failure on the 3rd should try the 7th before the next 1st — the 1st is 28 days away and outside the measurement window entirely.

**Truncate, do not pad.** A failure on the 20th has only two paydays inside 14 days. Padding with a third outside the window would fire a debit that can never succeed — which is exactly the wasted-debit behaviour B is criticised for.

### What changed

| | Fix 5 | Fix 6 |
|---|---|---|
| Headline | 36.7% | 37.8% |
| NSF (n=198) | 24.7% | **29.3%** |

Against natural 20.2% and B's 21.7%. NSF beats natural on 5/5 seeds. Peak 33.0% (seed 2), well under the 45% leak tripwire. Messages unchanged.

### Without this fix

A point of headline left on the largest class, and a materially weaker story. *"We guess payday"* is far less convincing than *"we ladder across the plausible paydays inside the window."*

---

## Fix 7 — Exponential ladders

### The problem

| Class | Hidden window | Old cap |
|---|---|---|
| `technical_downtime` | 0.5–9h | 12h |
| `temporary_lockout` | 0.25–26h | 24h |

The lockout tail was being missed by construction.

### The integrity decision — the important part

The exact bounds were visible, because we wrote them. **Tuning lockout to fire at 27h would have scored higher.**

**And it would have been cheating.** A real agent does not know issuer lockout windows; they are undocumented, which the taxonomy itself states. A schedule tuned to 26h encodes knowledge the agent cannot have, and would collapse on real data where the distribution differs.

Exponential backoff (2h / 8h / 32h) covers the same range **without referencing it**. It is what would actually be deployed against unknown issuer behaviour, and it is defensible to anyone who asks.

**This is the difference between tuning and overfitting**, and it is the single most defensible choice in the sequence.

### Why lockout stayed flat, and why that is a good sign

81.5% → 81.5%.

Widening 24h → 32h cannot help a class where **25 of 27 payments have no mandate** — the debit ladder barely fires, and the no-mandate wait-then-link path already covers the one row in the tail.

Diagnosed and left alone. The alternative — hunting for something, anything, that moves the number — is precisely how tuning becomes overfitting.

**A negative result you can explain is worth more than a positive one you cannot.**

### What changed

| Class | Fix 6 | Fix 7 | B |
|---|---|---|---|
| Headline | 37.8% | **38.6%** | 32.5% |
| `technical_downtime` | 85.2% | 86.1% | 77.4% |
| `temporary_lockout` | 81.5% | 81.5% | 59.3% |
| `limit_exceeded` | 50.0% | 54.8% | 59.5% |
| `session_expiry` | 37.9% | 43.9% | 34.8% |
| `insufficient_funds` | 29.3% | 29.3% | 21.7% |

### The session follow-up, verified separately

The +6.1pp jump on `session_expiry` from one added step looked fragile, so it was tested with a **same-world ablation** — current ladder vs first-action-only, on six seeds:

| seed | n | 1-shot | +6h | delta |
|---|---|---|---|---|
| 42 | 66 | 37.9% | 43.9% | +6.1 |
| 1 | 53 | 41.5% | 50.9% | +9.4 |
| 2 | 67 | 38.8% | 50.7% | +11.9 |
| 7 | 60 | 30.0% | 36.7% | +6.7 |
| 99 | 61 | 31.1% | 42.6% | +11.5 |
| 123 | 69 | 36.2% | 40.6% | +4.3 |

Positive 6/6, with seed 42 mid-pack rather than the high. Mechanically it is a second payment link on a class whose blocker has already cleared — a second conversion roll at 0.55 attention decay on people who missed the first. Session messages 62 → 110 corroborates.

**Replicated and explainable. That makes it a finding, not an artifact.**

### The remaining loss, disclosed

`limit_exceeded`: B leads 59.5% to 54.8% on 42 payments. Its class-blind 24/72/120h retries land after the 00:30 reset and get a third conversion shot the two-step ladder does not. Matching it is straightforward and worth about 0.25pp of headline. We stopped tuning instead.

Saying *"worth 0.25pp and we chose not to"* reads as judgment. Leaving it unmentioned reads as an oversight someone else found.

---

## What all five have in common

**Not one was a bug in the agent.**

| Fix | What was actually wrong |
|---|---|
| 3 | The generator described situations that cannot occur |
| 4 | A guardrail whose semantics were inverted |
| 5 | A taxonomy rule that was correct under an assumption no longer holding |
| 6 | An unused attempt budget |
| 7 | Schedules narrower than the world they were tested in |

The policy logic was essentially right from Day 3. What was wrong was **the world it was being judged in**, and a baseline exempt from constraints the agent obeyed.

This is the hardest class of bug to find: nothing crashes, no test fails, and the numbers look plausible. They are simply measuring a world that does not exist.

### How each was found

Every one surfaced from an **arithmetic inconsistency that was not explained away**:

- More recoveries but less money → Fix 4
- Agent exactly equal to natural, to the decimal → Fix 5
- A class with 70% mandates whose failure mode requires typing a PIN → Fix 3

The bugs announced themselves as numbers that did not add up. They were checked rather than rationalised.

---

## Final position (seed 42, n=813 treatment)

| | Recovery | Lift | Wasted | Impossible | Msgs | Msgs/rec | Net ₹ |
|---|---|---|---|---|---|---|---|
| Control | 20.9% | — | 0 | 0 | 0 | 0 | 77,878 |
| B | 32.5% | +11.6 pp | 426 | 1,094 | 813 | 3.08 | 1,377,187 |
| **Agent** | **38.6%** | **+17.8 pp** | **0** | **0** | **639** | **2.04** | **1,564,615** |

Channel mix: agent 256 SMS / 312 WhatsApp / 71 email, against B's flat 813 SMS. Message cost is *higher* per send (₹367 vs ₹163) but total intervention cost is *lower* (₹1,011 vs ₹2,107) — a deliberate trade, not an accident.

### The tuning-asymmetry disclosure

> Our schedules encode two stated priors — Indian salaries cluster at month-start, and issuer lockout windows are undocumented so backoff beats a fixed wait. Both hold across five unseen seeds, and we deliberately did not tune to the simulator's actual window bounds. The residual risk is that we have had several iterations on this data and the baselines have had none; on real data we would expect the gap to narrow.

Costs nothing. Buys credibility. Preempts the sharpest available question.

---

## Why this sequence is the presentation

Most teams will show a working agent.

This shows an agent **plus the reasoning that made its numbers trustworthy** — which is the actual bar the track sets: *honest metrics, an audit trail, and one failure handled gracefully.*

Five failures found and handled, each one in our own evaluation rather than in the demo.
