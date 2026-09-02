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

Canonical batch: seed 42, treatment n=813, `results_after_rebaseline.json`.

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
| **Agent** | **41.6%** | **+20.7 pp** | **0** | **0** | **949** | **2.81** | 0 | **1,657,412** |

Agent beats B on recovery and on efficiency. Zero wasted debits, zero impossible debits, **zero downtime-with-mandate messages**, 27 gate rejections, 46 high-value rows flagged for review (policy still runs).

Messages: agent **949** vs B 813. That exceeds B. The restraint claim was never “fewest messages” — it is messages-per-recovery and zero messages where they don’t help. Messages per recovery **2.81 vs 3.08**. Channel mix (preferred_channel, not a spray): 372 SMS / 463 WhatsApp / 114 email. B is 813 SMS. The 6h second ask on four customer-action classes is in `policy.py` (folded in after a no-model ablation showed it was a rule, not an ML result). Fix 7 sat at 38.6% / 639 messages; `results_before_rebaseline.json` holds that snapshot. 41.5% / 951 messages was the same rules with a blanket 21:00–09:00 quiet-hours block. TRAI exempts service-class messages from that window; removing it recovered one additional payment.

Where the taxonomy actually changes the action:

| class | n | B | agent | what changed |
|---|---|---|---|---|
| `instrument_invalid` | 91 | 1.1% | **23.1%** | update request, no debit; 6h follow-up |
| `mandate_failure` | 14 | 14.3% | **42.9%** | reauth; policy never proposes a debit, so the mandate gate does not fire |
| `insufficient_funds` | 198 | 21.7% | **29.3%** | nearest 1st/7th/15th (not `salary_day`) |
| `technical_downtime` | 115 | 77.4% | **85.2%** | wait then backoff; no SMS if mandate |
| `temporary_lockout` | 27 | 59.3% | **81.5%** | exponential 2h/8h/32h |
| `session_expiry` | 66 | 34.8% | **43.9%** | immediate then 6h (holds on 5/5 seeds) |
| `customer_input_error` | 122 | 40.2% | **53.3%** | link then +6h |
| `limit_exceeded` | 42 | **59.5%** | 54.8% | 00:30 ladder; B’s 24/72/120h still leads |

B still leads on `limit_exceeded` (59.5% vs 54.8% on 42 payments). Its 24/72/120h retries catch daily-cap resets the two-step 00:30 ladder misses. Matching it is possible; schedule-tuning stops here.

`insufficient_funds` at 29.3% is not a leak (gate would fire above 45%; five unseen seeds stay well under).

Our schedules encode two stated priors — Indian salaries cluster at month-start, and issuer lockout windows are undocumented so backoff beats a fixed wait. Both hold across five unseen seeds, and we deliberately did not tune to the simulator's actual window bounds. The residual risk is that we've had several iterations on this data and the baselines have had none; on real data we'd expect the gap to narrow.

Example gate rejection (`PAY_00071`): opted-out NSF → `retry_debit` rejected → `mark_uncollectible`. Complement (`PAY_00062`): mandate_failure, policy goes straight to reauth — the gate is unnecessary on that row.

### Regulatory (checked 31 August 2026)

**RBI**, *Digital Payments – E-mandate Framework, 2026*, 21 April 2026 (effective immediately; consolidates eight earlier circulars). Recurring payments skip AFA up to **₹15,000**. Insurance premiums, mutual-fund subscriptions, and credit-card bill payments sit at ₹1 lakh. This batch is a **general subscription / recurring merchant**, so ₹15,000 is the right threshold (`AFA_THRESHOLD = 15000`). AFA is required for registration, modification or withdrawal, the first transaction, customer opt-out, and higher-value recurring transactions.

Issuers must send a pre-debit notification at least 24 hours before a mandate debit, with merchant name, amount, date/time of debit, e-mandate reference, and reason. The agent logs `pre_debit_notification` as an audit row (not a conversion lever). Debits scheduled less than 24h out are flagged `pre_debit_window_violation` — 189 of 448 mandate debits on this batch, mostly the 4h/10h downtime and 2h/8h lockout retries.

**TRAI.** Transactional and service SMS have no time restriction and reach DND subscribers. The 9am–9pm window and DND scrub apply to promotional traffic. A payment-failure notice is service-class. Mixing an offer into that message reclassifies it as Promotional. The bounded-offer validator is therefore doing compliance work, not just policy work: POL-002’s 5% waiver is promotional; a no-offer recovery message is not.

---

## Robustness (calibration and sensitivity)

Headline numbers above are the canonical `data/` batch (seed 42, estimated mix). They are not replaced. The checks below write to `--out` directories and answer *"you made the data up"* without retuning policy. They were run on the Fix 7 agent (38.6%) and were not repeated after the second-ask rebaseline (`results_after_rebaseline.json`).

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

The rule agent above is the floor: **41.6%**. 41.5% / 951 messages was the same rules with a blanket 21:00–09:00 quiet-hours block. TRAI exempts service-class messages from that window; removing it recovered one additional payment. A LightGBM propensity model (`P(recover | visible features, action, channel)`) was trained on seeds **101–108** only. Eval seeds 42 / 1 / 2 / 7 / 99 / 123 were never used to fit. Converting-step labels: ROC-AUC **0.778**, PR-AUC **0.409**. Those AUCs, and the 6/6 seed bar below, were measured against `results_after_rebaseline.json` and were not repeated after the TRAI send-time correction.

