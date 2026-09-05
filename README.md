# Recovery Agent

A decision layer for failed recurring payments. Instead of the same 24-hour retry and SMS for every decline, the agent diagnoses the failure and chooses a bounded next action: wait, retry, request an instrument update, send a payment link, or stop.

It is scored against a do-nothing control group and class-blind baselines on recovery rate, wasted and impossible debits, opt-outs, and net rupees. The agent never reads simulator-only state.

---

## Results

Canonical batch: 1,000 payments, seed 42, 14-day window. Treatment n = 813. Live files are in `results/`.

| Policy | Recovery | Lift vs control | Wasted | Impossible | Messages | Opt-outs | Net ₹ |
|---|---|---|---|---|---|---|---|
| Control (no contact) | 20.9% | — | 0 | 0 | 0 | 0 | 77,878 |
| A — retry at 24h | 26.8% | +6.0 pp | 142 | 417 | 0 | 0 | 1,072,537 |
| B — SMS + 3 retries | 32.5% | +11.6 pp | 426 | 1,094 | 813 | 0 | 1,377,187 |
| C — aggressive dunning | 32.2% | +11.4 pp | 569 | 1,427 | 3,297 | 329 | 85,761 |
| **Agent** | **41.6%** | **+20.7 pp** | **0** | **0** | **949** | 0 | **1,657,412** |

**Wasted** — a debit was sent on a class where the same instrument can never succeed (`instrument_invalid`, `issuer_decline`, `mandate_failure`).  
**Impossible** — a debit was proposed with no active mandate, so it is never sent.

Costs were fixed in `config/costs.py` before seeing results: ₹2 / debit, ₹0.20 / SMS, ₹1 / WhatsApp, ₹0.05 / email, new opt-out = 30% of lifetime value.

Where diagnosis changes the action versus B:

| Class | n | B | Agent | Policy |
|---|---|---|---|---|
| `instrument_invalid` | 91 | 1.1% | **23.1%** | Update request, no debit |
| `mandate_failure` | 14 | 14.3% | **42.9%** | Re-authorise; no debit proposed |
| `insufficient_funds` | 198 | 21.7% | **29.3%** | Nearest 1st / 7th / 15th (not hidden salary day) |
| `technical_downtime` | 115 | 77.4% | **85.2%** | Wait, then backoff; no SMS if mandate |
| `temporary_lockout` | 27 | 59.3% | **81.5%** | Exponential 2h / 8h / 32h |
| `session_expiry` | 66 | 34.8% | **43.9%** | Immediate, then +6h |
| `customer_input_error` | 122 | 40.2% | **53.3%** | Link, then +6h |
| `limit_exceeded` | 42 | **59.5%** | 54.8% | 00:30 ladder; B’s 24/72/120h still leads |

The agent does not beat B on every class. `limit_exceeded` is left as-is.

---

## How it works

```text
visible payment + customer
        → diagnose (error_reason)
        → policy (bounded schedule)
        → guardrails (may reject; never execute a blocked action)
        → simulator (eval only; hidden state)
        → recovery, cost, net value
```

Offers in customer copy are retrieved from `config/merchant_policy.md` (failure class, amount band, customer tier). No matching chunk → no-offer template. The agent cannot invent a waiver.

**Identity check.** `respond(..., actions=[])` returns the row already written in `ground_truth.csv`. It does not re-roll. Must hold on all 1,000 payments.

---

## Data split

The agent may read only what a merchant can see.

| File | Reader |
|---|---|
| `data/payments_visible.csv` | Agent |
| `data/customers_visible.csv` | Agent |
| `data/payments_hidden.csv` | Simulator and eval only |
| `data/customers_latent.csv` | Simulator and eval only |
| `data/ground_truth.csv` | Simulator and eval only |

`agent/` does not import `simulator/`, `generator.latents`, or `generator.natural_recovery`. Hidden fields such as `salary_day` and `true_intent_to_pay` do not exist in production.

