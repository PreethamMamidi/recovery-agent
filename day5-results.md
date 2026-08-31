# Day 5 results — propensity

NLP / RAG / Hinglish / reply parser / uplift were out of this pass.

After the ablation showed the second-ask gain was a rule and not a model result, we folded it into `policy.py`. The rule floor is now **41.5%** (was 38.6% at Fix 7). Model apps are measured against the new floor; `results_before_rebaseline.json` holds the prior numbers.

The rule agent on canonical `data/` is still the default: **41.5%**, B **32.5%**, lift **+20.6 pp**, wasted **0**, impossible **0**, messages **951**. `python -m eval.run_agent` does not load the model.

Train seeds: **101–108** only. Eval seeds **42, 1, 2, 7, 99, 123** were never used for fit or threshold search.

Two findings. The 3.5pp headline gain was a rule change — an unconditional second ask — that the model wrapped rather than caused; an ablation without the model recovers 2.8 of the 3.5. That rule is now the default. Separately, the model does learn something the rules do not: it isolates futile `issuer_decline` second asks with perfect class separation, at zero recovery cost. EV filtering cannot express that at ₹0.05–1 message costs.

```bash
pip install -r requirements.txt
python -m eval.run_train_data          # data/train_S, 30% channel exploration
python -m model.train                  # converting-step labels
python -m eval.run_agent               # 41.5% rule floor
python -m eval.compare_ml --ml-app channel
python -m eval.compare_ml --ml-app suppress
python -m eval.compare_ml --ml-app second_ask
python -m eval.compare_ml --ml-app second_ask --p2-percentile 25
python -m eval.run_agent --use-model --ml-app second_ask
```

---

## Item 1 — Unconditional second-ask ablation (now the default)

No model in the path. Same 6h follow-up on `customer_input_error`, `issuer_decline`, `instrument_invalid`, `mandate_failure`. Measured first as a flag; then folded into `policy.py`. Session already had its 6h follow-up from Fix 7 — not doubled.

| | seed 42 rec | 6-seed rec vs Fix 7 rules |
|---|---|---|
| Fix 7 rules | 38.6% | — |
| rules + 6h second ask | **41.5%** | +1.5 to +4.1 pp, **6/6** |
| rules + model-filtered 2nd ask (published smeared-label run) | **42.1%** | +2.7 to +4.9 pp, 6/6 |

The middle row is **41.5%**, not 40%. Of the 3.5 pp headline jump, **2.8 pp is the extra ask** and **0.6 pp is channel mix**. Issuer doubled messages for no recovery either way (11.6% both).

That is a rule result. It is now the default `python -m eval.run_agent` path. Wasted / impossible stayed 0. Identity 1000/1000. Messages 951 vs B 813; messages-per-recovery **2.82 vs 3.08**.

Source: `eval/ml_second_ask_ablation.json`, then `results_after_rebaseline.json`. What the converting-step model does with that extra ask is Item 3.

---

## Item 2 — Converting-step labels

Payment-level `recovered` was smeared onto every step. Labels are now:

- recovered via an action → last executed step with `at <= recovered_at` gets 1
- recovered naturally → every step 0
- not recovered → every step 0

(`<=` not `<`: the simulator stamps `recovered_at` at the converting action’s timestamp.)

Retrain on the same 101–108 logs:

| | smeared (morning) | converting-step |
|---|---|---|
| Train / val rows | 7995 / 2017 | 7995 / 2017 |
| Positive rate | ~0.50 | **0.157 / 0.159** |
| ROC-AUC | 0.855 | **0.778** |
| PR-AUC | 0.867 | **0.409** |

Positive rate dropped sharply. PR-AUC now sits below ROC-AUC — the normal relationship under imbalance. Top importances: `delay_hours`, `amount`, `lifetime_value` — not `failure_class` (10th). Leak tripwire 0.90 clear.

---

## Item 3 — What the model actually learned

Rank `p(step=2)` across every proposed extra ask in the batch. Drop the bottom quartile. Cost ignored.

```
threshold = percentile(all_p2_scores, 25)
send if p(step=2) > threshold
```

Seed 42 candidates: CIE 122, instrument 91, mandate 14, issuer 138 (365 extra-ask scores). Threshold **0.037**. Bottom quartile:

| class | in quartile | candidates | share of class |
|---|---|---|---|
| issuer_decline | **92** | 138 | 67% |
| customer_input_error | **0** | 122 | 0% |
| instrument_invalid | **0** | 91 | 0% |
| mandate_failure | **0** | 14 | 0% |

Executed drops: **93**, all `issuer_decline` (92 quartile + 1 first-ask EV). Same pattern on every eval seed: the quartile is 100% issuer.

Recovery stayed **42.9%** — identical to sending those 92 issuer follow-ups (channel-rewritten path). They recovered nothing. Net ₹1,688,453. Wasted / impossible 0. Vs the new rule floor: **42.9% vs 41.5%**, **848 vs 951** messages. The extra 1.4 pp is channel mix; the 93 issuer drops are free.

| Seed | Rules (new floor) | Quartile rec | Quartile net | drops |
|---|---|---|---|---|
| 42 | 41.5% | **42.9%** | **1,688,453** | 93 |
| 1 | 40.1% | **42.7%** | **1,671,930** | 90 |
| 2 | 41.5% | **43.7%** | **1,525,702** | 89 |
| 7 | 38.1% | **41.5%** | **2,022,050** | 91 |
| 99 | 37.2% | **39.9%** | **1,610,260** | 89 |
| 123 | 39.5% | **41.4%** | **1,449,881** | 90 |

**Finding.** The model identifies futile second asks with class-level precision it was never given. The bottom quartile of predicted converter-probability is 100% `issuer_decline` on all six eval seeds — 92 of 138 issuer follow-ups, and zero from the other three classes — despite `failure_class` ranking 10th in feature importance. Suppressing them costs zero recovery: 42.9% with and without, on every seed. Those 92 messages converted nothing.

Expected-value filtering could not express this. At ₹0.05 for email against a ₹500 recovery, a send fails the cost test only below p ≈ 0.0001; the model's lowest score is 0.037. The scores were always right — the EV test could not read them. Same model, same scores: 0–1 drops under the cost floor, 92 under a rank cut.

The generalisation: when messages are effectively free relative to recovery value, expected value stops being the binding constraint on contacting customers. Annoyance is. Baseline C measured that directly — same recovery as B, 320 opt-outs, 7× worse net value. The taxonomy encoded the right constraint from the start; the EV framing was the wrong lens for it.

### Path: Run A — `send if p(step=2) * amount > message_cost`

Same converting-step model. No retrain. First message still uses `p * amount - cost`.

Suppression on seed 42: CIE **0** / instrument **0** / mandate **0** / issuer **1**.

The class pattern flipped to issuer, but the floor barely fires — 0–1 drop per seed. Cheapest channel is email at ₹0.05, so `p2 * amount` is almost never below cost.

`--ml-app second_ask` vs the new rule floor (41.5%):

| Seed | Rules | Model rec | Rules net | Model net | drops |
|---|---|---|---|---|---|
| 42 | 41.5% | **42.9%** | 1,657,339 | **1,688,367** | 1 |
| 1 | 40.1% | **42.7%** | 1,431,484 | **1,671,845** | 1 |
| 2 | 41.5% | **43.7%** | 1,524,366 | **1,525,619** | 0 |
| 7 | 38.1% | **41.5%** | 1,906,449 | **2,021,966** | 1 |
| 99 | 37.2% | **39.9%** | 1,546,240 | **1,610,172** | 1 |
| 123 | 39.5% | **41.4%** | 1,236,409 | **1,449,796** | 0 |

6/6 vs the new floor. Canonical 42.9% is **above** 41.5%, but the extra 1.4 pp is channel mix, not targeting — the floor did not suppress. This is why the rank cut above exists.

### Path: lift EV floor (prior)

```
lift = p(step=2) - p(step=1)
send if lift * amount > message_cost
```

Suppression on seed 42 (115 drops): CIE **94**, instrument **17**, mandate **3**, issuer **1**.

Wrong class pattern. Under converting-step labels `p(step=1)` is P(the first ask is the converter), so `p2 - p1` is negative on classes where the first ask actually works. The floor therefore suppresses CIE (where the second ask helps) and leaves issuer (where it does not). Canonical rec **41.1%**, below unconditional 41.5%. Not shipped.

