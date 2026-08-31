# Day 5 Plan — Propensity, Channel Selection, and Messaging

**Recovery Agent — Razorpay Buildathon Track 03**

Plan written before the Day 5 eval. The EV floor in §3.2–3.3 predicted suppression would work via `p * amount > cost`. It did not: at these message costs (email ₹0.05, WhatsApp ₹1) almost nothing fails the test. What the model actually learned is in `day5-results.md`. After the ablation, the 6h second ask was folded into `policy.py`; the live rule floor is **41.6%**. 41.5% / 951 messages was the same rules with a blanket 21:00–09:00 quiet-hours block. TRAI exempts service-class messages from that window; removing it recovered one additional payment.

Two halves. Morning is the ML decision layer. Afternoon is NLP and RAG.

**Governing rule for the day:** the rule-based agent at 41.6% is your floor and your fallback. Every ML addition must beat it on the same seeds or it does not ship. *"We tried the model, it did not beat the rules, here is the evidence"* is a strong result — it demonstrates judgment. A shaky model presented as a win is not.

---

## Where the remaining signal actually is

The rule-based policy is class-level. It treats all 198 NSF customers identically, all 128 input-error customers identically. But the latents say customers differ:

| Latent | Visible proxy | What a model could learn |
|---|---|---|
| `channel_responsiveness` | `preferred_channel` | Which channel converts *this* customer |
| `reattempt_propensity` | `past_payment_count`, `past_failure_count`, `tenure_months` | Who recovers unprompted — do not spend on them |
| `true_intent_to_pay` | payment history ratio | Who is reachable at all |
| `annoyance_threshold` | `opted_out`, contact history | Who to stop asking |

The session ablation already proved a **second ask converts** (+4.3 to +11.9pp across six seeds). That turns *"who deserves a second ask"* from a hypothetical into a live prediction problem.

**That is the day's thesis: the rules decide *what* to do; the model decides *who* to do it to and *how*.**

---

## Morning — Part 1: Training data (60–90 min)

### 1.1 What you are predicting

Start with **propensity**: given the visible features and a proposed action, will this payment recover?

```
P(recover | visible features, action type, channel, attempt number)
```

Not uplift yet. Uplift needs the control arm to estimate a *difference*, and 187 controls is thin. Propensity first, uplift as the stretch (§ 4).

### 1.2 Generating the training set

You need history. Run the existing agent over **several seeds** and log every decision with its outcome.

```bash
for s in 101 102 103 104 105 106 107 108; do
    python -m generator.generate --n 1000 --seed $s --out data/train_$s
    python -m eval.run_agent --data data/train_$s --log-decisions
done
```

Eight seeds × ~800 treatment payments ≈ **6,400 rows**. Comfortable for LightGBM.

**Use seeds you have never evaluated on.** Do not train on 42, 1, 2, 7, 99, or 123 — those are your test seeds and you have already looked at them repeatedly. Keeping them clean is what makes the final comparison meaningful.

### 1.3 Feature list — visible only

```python
FEATURES = [
    # payment
    "amount", "log_amount", "method", "failure_class",
    "has_active_mandate", "attempt_number",
    "days_until_due", "failed_hour", "failed_day_of_month",
    # customer
    "tenure_months", "past_payment_count", "past_failure_count",
    "failure_ratio",              # past_failure / past_payment
    "payments_per_month",         # past_payment / tenure
    "lifetime_value", "preferred_channel", "opted_out",
    # action being scored
    "action_type", "channel", "delay_hours", "step_index",
]
```

**Hard rule:** nothing from `generator/latents.py`, `generator/natural_recovery.py`, `payments_hidden.csv`, or `ground_truth.csv`. A model that reads `true_intent_to_pay` will score beautifully and be worthless — that column does not exist in production.

Add a test:

```python
def test_model_features_are_visible_only():
    hidden = set(latent_columns) | set(payment_hidden_columns)
    assert not (set(FEATURES) & hidden)
```

`failure_ratio` and `payments_per_month` are the two derived features worth the effort — they are the sharpest proxies for `true_intent_to_pay`, which is the latent driving the most variance.

### 1.4 Splitting

**Split by customer, not by row.** The same customer appears on multiple payments; a random row split leaks their behaviour across the boundary.

```python
train_customers, val_customers = split(unique_customer_ids, 0.8)
```

Then hold the six evaluation seeds out entirely as a third, untouched set.

---

## Morning — Part 2: The model (60 min)

### 2.1 Train

