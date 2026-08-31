# Day 2 Review & Day 3 Plan

**Recovery Agent — Razorpay Buildathon Track 03**

Plan written after the first Day 2 eval. Canonical numbers after later generator and agent work are in `README.md` and `results_after_rebaseline.json` (agent 41.5% / B 32.5% / +20.6 pp). Fix 7 sat at 38.6%; that snapshot is `results_before_rebaseline.json`.

---

## Part 1 — Day 2 Review

### What's working

**The identity check.** `respond(..., actions=[])` returning exactly what `ground_truth.csv` holds, on all 1000 rows, is the single most valuable test in the repo. It proves the simulator and the generator agree about the no-intervention world. Without it you'd have two definitions of "natural recovery" quietly drifting apart. Keep it in CI for the rest of the week.

**Baseline B is genuinely competitive at 40.1%.** This is uncomfortable and it is the right outcome. Most teams build a strawman baseline so their agent looks heroic; a judge spots that instantly. A strong baseline means your Day 3 win has to be real.

**The consequence:** your win will not come mainly from raw recovery. It comes from **efficiency**. B spends 700 wasted debits to reach 40.1%. That contrast is the demo.

**The per-class table already tells the story.** Downtime, lockout, and session recover well under any retry. Instrument-invalid, issuer-decline and mandate-failure recover under none. That split is exactly what a diagnosis layer exists to exploit.

---

### Issue 1 — `insufficient_funds`: baseline is losing to doing nothing

| | recovery |
|---|---|
| Control (no contact) | 17.1% |
| Baseline A (retry 24h) | 13.2% |
| Baseline B (SMS + 3 retries) | 15.2% |

Both baselines sit **below** control on your largest class — 204 treatment payments, roughly a quarter of the batch.

This may be noise: the control slice for this class is only ~50 payments, so the confidence interval is wide. But it may also be a simulator artifact, and you need to know which before Day 3.

**Investigate first thing. The specific question: does a failed retry consume or suppress the natural-recovery path?**

Check for these in `simulator/response.py`:

- Does an attempted debit mark the payment as "handled", short-circuiting the natural-recovery branch?
- Does natural recovery only get evaluated when `actions == []`, rather than always running in parallel?
- Does a failed retry advance a clock or consume budget that natural recovery depends on?

**Correct behaviour:** natural recovery and agent action are **independent paths to the same outcome**. A customer whose salary lands on the 7th pays on the 7th whether or not the merchant retried on the 2nd and failed. A failed debit should be a no-op on the world, not a state change.

**If it is real rather than artifact**, it is defensible and worth a sentence in the demo: a retry at 24h fails because salary has not landed, and the failed attempt does nothing useful. It is not that the retry *hurts* — it is that it does not help, and the apparent gap is sampling noise around an ineffective intervention. Either way, say which one you concluded and why.

**Diagnostic:** re-run with 3–5 different seeds and see whether the sign flips. If control beats B on some seeds and loses on others, it is noise. If B loses every time, it is mechanism.

---

### Issue 2 — Control slices are thin per class

Your per-class control column comes from ~192 control payments spread over 9 classes. `mandate_failure` has roughly 5 controls; `temporary_lockout` maybe 6.

`mandate_failure` control = 0.0% and `instrument_invalid` control = 0.0% are almost certainly true-ish, but they are resting on tiny samples.

**Options, in order of preference:**

1. **Report per-class control with n, and do not over-claim.** Cheapest and most honest. Put the count next to every rate.
2. **Use the whole batch's natural recovery for per-class baselines**, not just the control arm. You have `would_have_recovered_naturally` for *all* 1000 payments in ground truth, including treatment ones. For per-class *diagnostics* this is a much larger sample. Keep the control arm strictly for the headline number.
3. **Bump n to 2000** for a stable per-class picture. The generator is fast; there is no cost beyond runtime.

Option 2 is the smart one and worth doing today: **headline lift from the control arm, per-class diagnostics from full ground truth.** State the distinction in the README so it does not look like you mixed them up.

---

### Issue 3 — The over-contact penalty is untested

You noted this yourself: one SMS never reaches `annoyance_threshold` (2–5), so B never triggers the penalty.

That means the mechanism that makes stopping rules matter has **never fired in any run**. It is untested code carrying a lot of narrative weight.

**Add a Baseline C: aggressive dunning.** Five messages over 14 days plus retries. It should *lose* to B on at least some classes, and it should generate opt-outs.

