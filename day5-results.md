# Day 5 results — propensity (morning only)

NLP / RAG / Hinglish / reply parser / uplift were out of this pass.

The rule agent on canonical `data/` is still the default and the floor: **38.6%**, B **32.5%**, wasted **0**, impossible **0**. `python -m eval.run_agent` does not load the model.

Train seeds: **101–108** only. Eval seeds **42, 1, 2, 7, 99, 123** were never used for fit or threshold search.

```bash
pip install -r requirements.txt
python -m eval.run_train_data          # data/train_S, 30% channel exploration
python -m model.train                  # LightGBM as in day5-plan.md
python -m eval.compare_ml --ml-app channel
python -m eval.compare_ml --ml-app suppress
python -m eval.compare_ml --ml-app second_ask
python -m eval.run_agent --use-model --ml-app second_ask
```

---

## Model

`LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=30, class_weight="balanced")`.

| | |
|---|---|
| Train / val rows | 7995 / 2017 (750 customers, split by `customer_id`) |
| ROC-AUC | **0.855** |
| PR-AUC | **0.867** |

AUC sits 0.005 above the hoped 0.70–0.85 band and well below the 0.90 leak tripwire. Top importances are `amount`, `lifetime_value`, `failed_day_of_month` — not `failure_class` (9th) and not hidden columns. `log_amount` is 0 because trees already use `amount`. `channel` importance is low (58): even with 30% exploration the channel signal is weak.

Calibration (val deciles): under-confident at the bottom (pred 0.05 vs actual 0.10) and over-confident at the top (pred 0.99 vs actual 0.90). EV will slightly over-contact expensive channels. We still measured the three apps because this is not a leak.

Label caveat: one row per executed step, `recovered` is payment-level. Delayed credit assignment.

---

## 3.1 Channel selection — accepted on recovery (6/6 rec ≥ rules)

`--use-model --ml-app channel`. Debits unchanged. Wasted / impossible 0 on every seed.

| Seed | Rules rec | Model rec | Rules net | Model net |
|---|---|---|---|---|
| 42 | 38.6% | 38.6% | 1,564,615 | 1,508,484 |
| 1 | 37.9% | **38.4%** | 1,305,172 | **1,324,222** |
| 2 | 39.2% | **39.8%** | 1,468,205 | **1,481,892** |
| 7 | 35.6% | **37.6%** | **1,819,762** | 1,765,726 |
| 99 | 35.7% | **36.2%** | **1,473,666** | 1,440,677 |
| 123 | 35.4% | **36.2%** | 1,086,122 | **1,151,950** |

Five of six seeds have higher recovery. Canonical seed 42 is flat on recovery and **down** on net (over-confident WhatsApp vs ₹0.20 SMS). Do not replace the headline table with this app.

---

## 3.2 Suppression — no incremental effect

`--ml-app suppress` (channel + drop if EV < 0) matched the channel table on every seed.

`p * amount - message_cost` is almost never negative: amounts start near ₹99 and WhatsApp costs ₹1, so EV < 0 only for p ≲ 0.01. The model’s lowest-decile mean prediction is 0.05. Restraint here is still the taxonomy (no downtime-with-mandate SMS), not per-customer EV.

**Not shipped as a separate change.**

---

## 3.3 Second-ask targeting — accepted 6/6 on recovery and net

`--use-model --ml-app second_ask` proposes a 6h second message on `customer_input_error`, `issuer_decline`, `instrument_invalid`, `mandate_failure`, then keeps it iff EV > cost. In practice EV almost never fails, so this is close to “always send the extra ask” — the same mechanism the session ablation already showed.

| Seed | Rules rec | Model rec | Rules net | Model net |
|---|---|---|---|---|
| 42 | 38.6% | **42.1%** | 1,564,615 | **1,648,999** |
| 1 | 37.9% | **41.6%** | 1,305,172 | **1,450,397** |
| 2 | 39.2% | **42.9%** | 1,468,205 | **1,553,666** |
| 7 | 35.6% | **40.5%** | 1,819,762 | **1,900,038** |
| 99 | 35.7% | **38.4%** | 1,473,666 | **1,528,432** |
| 123 | 35.4% | **40.3%** | 1,086,122 | **1,410,506** |

Canonical `data/` with this flag: identity 1000/1000, wasted 0, impossible 0, NSF 29.8% (under 45%), downtime-with-mandate messages 0. Messages **947 vs B 813** — the Day 3 “stay under B” gate is rule-path only.

Where the extra asks landed on seed 42 (treatment):

| class | rules rec | model rec | msgs rules → model |
|---|---|---|---|
| `customer_input_error` | 41.0% | **54.9%** | 117 → 202 |
| `instrument_invalid` | 13.2% | **29.7%** | 90 → 166 |
| `mandate_failure` | 35.7% | **42.9%** | 13 → 23 |
| `issuer_decline` | 11.6% | 11.6% | 135 → 270 |

`issuer_decline` doubled messages for no recovery. The model did not suppress those — another EV-floor effect.

---

## Decision

| App | 5/6? | Ship as default? |
|---|---|---|
| channel | Yes (recovery) | No — canonical net drops |
| suppress | Same as channel | No — no incremental effect |
| second_ask | Yes (rec and net) | **No, but available behind the flag.** Lift is real; most of it is “add a second ask,” not fine-grained who-to-contact. Default stays the 38.6% rule agent. |

`data/` was not regenerated. Policy, simulator, and hidden modules were not imported by `agent/` or `model/score.py`.