```python
import lightgbm as lgb

model = lgb.LGBMClassifier(
    n_estimators=300,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=30,
    class_weight="balanced",     # recovery is ~35%, not severe, but be explicit
)
model.fit(X_train, y_train, eval_set=[(X_val, y_val)], eval_metric="auc")
```

### 2.2 Report honestly

- **PR-AUC**, not just ROC-AUC
- Calibration plot — predicted vs actual in deciles. If the model says 0.3 and 30% recover, it is usable for expected-value decisions. If it is badly calibrated, EV arithmetic is meaningless
- Feature importances

### 2.3 Sanity checks before you use it

| Check | What failure means |
|---|---|
| AUC > 0.90 | Almost certainly a leak. Hunt it |
| `failure_class` dominates importance | The model is just relearning the taxonomy — no new signal |
| `preferred_channel` has near-zero importance | The channel signal is not being learned; check encoding |
| Calibration badly off | Do not use the scores for EV decisions |

**An AUC around 0.70–0.80 is the realistic target.** Higher should make you suspicious, not pleased.

---

## Morning — Part 3: Using the scores (90 min)

Three applications, in order of expected payoff. Ship them **one at a time**, measuring each.

### 3.1 Channel selection (highest expected payoff)

Currently: fixed SMS then WhatsApp. Instead, score each channel and pick the best expected value.

```python
def choose_channel(payment, customer, step):
    best, best_ev = None, -inf
    for ch in ("sms", "whatsapp", "email"):
        p = model.predict_proba(features(payment, customer, ch, step))[1]
        ev = p * payment.amount - CHANNEL_COST[ch]
        if ev > best_ev:
            best, best_ev = ch, ev
    return best
```

Cost matters here: WhatsApp is ₹1 against SMS at ₹0.20, so for a ₹300 payment the higher conversion may not justify the cost, while for ₹40,000 it obviously does. **The rules cannot express that trade-off; the model can.**

Expect the mix to shift away from the flat `preferred_channel` mapping.

### 3.2 Suppression — do not contact when EV is negative

```python
if best_ev < 0:
    return None      # no message. Log the decision and the reason.
```

This is the restraint story becoming quantitative. Right now you avoid messaging downtime customers because the taxonomy says so. With the model you can also skip messaging customers the model says will not convert — a per-customer decision the rules cannot make.

**Watch:** recovery may dip slightly while net value rises. That is the correct trade, but report both. And check the suppression is not concentrated in one class — if it suppresses 90% of one class, the model has learned the taxonomy rather than the customer.

### 3.3 Second-ask targeting

The session ablation showed a second ask converts. But it converts for *some* people.

```python
send_second_ask = model.predict_proba(features(..., step=2))[1] * amount > cost
```

Apply across all customer-action classes, not just session. This is where the "who deserves a second ask" question gets answered with data.

### 3.4 After each of the three

```bash
python -m eval.run_agent          # canonical
python -m eval.check_seeds        # all six
```

**Accept only if it holds on 5/6 or better.** Keep the rule-based path behind a flag so you can A/B them in the demo — showing rules vs model side by side is a stronger slide than the model alone.

---

## Afternoon — Part 4: NLP and RAG (2–3 hours)

### 4.1 The policy corpus for RAG

Write a small merchant policy document — 1–2 pages, realistic:

- what discounts or waivers may be offered, and their ceilings
- tone guidance per customer tier
- claims that must never be made ("your account will be closed")
- escalation criteria
- regulatory constraints (quiet hours, opt-out language)

Index with FAISS or Chroma. Retrieve on `(failure_class, customer_tier, amount_band)`.

**This is not decoration.** It is what keeps offers *bounded*. An agent that can invent a 30% discount is a liability; one that can only offer what policy retrieval returns is a product. Say exactly that in the demo.

### 4.2 Message generation inside DLT-style templates

TRAI's DLT framework means commercial SMS uses pre-registered templates. So the LLM **fills slots**, it does not free-generate:

```
TEMPLATE_004: "Hi {name}, your payment of Rs {amount} to {merchant}
               didn't go through. {reason_phrase} Complete it here: {link}"
```

The model generates `reason_phrase` only, grounded in the retrieved policy and the diagnosed class.

Two benefits: it is compliant, and it is a **naturally bounded generation task** — exactly the shape the track's brief asks for. A constraint that improves the architecture rather than limiting it.

### 4.3 Hinglish variants

For SMS and WhatsApp, generate Hinglish alternates per template. Keep English for email.