This is worth an hour because:
- It proves the penalty works before your agent depends on it
- It gives you a third comparison point that makes restraint visible
- "We built a baseline that over-contacts and it performed worse" is a strong demo beat — it shows the trade-off is real, not asserted

---

### Issue 4 — Not yet measuring what you will win on

Your Day 2 table reports recovery and lift. Your Day 3 win is efficiency. Add these to the eval **now**, so the comparison is apples-to-apples:

| Metric | Why |
|---|---|
| **Wasted debits** | Retries on classes where `retry_viable == never`. B: 700 |
| **Messages sent** | Total, and per class |
| **Messages per recovery** | The restraint ratio |
| **Opt-outs triggered** | Zero for A and B today; will not be zero for C |
| **Net value** | Recovered ₹ − (debit cost × attempts) − (message cost × sends) − (churn cost × opt-outs) |

Pick cost constants now and write them in config: something like ₹2 per debit attempt, ₹0.20 per SMS, ₹1 per WhatsApp, and an opt-out churn cost of the customer's `lifetime_value` × 0.3. The exact numbers matter less than having them fixed and stated before you see results.

---

### Day 2 fix list

- [ ] Multi-seed run to determine whether the `insufficient_funds` inversion is noise or mechanism
- [ ] Confirm natural recovery and agent action are independent paths in the simulator
- [ ] Per-class diagnostics from full ground truth; headline strictly from the control arm
- [ ] Report n alongside every per-class rate
- [ ] Baseline C (aggressive dunning) to exercise the annoyance penalty
- [ ] Add wasted-debits, messages, opt-outs, net-value metrics
- [ ] Fix cost constants in config before running anything

---

## Part 2 — Day 3 Plan

**Goal: rule-based agent, end to end, ugly. No ML. No LLM.**

Once this exists you have a submittable project and everything after is upside.

---

### Morning — the skeleton

#### `agent/diagnose.py`

Error reason string → failure class. Straight lookup off `config/failure_classes.csv` and `ERROR_REASONS`.

**Assert coverage on all 74 reasons.** An unmapped string must raise, not silently default to a fallback class. A silent default is how a diagnosis layer rots without anyone noticing.

This is the same lookup a production system does off a webhook payload. Say that in the demo — it is not a synthetic shortcut.

#### `agent/actions.py`

The nine bounded actions as real functions with a validated schema.

```python
ALLOWED_ACTIONS = {
    # autonomous — require has_active_mandate
    "retry_debit":                {"delay_hours": int},
    "schedule_for_payday":        {"target_date": "date"},
    "wait_for_downtime_recovery": {"recheck_hours": int},
    # customer-action
    "send_payment_link":          {"channel": ["sms", "whatsapp", "email"]},
    "request_instrument_update":  {"channel": [...]},
    "request_mandate_reauth":     {"channel": [...]},
    "send_reminder":              {"template_id": str, "channel": [...]},
    # terminal
    "escalate":                   {"reason": str},
    "mark_uncollectible":         {"reason": str},
}
```

Agent returns `{"action": ..., "args": {...}}`. Nothing free-form. This fixed schema **is** the "bounded and gated" requirement in the track brief — it is the mechanism, not a description of one.

#### `agent/guardrails.py`

The gate. Every action passes through before execution.

| Guardrail | Rule |
|---|---|
| Mandate gate | `has_active_mandate == False` → all autonomous actions rejected |
| Attempt budget | Reject if `attempt_number >= max_attempts` for the class |
| Opt-out | Opted out → only terminal actions allowed |
| Quiet hours | No messages outside permitted windows |
| Contact frequency | Max N messages per customer per week |
| Cooling-off | Promise-to-pay on record → no contact until that date |
| Value threshold | Above ₹X → escalate instead of auto-acting |

**On rejection: log it and fall back. Never execute.**

Build this today, not Day 6. It is twenty minutes and it is your graceful-failure demo. `mandate_failure` will trigger the mandate gate naturally — `has_active_mandate` is always `False` for that class by construction — so you get a live rejection on the first run without staging anything.

#### `audit/log.py`

SQLite. One row per decision:

```
payment_id, attempt_number, timestamp, failure_class,
chosen_action, action_args, gate_result, gate_reason,
executed, outcome, cost
```

Cheap to add now, painful to retrofit on Day 6 when you need a per-case timeline for the dashboard.

---

### Afternoon — the policy

