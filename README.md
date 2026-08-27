# Recovery Agent — Days 1–3

Synthetic failed-payment batch, hidden simulator, naive baselines, and a bounded rule-based agent.

```bash
python -m generator.generate --n 1000
python -m eval.run_baselines
python -m eval.run_agent
python -m unittest tests.test_day3
```

---

## The visible / hidden split

This is the most important structural decision in the repo.

| File | Who may read it |
|---|---|
| `data/payments_visible.csv` | **agent** — this is what a real merchant sees |
| `data/customers_visible.csv` | **agent** |
| `data/payments_hidden.csv` | simulator only |
| `data/customers_latent.csv` | simulator only |
| `data/ground_truth.csv` | simulator + eval only — write once, never read while modelling |

**Rule: `agent/` never imports `generator/latents.py`, `generator/natural_recovery.py`, or anything under `simulator/`.**

If the agent can physically read hidden state, you can leak by accident. The leak has no symptom — the code runs, nothing errors, and the numbers just look great. A model that reads `true_intent_to_pay` scores beautifully here and is worthless in production, because that column does not exist in production.

---

## Why latents exist at all

No merchant can observe a customer's salary date or irritation threshold. Neither can our agent. The latents are not a data requirement — they are a **mechanism for generating realistic outcomes**.

Real customers *do* have salary dates and annoyance limits; you just can't see them. Modelling them explicitly means outcomes are driven by mechanism rather than by a flat probability per class — which gives the agent something to be genuinely right or wrong about.

Concretely: a customer's `salary_day` might be the 7th. The agent applies a payday heuristic and guesses the 1st. The retry fails, because the simulator checks the actual date. **That is the difference between a real test and a rigged one** — the simulator rewards *being right about a hidden fact*, not *using a strategy the author liked*.

In production the latents disappear entirely and reality resolves outcomes instead. The agent code is unchanged, because it never depended on them.

---

## The natural-recovery formula

```
P(recovers unprompted) = P(problem resolves) × P(customer re-attempts)
```

Two independent things must both happen: the blocker has to clear, **and** the customer has to bother trying again.

- **P(resolves)** — a property of the failure class. A rail heals; a dead card doesn't.
- **P(re-attempts)** — a property of the person, from `reattempt_propensity` and `true_intent_to_pay`, scaled by class friction.

Mostly `P(resolves)` is computed from real hidden timestamps rather than a class constant:

| Class | How resolution is decided |
|---|---|
| `technical_downtime` | is `downtime_ends_at` inside the window? |
| `temporary_lockout` | is `lockout_ends_at` inside the window? (0.25–26h spread — undocumented in reality, which is why backoff beats a fixed wait) |
| `limit_exceeded` | daily caps reset at 00:30; structural caps mostly don't (0.15) |
| `insufficient_funds` | does the customer's `salary_day` fall inside the window? |
| others | class constant from config |

Note the two classes with `p_resolves = 1.00` — `session_expiry` and `customer_input_error`. Nothing is actually broken in either, so their entire base rate is driven by whether the customer comes back. That falls out of the formula rather than being asserted, and it's a genuine insight the flat numbers hid.

---

## Measurement window

`MEASUREMENT_WINDOW_DAYS = 14`.

"Recovers without intervention" is meaningless without a deadline — almost everything recovers eventually. Every probability in this repo means *within 14 days*.

---

## Arm assignment

Assigned at **generation time**, written into the payment record. Not decided later by the eval script.

It is a **random slice of every class and customer type** — never a selected category. Selecting the control arm by failure type would break the comparison, because any difference could then come from the failure mix rather than from the agent.

Default: 20% control.

---

## Sanity check

The generator prints it on every run:

```
natural recovery, control arm : 16.2%
expected from priors          : 19.7%
OK  gap 3.5%
```

Control-arm recovery should track the weighted average of the per-class probabilities. A large gap means the simulator has a bug. **Run this before building anything on top.**

Current output at n=1000:

| class | n | share | natural recovery |
|---|---|---|---|
| `technical_downtime` | 148 | 14.8% | 41.2% |
| `temporary_lockout` | 35 | 3.5% | 37.1% |
| `limit_exceeded` | 52 | 5.2% | 36.5% |
| `session_expiry` | 75 | 7.5% | 29.3% |
| `customer_input_error` | 150 | 15.0% | 14.7% |
| `insufficient_funds` | 245 | 24.5% | 13.5% |
| `mandate_failure` | 17 | 1.7% | 5.9% |
| `issuer_decline` | 159 | 15.9% | 4.4% |
| `instrument_invalid` | 119 | 11.9% | 2.5% |

The ordering is the thing to defend, not the values. Time-fixes-it at the top, needs-a-real-change at the bottom — arguable from the Razorpay docs alone.

**Note:** `insufficient_funds` lands at 13.5% rather than the flat 0.35 prior in the taxonomy sheet, because the salary mechanism supersedes the constant. If salary falls outside the 14-day window, the payment mostly doesn't recover on its own. That's the formula disagreeing with the guess — keep the formula, and update the sheet's `base_recovery_prob` column to match what the mechanism actually produces.

---

## Generation weights

From `config/failure_classes.csv`, column `gen_weight`. Must sum to 1.0 (enforced at load).

**These come from expected real-world frequency, not from `code_count`.** `insufficient_funds` maps from only 2 Razorpay codes but is one of the commonest real failures; `issuer_decline` maps from 15 largely because cards fail in many specific ways. Unrelated quantities.