Three applications, measured against that pre-TRAI snapshot, 5/6-seed bar:

- **Channel selection** — 6/6 rec and net on that floor. Live dashboard (TRAI 24/7): **43.9%**. Not the default.
- **EV suppression** — no incremental effect (`p * amount` almost never fails at ₹0.05–1).
- **Second-ask rank cut** — same recovery as channel on the live batch, **837 vs 949** messages. Bottom quartile of `p(step=2)` is 100% `issuer_decline` on 6/6. Available, not the default.

```bash
python -m eval.run_agent                                    # 41.6%
python -m eval.run_agent --use-model --ml-app second_ask    # 43.9% on data/
```

Full tables and the EV-floor finding: [day5-results.md](day5-results.md).

---

## Day 6 — Dashboard

Precompute once, then Streamlit reads disk. It never runs the batch.

```bash
python -m eval.precompute_dashboard
streamlit run dashboard/app.py
```

Bookmarks: `PAY_00210` clean recovery, `PAY_00062` policy-not-gate, `PAY_00071` opt-out gate, `PAY_00026` downtime wait, `PAY_00011` give-up, `PAY_00002` high-value NSF, plus staged `PAY_HV` / `PAY_LV`. Headline: agent **41.6%**, lift **+20.7 pp**, wasted **0**.

Failure demos, all landing in the same `decisions` table. Lead with the rogue composer (the model misbehaving, not infrastructure failing):

```bash
python -m agent.messaging --demo rogue          # validator rejects an invented offer
python -m eval.run_agent --trace PAY_00071      # gate rejects a debit, live trace
python -m agent.messaging --demo no-index       # retrieval fails, no-offer fallback
```

---

## API

Same agent as the dashboard, behind HTTP. `api/` imports `agent/` the way `dashboard/` does — nothing in `agent/`, `simulator/`, or `generator/` changes. The dashboard still reads `results/` and `results/audit.db` directly; it does not call this service.

```bash
.venv/bin/uvicorn api.main:app --reload   # from repo root, not api/
```

Then [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) (Swagger). `/` is empty.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/metrics` | `results/agent.json` |
| `GET` | `/payments/{id}` | Decision chain from `results/audit.db` (read-only, per-request). Try `PAY_00071`. |
| `POST` | `/webhook` | Razorpay `payment.failed` → diagnose → policy → gate. Returns gate results, not just actions. |

`GET /payments/PAY_00071` is the two-row opt-out chain the timeline renders: rejected `retry_debit`, then `mark_uncollectible`. `test_audit_chain_matches_dashboard_render` asserts the API and `dashboard/render.py` do not drift.

### Webhook

Set `RAZORPAY_WEBHOOK_SECRET` in `.env` — that is the **webhook** secret from the Razorpay dashboard, not `RAZORPAY_KEY_SECRET`.

Signature is HMAC-SHA256 of the **raw body** (`await request.body()`), compared with `hmac.compare_digest`. Parse-then-re-serialise will never match. Header: `X-Razorpay-Signature`.

The fixture `tests/fixtures/razorpay_payment_failed.json` uses the [official `payment.failed` envelope](https://razorpay.com/docs/webhooks/payments/) (`entity`, `account_id`, `event`, `contains`, `payload`, `created_at`) — not a guessed `{id, event, payload}` wrapper. Official bodies have no event `id`; idempotency uses `X-Razorpay-Event-Id`, then body `id` if present, then `pay_id:created_at`.

Four decisions in the handler:

1. **Verify signature first.** Missing or wrong → 401.
2. **Idempotency before any work.** A duplicate is free: `{"status": "duplicate", "event_id": ...}`.
3. **Unknown / null `error_reason` → 200**, not 500. `diagnose()` raises `KeyError` on purpose for synthetic data; a 500 would make Razorpay retry forever. Accept, log, return `failure_class: "unknown"`. That log is the list of reasons the taxonomy does not cover.
4. **Merchant-data join.** A webhook has amount, method, error fields, notes. It does not have `tenure_months`, `past_payment_count`, or `has_active_mandate`. `build_visible_from_webhook` looks up `notes.internal_payment_id` in the batch CSVs and falls back to conservative defaults (no mandate). Amount is paise → rupees. Webhook writes go to `api/events.db` (gitignored), never the committed dashboard DB.

```bash
set -a; source .env; set +a
SIG=$(python -c "from pathlib import Path; from api.security import sign; import os; print(sign(Path('tests/fixtures/razorpay_payment_failed.json').read_bytes(), os.environ['RAZORPAY_WEBHOOK_SECRET']))")
curl -s -X POST localhost:8000/webhook \
  -H "X-Razorpay-Signature: $SIG" \
  -H "Content-Type: application/json" \
  --data-binary @tests/fixtures/razorpay_payment_failed.json
```

Run it twice. First returns a decision (`insufficient_funds`, payday `retry_debit` for `PAY_00001`); second returns `duplicate`.

```bash
python -m unittest tests.test_api
```

