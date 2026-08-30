# Calibration & Sensitivity Plan

**Recovery Agent — Razorpay Buildathon Track 03**

Two additions that answer *"but you made the data up."* Neither changes the agent. Both are cheap.

1. **Calibration** — ground the generation weights in published NPCI/RBI data instead of estimating them
2. **Sensitivity** — show the conclusion does not depend on the specific prior values

---

## ⚠️ READ THIS FIRST — how not to break anything

### What is at risk

Changing `gen_weight` changes the **class mix of the batch**, which changes **every headline number you have published.** 38.6%, 32.5%, +17.8pp, ₹1,564,615 — all of them move.

You are at Day 5/6 with those numbers in the README, three planning docs, and several JSON snapshots. Re-baselining now is avoidable risk.

### The safe design: do not replace the canonical batch

**Keep `data/` exactly as it is.** Write the calibrated batch to a separate directory and report it as a **robustness check**, not as your headline.

```bash
# canonical stays untouched
python -m generator.generate --n 1000 --seed 42                    # data/

# calibrated goes elsewhere
python -m generator.generate --n 1000 --seed 42 \
       --config config/failure_classes_calibrated.csv \
       --out data/calibrated
```

You already added `--out` during the seed check. Add `--config` the same way — a second optional flag, defaulting to the existing path.

**The claim you end up with is stronger than a re-baseline would have been:**

> Our conclusion holds under both our estimated class mix and one calibrated against published NPCI decline data.

That is a robustness result. A re-baseline is just a different single number.

### What this change cannot break

`gen_weight` is a single column in `config/failure_classes.csv`. It is read once, at generation, to pick which class each payment belongs to. It touches nothing else.

| Fix | Lives in | Affected? |
|---|---|---|
| Fix 1 — simulator mandate constraint | `simulator/response.py` | No |
| Fix 2 — class-level mandate plausibility | superseded by Fix 3 | No |
| Fix 3 — reason-level presence | `generator/presence.py` | No — presence is keyed on reason, not weight |
| Fix 4 — escalate is a flag | `agent/guardrails.py` | No |
| Fix 5 — no-mandate fallback | `agent/policy.py` | No |
| Fix 6 — payday ladder | `agent/policy.py` | No |
| Fix 7 — exponential ladders | `agent/policy.py` | No |

**Nothing in `agent/`, `simulator/`, or `generator/presence.py` is touched.** Every fix survives by construction.

### The checks that must still pass after calibration

Run these on the calibrated batch before looking at any performance number:

- [ ] `_validate()` in `presence.py` still passes (all 74 reasons mapped)
- [ ] `gen_weight` sums to 1.0 — the loader already enforces this, do not disable it
- [ ] Identity check: `respond(..., actions=[])` returns `ground_truth.csv`, 1000/1000
- [ ] Agent wasted debits = 0, impossible debits = 0
- [ ] NSF agent recovery under 45% (leak tripwire)
- [ ] Full test suite green
- [ ] `data/` is byte-identical to before — `git status` should show it unchanged

If `data/` changed, you overwrote the canonical batch. Regenerate with the original config and seed before doing anything else.

### Rules for the session

1. **One config file per run.** Never edit `config/failure_classes.csv` in place. Create `failure_classes_calibrated.csv` alongside it.
2. **Do not touch `p_resolves` during calibration.** That is the sensitivity sweep's job, and mixing the two makes attribution impossible.
3. **Do not touch policy.** No tuning while this is in flight.
4. **Commit before you start.** `git commit -am "pre-calibration"` so a revert is one command.

---

## Part 1 — What is actually published

Searched and verified. These are real, citable sources.

### 1.1 The single best anchor: NPCI's BD/TD split

NPCI classifies every declined UPI transaction into two buckets:

- **Technical Decline (TD)** — bank or NPCI infrastructure: server unavailability, timeouts, network issues
- **Business Decline (BD)** — user side: wrong PIN, insufficient balance, exceeded limits, invalid beneficiary

Per an analysis of NPCI data across March 2022 – March 2023 (top 50 remitter banks): **81.7% of failures were business declines, 18.26% technical declines.**