Keep classes reasonably balanced. If 80% of failures were `insufficient_funds`, the agent and the baseline would converge and the lift would vanish. The three classes where the agent beats the baseline hardest — `customer_input_error`, `instrument_invalid`, `technical_downtime` — must stay well represented or the demo has nothing to show.

---

## Decisions recorded

**`mandate_failure` kept as a real class.** These are *setup* failures, not debit failures — a different funnel. Kept because mandates are the primary scope, so a failed mandate setup is a real revenue leak worth recovering. Consequence: `has_active_mandate` is always `False` for this class by definition, which makes it a natural test of the mandate gate.

**Two-factor natural recovery** rather than a flat per-class constant. Resolution is a property of the class; re-attempting is a property of the person. Keeping them separate gives the Day 5 propensity model real structure to learn, and gives Day 4's sensitivity check two levers instead of one.

---

## Day 2 — Simulator and baselines

The simulator checks **hidden facts** (downtime end, salary day, annoyance threshold), not strategy names. `schedule_for_payday` is just a debit at a timestamp. `agent/` must never import `simulator/`.

```bash
python -m eval.run_baselines
python -m eval.check_seeds
```

**Identity:** `respond(..., actions=[])` looks up `ground_truth.csv`. It does not re-roll. Holds on all 1000 rows.

**Headline lift** uses the randomized control arm (n=192). **Per-class diagnostics** use each treatment row's own `would_have_recovered_naturally` (same n as A/B/C). Those are different samples; do not mix them.

Costs (fixed in `config/costs.py` before seeing results): Rs 2 / debit, Rs 0.20 / SMS, Rs 1 / WhatsApp, Rs 0.05 / email, opt-out = 30% of LTV.

| | rec | lift vs control | wasted | msgs | opt-outs | net Rs |
|---|---|---|---|---|---|---|
| Control (no contact) | 16.1% | - | 0 | 0 | 0 | 198,314 |
| **A** — retry at 24h | 34.3% | +18.1 pp | 235 | 0 | 0 | 1,312,711 |
| **B** — SMS + 3 retries | **40.1%** | +24.0 pp | 700 | 808 | 0 | **1,470,786** |
| **C** — 5 SMS + retries | 40.1% | +24.0 pp | 936 | 2990 | **298** | 204,159 |

C matches B on recovery and destroys net value. The annoyance penalty is real; A/B never reached it (one SMS < threshold 2–5).

Per class on **treatment** rows (natural = same-row ground truth, not the thin control slice):

| class | n | natural | A | B | C |
|---|---|---|---|---|---|
| `technical_downtime` | 117 | 45.3% | 73.5% | 78.6% | 80.3% |
| `temporary_lockout` | 29 | 34.5% | 72.4% | 89.7% | 89.7% |
| `session_expiry` | 59 | 28.8% | 67.8% | 74.6% | 78.0% |
| `customer_input_error` | 121 | 16.5% | 57.0% | 76.0% | 76.9% |
| `limit_exceeded` | 43 | 32.6% | 55.8% | 67.4% | 67.4% |
| `insufficient_funds` | 204 | 12.7% | 13.2% | 15.2% | 14.2% |
| `mandate_failure` | 12 | 8.3% | 8.3% | 8.3% | 0.0% |
| `issuer_decline` | 127 | 4.7% | 4.7% | 4.7% | 3.9% |
| `instrument_invalid` | 96 | 3.1% | 3.1% | 3.1% | 2.1% |

`insufficient_funds` looking worse than the *control slice* (17.1% on ~40 rows) was sampling noise. Five other seeds: B sits at or above both the control slice and same-row natural. A failed 24h debit is a no-op on the payday path.

---

## Day 3 — Rule-based agent

Diagnose from `error_reason` (74-way lookup, unknown raises). Nine bounded actions. Guardrails reject; they never execute. Policy does not import `simulator/`, `generator.latents`, or `generator.natural_recovery`.

```bash
python -m eval.run_agent
```

| | rec | lift | wasted | msgs | opt-outs | net Rs |
|---|---|---|---|---|---|---|
| Control | 16.1% | - | 0 | 0 | 0 | 198,314 |
| B | **40.1%** | +24.0 pp | 700 | 808 | 0 | **1,470,786** |
| **Agent** | 33.4% | +17.3 pp | **0** | **396** | 0 | 994,580 |

Recovery is below B. The Day 3 win is efficiency: **zero wasted debits** (from 700), **half the messages**, **zero downtime messages**, 202 gate rejections in `audit/log.db`.

Where the taxonomy actually changes the action:

| class | n | B | agent | what changed |
|---|---|---|---|---|
| `instrument_invalid` | 96 | 3.1% | **18.8%** | update request, no debit |
| `mandate_failure` | 12 | 8.3% | **25.0%** | reauth, mandate gate demo |
| `insufficient_funds` | 204 | 15.2% | **17.6%** | payday heuristic (1st/7th), not `salary_day` |
| `technical_downtime` | 117 | 78.6% | 73.5% | wait then retry, no SMS |
| `customer_input_error` | 121 | 76.0% | 31.4% | link only; B also sprays retries |

`insufficient_funds` at 17.6% is not a leak (gate would fire above 45%).

Example gate rejection (`PAY_00003`): no mandate → `schedule_for_payday` rejected → fallback `send_payment_link`.

---

## Next: Day 4

Formalise lift, wasted-attempt reporting, and net-value slides. ML is Day 5. Dashboard is Day 6.