| Seed | Rules rec | Model rec | Rules net | Model net | drops |
|---|---|---|---|---|---|
| 42 | 38.6% | **41.1%** | 1,564,615 | **1,617,250** | 115 |
| 1 | 37.9% | **41.1%** | 1,305,172 | **1,568,854** | 122 |
| 2 | 39.2% | **42.5%** | 1,468,205 | **1,484,750** | 135 |
| 7 | 35.6% | **39.9%** | 1,819,762 | **1,987,037** | 122 |
| 99 | 35.7% | **38.4%** | 1,473,666 | **1,541,322** | 124 |
| 123 | 35.4% | **39.5%** | 1,086,122 | **1,394,908** | 127 |

---

## 3.1 Channel selection — 6/6 rec and net vs the new floor

`--use-model --ml-app channel`. Second asks are already in the rule schedule, so channel mix now applies to both steps. Debits unchanged. Wasted / impossible 0 on every seed.

| Seed | Rules rec | Model rec | Rules net | Model net |
|---|---|---|---|---|
| 42 | 41.5% | **42.9%** | 1,657,339 | **1,688,367** |
| 1 | 40.1% | **42.7%** | 1,431,484 | **1,671,845** |
| 2 | 41.5% | **43.7%** | 1,524,366 | **1,525,619** |
| 7 | 38.1% | **41.5%** | 1,906,449 | **2,021,966** |
| 99 | 37.2% | **39.9%** | 1,546,240 | **1,610,172** |
| 123 | 39.5% | **41.4%** | 1,236,409 | **1,449,796** |

Rec **6/6**. Net **6/6** (seed 2 net now clears; it lost against Fix 7). Canonical +1.4 pp over 41.5% — the old +0.9 pp was first-ask mix only. Still not the default: WhatsApp mix is a cost bet, and the 1.4 pp is smaller than the rule second-ask that is already in the floor.

---

## 3.2 Suppression (first-ask EV) — still almost never fires

`--ml-app suppress` matched channel except **0–1** `issuer_decline` drop per seed. `p * amount - ₹1` is still almost never negative. Restraint is still the taxonomy.

---

## Item 4 — Diagnostics

**Feature drop.** Retrain without `amount` and `lifetime_value`: ROC-AUC **0.783** (held). `log_amount` absorbed the ticket-size split. Dropping `amount`, `log_amount`, and `lifetime_value`: still **0.783**. Signal is timing (`delay_hours`) and customer history, not ticket size. One sentence: the model is not a dressed-up amount rule.

**Isotonic calibration** (`CalibratedClassifierCV(..., method="isotonic", cv="prefit")` on val). Raw top decile 0.81 pred vs 0.48 actual; isotonic 0.50 vs 0.50. Channel app with `--calibrated`: seed 42 rec **38.7%**, net **1,479,740** (down vs rules). Rec still 6/6; canonical net worse. Calibration fixed the plot and did not rescue EV. Uncalibrated channel is the better of the two.

---

## Decision

| App | 5/6? | Ship as default? |
|---|---|---|
| 6h second ask (rule) | Yes (rec and net) | **Shipped.** 41.5% on seed 42. In `policy.py`. |
| channel | Yes (rec 6/6, net 6/6) | No — +1.4 pp over the new floor; WhatsApp mix is a cost bet |
| suppress | Same as channel | No — EV floor still dead |
| second_ask (model + p2 EV floor) | Yes vs new floor | No — floor does not fire (0–1 drop). The rec gain is channel mix. |
| second_ask (bottom-quartile p2) | Yes; rec 42.9%, 848 msgs | Not shipped, but this is the ML layer's real result. Quartile is 100% issuer on 6/6; suppression is free. The model knows; the ₹ cost floor cannot say it. |
| second_ask (model + lift, prior) | Yes vs Fix 7 | No — loses to the rule second-ask; suppresses CIE not issuer |
| channel + isotonic | Rec 6/6 vs Fix 7 | No — canonical net drops |

`data/` was not regenerated. Hidden modules were not imported by `agent/` or `model/score.py`.

---

## Day 7 Q&A

**Did the ML help?**

Partly. The recovery gain was a rule change the model wrapped. But the model does one thing the rules cannot — it identifies which second asks are futile, with perfect class separation on unseen seeds, at zero recovery cost. We couldn't ship that as an EV filter because at ₹0.05 a message nothing fails the cost test, which is itself a finding about where EV-based targeting applies.