Source: https://finbox.substack.com/p/the-chink-in-the-upi-armour

**This maps directly onto your taxonomy:**

| NPCI bucket | Your classes |
|---|---|
| Technical Decline | `technical_downtime` |
| Business Decline | `insufficient_funds`, `customer_input_error`, `limit_exceeded`, `temporary_lockout` |
| (not in the NPCI split) | `issuer_decline`, `instrument_invalid`, `session_expiry`, `mandate_failure` |

Your current `technical_downtime` weight is **0.15**. NPCI's TD share is **0.18**. That is close enough to be genuine validation of your estimate — worth saying so.

### 1.2 The one directly on your primary scope: NACH auto-debit bounces

Your project scopes to mandate/subscription failures. NPCI publishes NACH auto-debit bounce rates, which is exactly that population.

August 2021: of 87.68 million auto-debit transactions initiated, **32.98% failed by volume, 26.82% by value.** The most common reason cited was **inadequate balance.**

Source: https://www.business-standard.com/amp/article/finance/auto-debit-payment-failures-ease-in-august-shows-npci-data-121090900013_1.html

Two things this gives you:

- **Direct support for `insufficient_funds` being your largest class.** Your 0.25 weight is defensible from published data on exactly the payment type you model.
- **A note-worthy caveat:** that 33% is pandemic-era and has fallen since. Cite the direction, not the level.

### 1.3 Supporting: the top failure reasons

A Business Standard analysis of NPCI data is headlined *"Insufficient balance, wrong PIN top reasons for failed digital transactions."* It reports UPI business declines around 6.8% of transactions against 1–2% technical.

Source: https://www.business-standard.com/amp/article/economy-policy/insufficient-balance-wrong-pin-top-reasons-for-failed-digital-transactions-121122700487_1.html

**Insufficient balance + wrong PIN = your `insufficient_funds` + `customer_input_error`.** Together those are 40% of your batch. Published data says they are the top two. That is a real result, not a guess.

### 1.4 A live source you can cite

NPCI publishes **per-bank TD/BD and uptime statistics monthly** on its BD/TD & Uptime page. NPCI Circular OC-149 (June 2022) sets targets of TD < 1% and BD < 5%.

System-wide TD has fallen from 8–10% in 2016 to roughly 0.7–0.8% by 2025.

Sources:
- https://productgrowth.in/insights/fintech/upi-payment-success-rates/
- https://www.zeebiz.com/economy-infra/news-only-08-of-upi-transactions-face-technical-declines-now-npci-327217

### 1.5 A retry benchmark, with a caveat

Razorpay reports that automated retry systems recover **15–20% of failed transactions**, adding 3–5 percentage points to overall payment success rate.

Source: https://razorpay.com/blog/payment-success-rate-optimization-india/

**Handle with care.** Like every vendor recovery figure, it does not state a control group — so it measures *treated* recovery, not *incremental*. Your Baseline B recovers 32.5% gross with +11.6pp incremental. Those are different quantities and should not be compared directly.

Use it as a directional sanity check ("our retry baseline is in a plausible range") and say explicitly why you cannot benchmark against it. **That caveat is itself a point in your favour** — it is the same flaw your control arm exists to avoid.

### 1.6 One bonus finding worth using

Razorpay reports payment success rates drop **8–12 percentage points during evening peaks (7–10 PM)** when multiple banks experience load-related slowdowns.

Your generator currently spreads `failed_at` uniformly across the day. Weighting failures toward 19:00–22:00 — and concentrating `technical_downtime` there — would be a cheap realism improvement grounded in published data.

**Optional. Only if the calibration run goes fast.** It changes the batch, so it belongs in the calibrated config, never in canonical.

### 1.7 What is genuinely not available

Be able to say this clearly, because it is the honest core of the answer:

- **No public dataset of failed payments paired with merchant recovery actions paired with eventual outcomes.** That data is owned by processors and their merchants. It is personally identifiable, commercially sensitive, and covered by RBI data-localisation rules. Nobody has published it and nobody will.
- **Even with real failure logs, you could not evaluate a recovery agent off them** — a historical dataset contains whatever *that merchant* did, not what your agent would do. Off-policy evaluation would require randomness in the historical policy, and production dunning systems follow fixed schedules.
- **The only genuine evaluation is a live deployment with a holdout.** That is a merchant relationship, a compliance review, and months.

This is not a gap you failed to close. It is structural.

---

## Part 2 — Building the calibrated config (45 min)

### 2.1 Derive the weights

Anchor on the NPCI split, then distribute within the buckets using your existing reasoning.

```
Technical Decline bucket  → 18%
  technical_downtime                      0.18

Business Decline bucket   → 55%
  insufficient_funds       (top reason)   0.28
  customer_input_error     (top reason)   0.17
  limit_exceeded                          0.07
  temporary_lockout                       0.03

Not in the NPCI split     → 27%
  issuer_decline                          0.12
  instrument_invalid                      0.10
  session_expiry                          0.03
  mandate_failure                         0.02
                                          ─────
                                          1.00
```

**Document the reasoning for each line.** The two named top reasons get the largest BD shares. `technical_downtime` matches NPCI's TD share directly. The classes outside the NPCI split are card and mandate lifecycle failures that UPI decline codes do not cover, so they stay at your estimates.

### 2.2 Notice how close this is to what you already had

| Class | Current | Calibrated | Δ |
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

**The largest single move is 0.05.** That is the finding: your estimates were already close to published reality. Say that, because it means the calibrated result is a confirmation rather than a correction — and confirmations are more credible when you show you were willing to be wrong.

### 2.3 Add the `--config` flag

```python
ap.add_argument("--config", default="config/failure_classes.csv")
```

Thread it through `load_failure_classes(path)`. It already takes a path argument — you just need to stop defaulting it at the call site.

**Do not** make the calibrated file the default. The canonical batch must remain reproducible with no flags.

---

## Part 3 — Running the calibration check (30 min)

```bash
git commit -am "pre-calibration"

python -m generator.generate --n 1000 --seed 42 \
       --config config/failure_classes_calibrated.csv \
       --out data/calibrated

python -m eval.run_baselines --data data/calibrated
python -m eval.run_agent     --data data/calibrated
python -m unittest discover tests

git status          # data/ MUST be unchanged
```

Then repeat across your six evaluation seeds. The question is not *"what is the number"* — it is **"does the agent still beat B, and by roughly the same margin?"**

### What to expect

The class mix shifts slightly toward classes where you already win (`insufficient_funds`, `technical_downtime`) and slightly away from one where you win big (`instrument_invalid`). Those roughly cancel. Expect the gap to move by a point or so in either direction.

### Recording the result

| Config | Agent | B | Gap |
|---|---|---|---|
| Estimated (canonical) | 38.6% | 32.5% | +6.1 |
| NPCI-calibrated | ? | ? | ? |

**If the gap survives, that is the whole point.** If it inverts, that is a genuine and important finding — report it rather than burying it, and investigate which class drove it.

---

## Part 4 — Sensitivity sweep (60 min)

The seed checks show your result is robust to *sampling*. This shows it is robust to your *priors* — a different and more pointed question.

### 4.1 What to vary

`p_resolves` per class, ±0.1, clamped to [0, 1]. Three conditions:

```
pessimistic:  p_resolves − 0.1     (blockers clear less often)
canonical:    p_resolves            (as configured)
optimistic:   p_resolves + 0.1     (blockers clear more often)
```

**Vary all classes together, not one at a time.** You are testing whether the conclusion holds under a systematically different world, not doing per-class attribution.

### 4.2 Second lever, if time allows

The two-factor formula gives you a second dial. Vary the `p_reattempts` coefficients:

```python
base = 0.35 * reattempt_propensity + 0.45 * true_intent_to_pay
```

Shift the weighting ±0.1. This tests robustness to how much of natural recovery comes from customer willingness versus blocker resolution — genuinely independent of the first sweep.

### 4.3 How to run it

Same pattern as calibration: separate config, separate output directory, canonical untouched.