Rule-based, read straight off the taxonomy row. No cleverness required — the taxonomy *is* the policy.

**Where you beat Baseline B, class by class:**

| Class | n (treat) | B's behaviour | Your move | Expected win |
|---|---|---|---|---|
| `insufficient_funds` | 204 | 24h retry → 15.2%, below control | `schedule_for_payday` | Biggest recovery gain available |
| `instrument_invalid` | 96 | wasted debits → 3.1% | Zero retries, `request_instrument_update` immediately | Efficiency + some recovery |
| `issuer_decline` | 127 | wasted debits → 4.7% | Zero retries, `send_payment_link` immediately | Efficiency |
| `mandate_failure` | 12 | 8.3% | `request_mandate_reauth`, no debits | Efficiency + gate demo |
| `technical_downtime` | 117 | 78.6% but messages a blameless customer | `wait_for_downtime_recovery`, **zero messages** | Same recovery, no message cost |
| `temporary_lockout` | 29 | fixed schedule → 89.7% | Backoff 30m / 2h / 6h | Window is 0.25–26h; backoff should match or beat |
| `session_expiry` | 59 | 74.6% | Immediate re-trigger / link under 5 min | Small gain from speed |
| `limit_exceeded` | 43 | 67.4% | Retry after 00:30 boundary; structural caps → ask | Small gain, good sub-rule demo |
| `customer_input_error` | 121 | 76.0% | `send_payment_link` under 5 min | Speed |

**The single biggest efficiency win:** `instrument_invalid` + `issuer_decline` + `mandate_failure` = **235 payments** where B spends most of its 700 wasted debits for near-zero return. Your agent spends zero debits there.

**Payday logic is your biggest recovery win.** `insufficient_funds` is 204 payments — a quarter of the treatment arm — and B is currently *below* control on it. Get this right and it moves the headline more than anything else.

The agent does not know `salary_day`. It guesses: most Indian salaries land on the 1st, some on the 7th. A reasonable heuristic is to schedule for the next 1st or 7th, whichever comes first after the failure. It will be wrong for customers on the 15th or 25th — **that is the point.** The simulator rewards being right about a hidden fact.

---

### Evening — run and compare

Report against Baseline B on four axes, not one:

| | Control | A | B | **Agent** |
|---|---|---|---|---|
| Recovery | 16.1% | 34.3% | 40.1% | ? |
| Lift vs control | — | +18.1 | +24.0 | ? |
| Wasted debits | 0 | 235 | 700 | ? |
| Messages sent | 0 | 0 | ~808 | ? |
| Net value | ? | ? | ? | ? |

---

### Gates before Day 4

- [ ] Agent recovery **≥ 40.1%**, ideally with a large `insufficient_funds` improvement
- [ ] Wasted debits **under 50** (from B's 700)
- [ ] Messages below B's count, with `technical_downtime` at **zero**
- [ ] At least one gate rejection visible in the audit log
- [ ] Every decision traceable end to end for a single payment

**If recovery lands near B rather than above it but wasted debits drop from 700 to 40, that is still a win.** Lead the framing with efficiency and net value rather than the headline rate alone. A judge who sees "same recovery, 94% fewer wasted debits, 40% fewer messages" understands the value immediately.

---

### What "ugly is the point" means

Do not:
- Add ML today (Day 5)
- Add LLM message generation today (Day 5)
- Build the dashboard today (Day 6)
- Tune the policy for hours — get the taxonomy rules running, then stop

Do:
- Get the loop closed end to end
- Write the number down before you improve anything

**The delta between today's number and Day 7's is itself a good slide.**

---

## Part 3 — Risks Going Into Day 3

| Risk | Signal | Response |
|---|---|---|
| Agent cannot beat B on recovery | Agent lands at 38–40% | Pivot the framing to efficiency + net value. Still a strong result |
| Payday heuristic is too accurate | `insufficient_funds` jumps above ~45% | Check the agent is not reading `salary_day`. Verify the import boundary |
| Wasted debits do not drop | Still in the hundreds | The policy is not reading `retry_viable == never`. Bug, not a finding |
| Gate never fires | No rejections in the audit log | The gate is not wired into the execution path |
| Everything looks perfect | All gates pass on the first run | Be suspicious. Trace one payment by hand end to end before believing it |

The last row is the one that matters most. **Suspiciously good numbers on the first run mean something leaked.** Finding and reporting that is a better story than the number would have been.