Do **not** claim a measured lift from Hinglish unless you model channel-language response differences in the simulator — and if you add that, it is another parameter you invented. Better to present it as a capability, honestly labelled: *"generated and reviewed, not A/B tested."*

### 4.4 Reply parsing — promise-to-pay

```
"bhai 5 tarikh ko kar dunga"  →  {intent: promise, date: 5th}
"already paid"                →  {intent: dispute,  action: escalate}
"stop messaging"              →  {intent: opt_out,  action: suppress}
```

Then wire the outcomes:

- **promise** → cooling-off guardrail activates until that date; track kept vs broken
- **dispute** → escalate, do not auto-retry
- **opt_out** → immediate suppression

Your cooling-off guardrail is already built and currently unused. This is what activates it.

**Scope note:** your simulator does not generate replies. Either add a small reply generator (customers with high `true_intent_to_pay` sometimes promise) or demo the parser on hand-written examples and label it clearly as not measured. **Do not let a demo-only component leak into your headline metrics.**

---

## Part 5 — Stretch: uplift (only if the day goes fast)

Two-model approach:

```python
m_treated = fit(X[treated],   y[treated])
m_control = fit(X[control],   y[control])
uplift    = m_treated.predict_proba(X) - m_control.predict_proba(X)
```

Contact only where uplift > threshold.

**The blocker is sample size.** 187 controls per seed; even pooled across eight training seeds that is ~1,500 control rows to estimate a difference from. Thin.

If you attempt it, report the **Qini curve** (`causalml` or `scikit-uplift` have implementations — do not reimplement) and be explicit about the confidence.

**If it does not beat propensity, say so.** *"Uplift is the theoretically correct target but needs a larger control arm than one week of synthetic data supports; we used propensity and explain why"* is a better answer than a noisy uplift model presented as a win.

---

## Part 6 — Gates before Day 6

- [ ] Model uses visible features only, enforced by test
- [ ] Trained on seeds never used for evaluation
- [ ] Split by customer, not by row
- [ ] AUC in the 0.70–0.85 range (higher → hunt for a leak)
- [ ] Calibration checked before scores drive EV decisions
- [ ] Each of the three applications measured separately
- [ ] Every accepted gain holds on 5/6 seeds
- [ ] Rule-based path still runnable behind a flag
- [ ] Messages still generated inside approved templates
- [ ] Policy retrieval bounds every offer
- [ ] Wasted / impossible debits still 0
- [ ] Identity check still 1000/1000

---

## Part 7 — What could go wrong

| Signal | Meaning | Response |
|---|---|---|
| AUC > 0.90 | Leak | Check every feature against the hidden columns |
| Model does not beat rules | Class signal dominates individual signal | Keep rules, report the comparison. This is a legitimate finding |
| Gains on canonical seed only | Overfit | Reject. 5/6 or it does not ship |
| Suppression kills recovery | Threshold too aggressive | Tune on validation, never on the eval seeds |
| Net value up, recovery down | Correct trade-off | Report both, lead with net value |
| Everything improves a lot | Suspicious | Trace one payment end to end by hand |

---

## Part 8 — Time budget

| Block | Duration |
|---|---|
| Training data generation | 60–90 min |
| Model training + validation | 60 min |
| Channel selection | 45 min |
| Suppression | 30 min |
| Second-ask targeting | 30 min |
| Policy corpus + RAG index | 45 min |
| Template generation | 45 min |
| Reply parser | 45 min |
| Eval + seed checks | 45 min |

**Roughly 7 hours.** If time runs short, cut in this order: uplift → Hinglish → reply parser → second-ask targeting. **Never cut the seed checks or the leak tests.**

---

## The framing for the demo

> The rules decide *what* to do — that comes from the failure taxonomy and it is auditable. The model decides *who* to do it to and *how* — which channel, whether a second ask is worth it, when contacting has negative expected value. The rules are the floor; the model is the margin. If the model fails, the agent degrades to the rule-based policy rather than to nothing.

That last clause matters. **A bounded agent with a working fallback is a better story than a clever one without.**

**Did the ML help?** Partly. The recovery gain was a rule change the model wrapped. But the model does one thing the rules cannot — it identifies which second asks are futile, with perfect class separation on unseen seeds, at zero recovery cost. We couldn't ship that as an EV filter because at ₹0.05 a message nothing fails the cost test, which is itself a finding about where EV-based targeting applies. Full answer: `day5-results.md`.