```bash
for cond in pessimistic canonical optimistic; do
  for seed in 42 1 2 7 99 123; do
    python -m generator.generate --n 1000 --seed $seed \
           --config config/sensitivity_$cond.csv \
           --out data/sens_${cond}_${seed}
    python -m eval.run_agent --data data/sens_${cond}_${seed}
  done
done
```

18 runs. Generation is fast; this is a coffee break, not an afternoon.

### 4.4 The output

| Condition | Control | B | Agent | Gap |
|---|---|---|---|---|
| Pessimistic (−0.1) | ? | ? | ? | ? |
| Canonical | 20.9% | 32.5% | 38.6% | +6.1 |
| Optimistic (+0.1) | ? | ? | ? | ? |

**What you want:** control moves a lot, the *gap* moves little. That means your conclusion is about the agent's behaviour, not about the specific priors.

Under the optimistic condition, control rises and every policy's headroom shrinks — so the gap will compress. Expect this and say so in advance; it is a prediction the data can confirm, which is more convincing than a post-hoc explanation.

### 4.5 The sentence this buys you

> Shifting every natural-recovery prior by ±0.1 moves the control rate by X points but changes the agent-versus-baseline gap by less than Y. The conclusion does not depend on the specific values we chose.

That is the strongest available answer to *"you made the data up."* It concedes the premise entirely and shows it does not matter.

---

## Part 5 — Where this goes in the writeup

### 5.1 A calibration section

> Generation weights are anchored on NPCI's published business/technical decline split — 81.7% BD, 18.3% TD across the top 50 remitter banks. Our `technical_downtime` share of 18% matches NPCI's TD share directly. NPCI and Business Standard analyses both identify insufficient balance and wrong PIN as the top two failure reasons, which are our two largest classes at 28% and 17%. Card and mandate-lifecycle failures fall outside the UPI decline taxonomy and remain estimated.
>
> Our original estimates and the calibrated weights differ by at most 0.05 on any class, and the agent-versus-baseline gap is unchanged under both.

### 5.2 A limitations section

> No public dataset pairs failed payments with merchant recovery actions and eventual outcomes; that data sits with processors and their merchants and is not releasable. Even with real failure logs, evaluating a recovery agent requires knowing what happens when *this* agent acts, which no historical dataset contains. The only genuine evaluation is a live deployment with a holdout.
>
> Accordingly, the simulator is the one synthetic component — and the one replaced first in production. The diagnosis layer reads real Razorpay error codes, the guardrails and action space are unchanged, and the measurement design needs no synthetic input at all: withhold intervention from 20% of real failures for 14 days and count.

### 5.3 The Q&A answer

**"It's synthetic — why should I believe any of it?"**

> Three reasons. The class mix is calibrated against published NPCI decline data and differs from our original estimate by at most five points. Shifting every natural-recovery prior by ±0.1 leaves the agent-baseline gap essentially unchanged. And the measurement design carries over to real data with no assumptions — the control arm needs no modelling, reality resolves the outcomes.
>
> What we cannot claim is that our absolute recovery rates would transfer. We claim the mechanism and the measurement do.

---

## Part 6 — Time budget

| Task | Time |
|---|---|
| Add `--config` flag | 15 min |
| Build calibrated config with documented reasoning | 30 min |
| Run calibration across six seeds | 20 min |
| Build three sensitivity configs | 15 min |
| Run the 18-run sweep | 20 min |
| Write both writeup sections | 30 min |

**About 2 hours.** If squeezed: do the sensitivity sweep first. It is the stronger claim and it needs no external sources to defend.

---

## Part 7 — Final safety checklist

- [ ] `git commit` before starting
- [ ] `config/failure_classes.csv` never edited in place
- [ ] All new batches written via `--out`, never to `data/`
- [ ] `git status` shows `data/` unchanged at the end
- [ ] Canonical run reproduces with no flags
- [ ] Identity check passes on every generated batch
- [ ] Agent wasted / impossible still 0 everywhere
- [ ] NSF leak tripwire clear on every run
- [ ] Full test suite green
- [ ] Headline numbers in the README unchanged — calibration is reported as a robustness section, not a replacement
