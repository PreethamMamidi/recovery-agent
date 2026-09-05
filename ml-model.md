# What we can say about the ML model

Rules are the product; ML is an optional layer on top.

---

## What it is

A **LightGBM propensity model**:

```text
P(this step recovers | visible features, proposed action, channel)
```

- Trained only on seeds **101–108**
- Eval seeds (including the headline batch **42**) were never used to fit
- Labels are **converting-step** (which action actually recovered), not “the whole payment recovered”
- ROC-AUC **0.778**, PR-AUC **0.409** — useful, not a leak (tripwire was 0.90)
- Features are merchant-visible only (amount, timing, history, LTV, action…)

Default `python -m eval.run_agent` **does not load the model**. Headline **41.6%** is the rule agent.

```bash
python -m eval.run_agent                                    # 41.6% rule floor
python -m eval.run_agent --use-model --ml-app second_ask    # 43.9% on data/
```

---

## What it is for (3 apps)

| App | What it does | Honest result |
|-----|----------------|---------------|
| **Channel** | Pick SMS / WhatsApp / email by score | Can lift live batch to **43.9%**. Not the default. |
| **EV suppress** | Drop a message if `p × amount` &lt; cost | **Almost no effect** — messages are ₹0.05–1, so almost everything passes. |
| **Second-ask cut** | Drop the weakest quartile of 6h follow-ups | **Same recovery, fewer messages** (837 vs 949). Bottom quartile is all `issuer_decline` — the model finds asks that never work. |

The big jump from ~38.6% → 41.6% was **folding a 6h second ask into the rules**, not the model. Ablation: most of that gain is the extra ask; the model wrapped it.

---

## What you can say in the form / video (30 seconds)

We trained a propensity model on held-out generate seeds to score *this action, this channel*. It is optional. The published agent is still the rules: 41.6%, zero wasted debits.

Where the model helps is not inventing policy. It down-ranks futile second asks — especially issuer declines — so we send fewer messages at the same recovery. Channel selection can add a couple of points. We did not make ML the default because the rule floor already beats the baseline, and we would not claim a model win for a change that is actually a rule.

---

## What not to say

- “The agent is an ML model” — it isn’t; diagnose → policy → gate is rules.
- “ML got us to 41.6%” — that floor is rules + second ask.
- “High AUC, so it knows who will pay” — 0.78 is modest; we checked it isn’t leaking hidden fields.
- “EV filtering saves money” — it doesn’t at these message costs.

**One line:** ML is a **ranker for optional sends**, not the decision engine.
