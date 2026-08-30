# Calibration and sensitivity results

Robustness only. Canonical headlines stay on `data/` (seed 42, estimated mix): agent **38.6%**, B **32.5%**, gap **+6.1pp**, lift vs control **+17.8pp**. These runs write under `--out`. Policy, `agent/`, `simulator/`, and `generator/presence.py` were not edited. `config/failure_classes.csv` was not edited in place.

Reproduce:

```bash
python -m eval.run_calibration --peak-hours
python -m eval.run_sensitivity --reattempt-lever
```

Raw JSON: `eval/calibration_results.json`, `eval/sensitivity_results.json`. Gates on every batch below: identity 1000/1000, agent wasted = 0, impossible = 0, NSF agent < 45%.

---

## 1. NPCI-calibrated class mix (weights only)

`config/failure_classes_calibrated.csv` copies the canonical taxonomy and changes **only** `gen_weight`. `p_resolves` is identical so this is not mixed with the sensitivity sweep.

| Class | Estimated | Calibrated | Δ |
|---|---|---|---|
| `insufficient_funds` | 0.25 | 0.28 | +0.03 |
| `technical_downtime` | 0.15 | 0.18 | +0.03 |
| `customer_input_error` | 0.15 | 0.17 | +0.02 |
| `issuer_decline` | 0.15 | 0.12 | −0.03 |
| `instrument_invalid` | 0.12 | 0.10 | −0.02 |
| `session_expiry` | 0.08 | 0.03 | −0.05 |
| `limit_exceeded` | 0.05 | 0.07 | +0.02 |
| `temporary_lockout` | 0.03 | 0.03 | 0 |
| `mandate_failure` | 0.02 | 0.02 | 0 |

**Largest single move is 0.05.** The estimated mix was already close to published UPI decline shares.

Sources:

- NPCI BD/TD **81.7% / 18.3%** across top 50 remitter banks, Mar 2022–Mar 2023 ([FinBox](https://finbox.substack.com/p/the-chink-in-the-upi-armour) analysis of NPCI bank stats). Technical Decline maps to `technical_downtime` (0.18). Business Decline (55% of our mix) is funds / input error / limit / lockout.
- NACH auto-debit bounces: inadequate balance the most common reason ([Business Standard](https://www.business-standard.com/amp/article/finance/auto-debit-payment-failures-ease-in-august-shows-npci-data-121090900013_1.html) / NPCI, Aug 2021). Directional support for `insufficient_funds` as the largest class; that 33% bounce rate is pandemic-era and has fallen, so we cite the ranking, not the level.
- [Business Standard](https://www.business-standard.com/amp/article/economy-policy/insufficient-balance-wrong-pin-top-reasons-for-failed-digital-transactions-121122700487_1.html): insufficient balance and wrong PIN are the named top reasons — our two largest calibrated classes (0.28 + 0.17).

### Weight-only, six seeds (`data/calibrated/seed_S`)

| Seed | Agent | B | Gap | Control |
|---|---|---|---|---|
| 42 | 36.0% | 32.7% | +3.3pp | 22.0% |
| 1 | 41.9% | 36.5% | +5.4pp | 25.0% |
| 2 | 39.3% | 32.6% | +6.7pp | 19.5% |
| 7 | 37.6% | 32.9% | +4.7pp | 22.2% |
| 99 | 39.7% | 33.2% | +6.5pp | 24.2% |
| 123 | 41.0% | 33.4% | +7.6pp | 18.7% |
| **mean** | **39.3%** | **33.6%** | **+5.7pp** | 21.9% |

| Config | Agent | B | Gap |
|---|---|---|---|
| Estimated (canonical `data/`, seed 42) | 38.6% | 32.5% | +6.1pp |
| NPCI-calibrated (mean, six seeds) | 39.3% | 33.6% | +5.7pp |
| NPCI-calibrated, seed 42 only | 36.0% | 32.7% | +3.3pp |

The agent beats B on every seed. Seed 42 is the smallest gap (+3.3pp); it does not invert. Mean gap is 0.4pp inside the published +6.1pp.

### Evening peak (separate batch)

Razorpay reports an 8–12pp success drop at 7–10 PM. `--peak-hours` weights `failed_at` toward 19:00–22:00 and concentrates `technical_downtime` there. Default **off**, never applied to `data/`, and not mixed into `data/calibrated/` — one change at a time.

| Seed | Agent | B | Gap | Control |
|---|---|---|---|---|
| 42 | 40.2% | 33.8% | +6.4pp | 20.4% |
| 1 | 38.4% | 33.1% | +5.3pp | 24.3% |
| 2 | 39.5% | 34.5% | +5.0pp | 19.5% |
| 7 | 41.6% | 36.5% | +5.1pp | 18.2% |
| 99 | 41.5% | 35.2% | +6.4pp | 23.7% |
| 123 | 37.7% | 33.7% | +4.0pp | 20.7% |
| **mean** | **39.8%** | **34.5%** | **+5.4pp** | 21.1% |

Still beats B on every seed. Mean gap +5.4pp vs published +6.1pp.

---

## 2. Sensitivity (`p_resolves` ± 0.1)

All classes shift together, clamped to [0, 1]. Canonical **row** is existing `data/` (seed 42) — not regenerated. Pessimistic / optimistic batches: `data/sens_{cond}_{seed}`.

**Prediction (stated before looking):** control moves a lot; the gap should move little; optimistic compresses headroom.

**What the lever actually touches:** `technical_downtime`, `temporary_lockout`, daily `limit_exceeded`, and `insufficient_funds` resolve from hidden timestamps / salary day, not `fc.p_resolves`. `session_expiry` and `customer_input_error` already sit at 1.00, so +0.1 clamps. The CSV shift therefore mainly moves issuer / instrument / mandate (and a −0.1 on session/input). That is why mean control barely moves on this lever. The gap result is still the one we care about.

| Condition | Control | B | Agent | Gap |
|---|---|---|---|---|
| Pessimistic (−0.1), mean of 6 seeds | 19.1% | 30.2% | 36.1% | **+5.9pp** |
| Canonical (`data/`, seed 42) | 20.9% | 32.5% | 38.6% | **+6.1pp** |
| Optimistic (+0.1), mean of 6 seeds | 20.7% | 32.7% | 38.8% | **+6.1pp** |

Per-seed (every row gates-ok):

| Condition | Seed | Control | B | Agent | Gap |
|---|---|---|---|---|---|
| pessimistic | 42 | 23.8% | 31.4% | 37.8% | +6.4pp |
| pessimistic | 1 | 16.7% | 30.3% | 36.3% | +6.1pp |
| pessimistic | 2 | 15.1% | 31.8% | 37.1% | +5.3pp |
| pessimistic | 7 | 21.2% | 29.0% | 35.1% | +6.1pp |
| pessimistic | 99 | 23.2% | 28.7% | 35.1% | +6.5pp |
| pessimistic | 123 | 14.4% | 30.3% | 35.2% | +4.9pp |
| optimistic | 42 | 23.2% | 34.7% | 39.8% | +5.1pp |
| optimistic | 1 | 19.0% | 32.8% | 39.0% | +6.2pp |
| optimistic | 2 | 15.8% | 33.4% | 39.3% | +5.9pp |
| optimistic | 7 | 23.2% | 34.5% | 39.3% | +4.7pp |
| optimistic | 99 | 25.5% | 28.8% | 35.9% | +7.1pp |
| optimistic | 123 | 17.5% | 32.0% | 39.6% | +7.6pp |

Gap range **+4.7pp to +7.6pp**. Agent beats B on every seed. Optimistic mean gap is not compressed relative to canonical on this lever, which matches the clamp/timestamp caveat above rather than contradicting the prediction for a lever that actually moves the floor.

### Second lever: `p_reattempts` coefficients ± 0.1

Canonical formula `0.35 * reattempt + 0.45 * intent` is unchanged unless generate kwargs are passed. Shift both coefficients together ±0.1; class mix stays the estimated CSV. This is the person side of the two-factor formula, independent of `p_resolves`.

| Condition | Control | B | Agent | Gap |
|---|---|---|---|---|
| Low (0.25 / 0.35), mean of 6 seeds | **15.2%** | 28.7% | 34.5% | **+5.8pp** |
| Canonical (`data/`) | 20.9% | 32.5% | 38.6% | **+6.1pp** |
| High (0.45 / 0.55), mean of 6 seeds | **25.4%** | 34.2% | 39.9% | **+5.6pp** |

Control moves **10.2pp**. The agent–B gap moves **0.5pp**. High-reattempt is the headroom compression: control rises, every policy has less room, gap stays positive.

The sentence this buys:

> Shifting the person-side natural-recovery weights by ±0.1 moves the control rate by 10 points and changes the agent-versus-baseline gap by less than 1 point. Shifting every `p_resolves` prior by ±0.1 (the classes that actually read that column) leaves the gap at 5.9–6.1pp. The conclusion does not depend on the specific values we chose.

---

## 3. Limitations

No public dataset pairs failed payments with merchant recovery actions and eventual outcomes; that data sits with processors and their merchants and is not releasable. It is personally identifiable, commercially sensitive, and covered by RBI data-localisation rules. Nobody has published it and nobody will.

Even with real failure logs, evaluating a recovery agent requires knowing what happens when *this* agent acts, which no historical dataset contains. Off-policy evaluation would need randomness in the historical policy; production dunning follows fixed schedules.

The only genuine evaluation is a live deployment with a holdout. That is a merchant relationship, a compliance review, and months. This is not a gap we failed to close. It is structural.

Accordingly, the simulator is the one synthetic component — and the one replaced first in production. The diagnosis layer reads real Razorpay error codes, the guardrails and action space are unchanged, and the measurement design needs no synthetic input at all: withhold intervention from 20% of real failures for 14 days and count.

---

## 4. Q&A

**"It's synthetic — why should I believe any of it?"**

Three reasons. The class mix is calibrated against published NPCI decline data and differs from our original estimate by at most five points. Shifting the person-side natural-recovery weights by ±0.1 moves control by ten points and leaves the agent–baseline gap essentially unchanged; shifting `p_resolves` ±0.1 does the same for the gap. And the measurement design carries over to real data with no assumptions — the control arm needs no modelling, reality resolves the outcomes.

What we cannot claim is that our absolute recovery rates would transfer. We claim the mechanism and the measurement do.
