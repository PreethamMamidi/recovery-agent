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
natural recovery, control arm : 20.9%
expected from priors          : 20.1%
OK  gap 0.8%
```

Control-arm recovery should track the weighted average of the per-class probabilities. A large gap means the simulator has a bug. **Run this before building anything on top.**

Current output at n=1000 (seed 42, `data/generation_summary.json`):

| class | n | share | natural recovery |
|---|---|---|---|
| `technical_downtime` | 143 | 14.3% | 45.5% |
| `temporary_lockout` | 33 | 3.3% | 51.5% |
| `limit_exceeded` | 52 | 5.2% | 28.9% |
| `session_expiry` | 91 | 9.1% | 22.0% |
| `insufficient_funds` | 243 | 24.3% | 20.2% |
| `customer_input_error` | 156 | 15.6% | 18.6% |
| `mandate_failure` | 15 | 1.5% | 13.3% |
| `issuer_decline` | 163 | 16.3% | 11.7% |
| `instrument_invalid` | 104 | 10.4% | 1.0% |

The ordering is the thing to defend, not the values. Time-fixes-it at the top, needs-a-real-change at the bottom — arguable from the Razorpay docs alone.

**Note:** `insufficient_funds` lands at 20.2% rather than the flat 0.35 prior in the taxonomy sheet, because the salary mechanism supersedes the constant. If salary falls outside the 14-day window, the payment mostly doesn't recover on its own. That's the formula disagreeing with the guess — keep the formula, and update the sheet's `base_recovery_prob` column to match what the mechanism actually produces.

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

**Headline lift** uses the randomized control arm (n=187). **Per-class diagnostics** use each treatment row's own `would_have_recovered_naturally` (same n as A/B/C). Those are different samples; do not mix them. Control net is gross recovered (zero costs); rupee figures on that arm are noisy — headline is recovery-rate lift.

Costs (fixed in `config/costs.py` before seeing results): Rs 2 / debit, Rs 0.20 / SMS, Rs 1 / WhatsApp, Rs 0.05 / email, opt-out = 30% of LTV.

Canonical batch: seed 42, treatment n=813, `results_after_fix7.json`.

| | rec | lift vs control | wasted | imposs | msgs | opt-outs | net Rs |
|---|---|---|---|---|---|---|---|
| Control (no contact) | 20.9% | - | 0 | 0 | 0 | 0 | 77,878 |
| **A** — retry at 24h | 26.8% | +6.0 pp | 142 | 417 | 0 | 0 | 1,072,537 |
| **B** — SMS + 3 retries | 32.5% | +11.6 pp | 426 | 1,094 | 813 | 0 | 1,377,187 |
| **C** — 5 SMS + retries | 32.2% | +11.4 pp | 569 | 1,427 | 3,297 | **329** | 85,761 |

C matches B on recovery and destroys net value. The annoyance penalty is real; A/B never reached it (one SMS < threshold 2–5).

Per class on **treatment** rows (natural = same-row ground truth, not the thin control slice):

| class | n | natural | A | B | C |
|---|---|---|---|---|---|
| `technical_downtime` | 115 | 46.1% | 73.9% | 77.4% | 84.3% |
| `temporary_lockout` | 27 | 51.9% | 55.6% | 59.3% | 48.1% |
| `limit_exceeded` | 42 | 28.6% | 50.0% | 59.5% | 61.9% |
| `session_expiry` | 66 | 24.2% | 21.2% | 34.8% | 34.8% |
| `customer_input_error` | 122 | 19.7% | 21.3% | 40.2% | 48.4% |
| `insufficient_funds` | 198 | 20.2% | 19.2% | 21.7% | 16.7% |
| `mandate_failure` | 14 | 14.3% | 14.3% | 14.3% | 7.1% |
| `issuer_decline` | 138 | 11.6% | 11.6% | 11.6% | 6.5% |
| `instrument_invalid` | 91 | 1.1% | 1.1% | 1.1% | 1.1% |

`insufficient_funds` vs the *control slice* is sampling noise. Five other seeds: agent NSF sits above same-row natural every time, and below the 45% leak tripwire. A failed 24h debit is a no-op on the payday path.

---

## Day 3 — Rule-based agent

Diagnose from `error_reason` (74-way lookup, unknown raises). Nine bounded actions. Guardrails reject; they never execute. Policy does not import `simulator/`, `generator.latents`, or `generator.natural_recovery`.

```bash
python -m eval.run_agent
```

| | rec | lift | wasted | imposs | msgs | m/rec | opt-outs | net Rs |
|---|---|---|---|---|---|---|---|---|
| Control | 20.9% | - | 0 | 0 | 0 | 0 | 0 | 77,878 |
| B | 32.5% | +11.6 pp | 426 | 1,094 | 813 | 3.08 | 0 | 1,377,187 |
| **Agent** | **38.6%** | **+17.8 pp** | **0** | **0** | **639** | **2.04** | 0 | **1,564,615** |

Agent beats B on recovery and on efficiency. Zero wasted debits, zero impossible debits, **zero downtime-with-mandate messages**, 27 gate rejections, 46 high-value rows flagged for review (policy still runs).

Messages: agent 639 vs B 813. Messages per recovery **2.04 vs 3.08** — both recovery and the ratio moved up from the pre-follow-up snapshot (591 / 1.93 at 37.8%), because the session 6h second ask converts and costs a send. The restraint claim is not “fewest messages”; it is no messages where they don’t help. Channel mix (preferred_channel, not a spray): 256 SMS / 312 WhatsApp / 71 email. B is 813 SMS. WhatsApp is ₹1 vs SMS ₹0.20; agent message cost is ₹367 vs B’s ₹163, more than covered by extra recoveries (net ₹1.56M vs ₹1.38M; Fix 7 vs Fix 6 net +₹8k on +48 sends).

Where the taxonomy actually changes the action:

| class | n | B | agent | what changed |
|---|---|---|---|---|
| `instrument_invalid` | 91 | 1.1% | **13.2%** | update request, no debit |
| `mandate_failure` | 14 | 14.3% | **35.7%** | reauth, mandate gate demo |
| `insufficient_funds` | 198 | 21.7% | **29.3%** | nearest 1st/7th/15th (not `salary_day`) |
| `technical_downtime` | 115 | 77.4% | **86.1%** | wait then backoff; no SMS if mandate |
| `temporary_lockout` | 27 | 59.3% | **81.5%** | exponential 2h/8h/32h |
| `session_expiry` | 66 | 34.8% | **43.9%** | immediate then 6h (holds on 5/5 seeds) |
| `customer_input_error` | 122 | 40.2% | 41.0% | link only; B also sprays retries |
| `limit_exceeded` | 42 | **59.5%** | 54.8% | 00:30 ladder; B’s 24/72/120h still leads |

B still leads on `limit_exceeded` (59.5% vs 54.8% on 42 payments). Its 24/72/120h retries catch daily-cap resets the two-step 00:30 ladder misses. Matching it is possible; schedule-tuning stops here.

`insufficient_funds` at 29.3% is not a leak (gate would fire above 45%; five unseen seeds stay well under).

Our schedules encode two stated priors — Indian salaries cluster at month-start, and issuer lockout windows are undocumented so backoff beats a fixed wait. Both hold across five unseen seeds, and we deliberately did not tune to the simulator's actual window bounds. The residual risk is that we've had several iterations on this data and the baselines have had none; on real data we'd expect the gap to narrow.

Example gate rejection (`PAY_00071`): opted-out NSF → `retry_debit` rejected → `mark_uncollectible`.

---

## Robustness (calibration and sensitivity)

Headline numbers above are the canonical `data/` batch (seed 42, estimated mix). They are not replaced. The checks below write to `--out` directories and answer *"you made the data up"* without retuning policy.

**Class mix.** Generation weights have a second file, `config/failure_classes_calibrated.csv`, anchored on NPCI's published business/technical decline split — 81.7% BD / 18.3% TD across the top 50 remitter banks (FinBox analysis of NPCI bank stats, Mar 2022–Mar 2023). `technical_downtime` at 18% matches NPCI's TD share. NPCI and Business Standard both name insufficient balance and wrong PIN as the top two reasons; those are the two largest calibrated classes at 28% and 17%. Card and mandate-lifecycle failures fall outside the UPI decline taxonomy and stay estimated. The largest weight move versus the estimated mix is 0.05. `p_resolves` is not changed in that file.

Across six seeds the agent still beats B on every run (mean gap **+5.7pp** vs published **+6.1pp**).

**Priors.** Shifting every `p_resolves` by ±0.1 (clamped to [0, 1]) leaves the agent–B gap at **+5.9pp / +6.1pp / +6.1pp** (pessimistic / canonical / optimistic means). The person-side lever — `p_reattempts` coefficients 0.35/0.45 ±0.1 — moves control from **15.2%** to **25.4%** while the gap stays **+5.8pp / +5.6pp**.

Full tables, the evening-peak batch, limitations, and the Q&A answer: [calibration-sensitivity-results.md](calibration-sensitivity-results.md).

```bash
python -m generator.generate --n 1000 --seed 42   # still writes data/
python -m eval.run_calibration                    # data/calibrated/seed_*
python -m eval.run_calibration --peak-hours       # data/calibrated_peak/  (separate)
python -m eval.run_sensitivity                    # data/sens_{cond}_{seed}
python -m eval.run_sensitivity --reattempt-lever
python -m unittest discover tests
```

---

## Day 5 — Propensity (optional; rules stay the default)

The rule agent above is the floor. A LightGBM propensity model (`P(recover | visible features, action, channel)`) was trained on seeds **101–108** only. Eval seeds 42 / 1 / 2 / 7 / 99 / 123 were never used to fit.

ROC-AUC **0.855**, PR-AUC **0.867**. Not a leak (`failure_class` is not the top feature). Calibration is a bit over-confident at the top.

Three applications, measured separately, 5/6-seed bar:

- **Channel selection** — 5/6 seeds higher recovery; canonical net **drops**. Not the default.
- **EV suppression** — no incremental effect (₹99 × p almost always beats a ₹1 WhatsApp).
- **Second-ask targeting** — 6/6 seeds higher recovery **and** net (canonical **42.1%**, net ₹1,648,999). Messages rise to 947 (above B). Most of the lift is “send a 6h follow-up on the four one-shot classes,” because EV almost never refuses. Available, not the default.

```bash
python -m eval.run_agent                                    # still 38.6%
python -m eval.run_agent --use-model --ml-app second_ask    # 42.1% on data/
```

Full tables and the “who vs extra ask” caveat: [day5-results.md](day5-results.md).

---

## Next: Day 6

Dashboard. Afternoon NLP/RAG from the Day 5 doc is still open.