Natural recovery in the generator is:

```text
P(recovers unprompted) = P(problem resolves) × P(customer re-attempts)
```

Resolution uses hidden timestamps where they exist (downtime end, lockout end, salary day). Re-attempt propensity is a property of the person. Outcomes are measured within 14 days.

---

## Quick start

```bash
python -m pip install -r requirements.txt
python -m eval.run_baselines
python -m eval.run_agent
python -m unittest discover tests
```

Regenerate the batch (rewrites `data/`):

```bash
python -m generator.generate --n 1000 --seed 42
```

---

## Dashboard

Precompute once, then Streamlit reads `results/`. It does not re-run the batch.

```bash
python -m eval.precompute_dashboard
streamlit run dashboard/app.py
```

Tabs: batch results, payment timeline, restraint, efficiency, try-it sandbox.

Useful timeline ids: `PAY_00210` (recovery), `PAY_00071` (opt-out gate), `PAY_00026` (downtime wait), `PAY_00002` (high-value NSF).

```bash
python -m agent.messaging --demo rogue     # validator rejects an invented offer
python -m eval.run_agent --trace PAY_00071
python -m agent.messaging --demo no-index  # retrieval fails closed
```

---

## API

Same agent as the dashboard, over HTTP. The dashboard does not call this service.

```bash
uvicorn api.main:app --reload
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/metrics` | Headline metrics from `results/agent.json` |
| `GET` | `/payments/{id}` | Decision chain from `results/audit.db` |
| `POST` | `/webhook` | Razorpay `payment.failed` → diagnose → policy → gate |

Set `RAZORPAY_WEBHOOK_SECRET` in `.env` (webhook secret, not the API key secret). Signature is HMAC-SHA256 of the raw body. Unknown `error_reason` returns 200 with `failure_class: "unknown"` so Razorpay does not retry forever.

```bash
python -m unittest tests.test_api
```

---

## Optional propensity model

The published agent is the rule path (`41.6%`). A LightGBM model can score `P(this step recovers | visible features, action, channel)` for channel choice or dropping a futile second ask. It is off by default.

```bash
python -m eval.run_agent                                 # 41.6%
python -m eval.run_agent --use-model --ml-app second_ask # 43.9% on this batch
```

What we claim about the model is in [ml-model.md](ml-model.md).

---

## Robustness

Headline numbers stay attached to `data/` (seed 42). Calibration and sensitivity write to separate directories and do not retune policy.

On a calibrated class mix (NPCI-anchored business vs technical decline), the agent still beats B on every seed (mean gap +5.7 pp). Shifting `p_resolves` or re-attempt coefficients by ±0.1 leaves the agent–B gap in a similar range.

```bash
python -m eval.run_calibration
python -m eval.run_sensitivity
python -m eval.check_seeds
```

---

## Compliance notes

**RBI e-mandate.** Recurring AFA skip up to ₹15,000 for a general subscription merchant (`AFA_THRESHOLD = 15000`). Pre-debit notice is logged as audit, not used as a conversion lever. Debits scheduled with less than 24 hours’ notice are flagged.

**TRAI.** A no-offer recovery message is service-class (24/7, DND-exempt). Mixing a permitted offer into the body reclassifies it as promotional (09:00–21:00, DND-scrubbed).

---

## Repository

| Path | Role |
|---|---|
| `agent/` | Diagnose, policy, guardrails, messaging |
| `baselines/` | Fixed retry, SMS + retries, aggressive dunning |
| `simulator/` | Hidden response function (eval only) |
| `generator/` | Synthetic batch |
| `eval/` | Scoring, robustness, dashboard precompute |
| `model/` | Optional propensity layer |
| `dashboard/` | Streamlit UI |
| `api/` | HTTP + webhook |
| `config/` | Costs, taxonomy, merchant policy |
| `data/` | Canonical visible / hidden / ground-truth CSVs |
| `results/` | Precomputed eval for the dashboard |
| `tests/` | Unit and integration tests |
