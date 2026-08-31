# What was implemented — Day 2 fixes and Day 3 agent

This note is a walkthrough of the work after the generator (Day 1) and first simulator/baselines (Day 2). It records what changed, why, and what the numbers mean. Canonical headlines are now **41.5%** (`results_after_rebaseline.json`); Fix 7 sat at 38.6%.

---

## 1. What was already there

Day 1 produced a 1,000-payment synthetic batch with a visible/hidden split. Day 2 added `simulator/response.py` and two class-blind baselines:

- **A** — one retry at 24h, no message
- **B** — one generic SMS plus retries at 24h / 72h / 120h

B was a strong baseline on purpose. The remaining work was: close measurement holes, prove the over-contact penalty, then ship a bounded rule-based agent. Those Day-2 recovery rates are from the pre-presence-map batch. Canonical figures after subsequent generator and agent work are in §4 and `results_after_fix7.json`.

---

## 2. Day 2 evidence gaps that were closed

### 2.1 Fixed costs (`config/costs.py`)

Net value is not meaningful if costs are chosen after seeing results. Constants were locked first:

| Item | Cost |
|---|---|
| Debit attempt | Rs 2 |
| SMS | Rs 0.20 |
| WhatsApp | Rs 1 |
| Email | Rs 0.05 |
| New opt-out | 30% of that customer's `lifetime_value` |

`net value = recovered rupees − those costs`.

### 2.2 Richer simulator outcomes (`simulator/response.py`)

`Outcome` now reports:

- messages by channel (SMS / WhatsApp / email)
- whether an opt-out was **triggered this run** vs already opted out at generation

The identity check is unchanged: `respond(..., actions=[])` still returns `ground_truth.csv` exactly (1,000 / 1,000).

A failed debit still does **not** cancel later natural recovery. Only recovery, opt-out, or crossing `annoyance_threshold` can suppress the natural path.

### 2.3 Baseline C — aggressive dunning (`baselines/aggressive_dunning.py`)

A and B never hit `annoyance_threshold` (2–5 contacts). One SMS cannot prove that over-contacting hurts.

C sends **five SMS** over 14 days plus a spray of retries, still class-blind. That is enough to fire the penalty.

Result on the treatment arm (canonical batch, n = 813):

| | Recovery | Wasted debits | Messages | Opt-outs | Net Rs |
|---|---|---|---|---|---|
| B | 32.5% | 426 | 813 | 0 | 1,377,187 |
| C | 32.2% | 569 | 3,297 | **329** | **85,761** |

Same recovery, ~16× worse net value. The stopping-rule story is now a measured fact, not a comment in the code.

### 2.4 Honest per-class numbers (`eval/metrics.py`, `eval/run_baselines.py`)

The old table used the **control slice** per class (~187 payments across 9 classes). `mandate_failure` and `instrument_invalid` control rates sat on a handful of rows.

Now:

- **Headline lift** = treatment recovery − randomized control arm (n = 187). This is the causal number.
- **Per-class diagnostics** = each *treatment* payment compared to its own `would_have_recovered_naturally`. Same n as A/B/C. Not mixed with the control slice.

Eval also prints wasted debits, messages, messages per recovery, triggered opt-outs, recovered rupees, cost, and net value.

### 2.5 The `insufficient_funds` inversion (`eval/check_seeds.py`)

On seed 42 the control *slice* for this class is thin and noisy.

`generator.generate` now accepts `--out` so other seeds can be written to temp dirs without touching `data/`.

Five other seeds: agent NSF is above same-row natural every time, and below the 45% leak tripwire. On the canonical batch, same-row natural is 20.2% and B is 21.7%.

**Conclusion:** a failed 24h retry is a no-op on the payday path. Gaps vs the control slice are small-n noise, not a simulator bug.

---

## 3. Day 3 — bounded rule-based agent

No ML. No LLM. The taxonomy is the policy. The agent never imports `simulator/`, `generator.latents`, or `generator.natural_recovery`.

### 3.1 Diagnose (`agent/diagnose.py`)

Inverts `ERROR_REASONS` to `error_reason → failure_class`.

- All **74** in-scope reasons map, exactly once
- Duplicates raise at import
- Unknown strings raise (`KeyError`) — no silent fallback class

Diagnosis uses `error_reason` only. It does not read the pre-labeled `failure_class` column on the payment (that column is generator truth; a production webhook would not have it).

### 3.2 Bounded actions (`agent/actions.py`)

Nine actions, validated schema, nothing free-form:

| Kind | Actions |
|---|---|
| Autonomous | `retry_debit`, `schedule_for_payday`, `wait_for_downtime_recovery` |
| Customer-action | `send_payment_link`, `request_instrument_update`, `request_mandate_reauth`, `send_reminder` |
| Terminal | `escalate`, `mark_uncollectible` |

The eval runner converts a validated `Decision` into a simulator `Action` only at the evaluation boundary.

### 3.3 Policy (`agent/policy.py`)

Visible fields plus the taxonomy row. Class by class:

| Class | What the agent does |
|---|---|
| `insufficient_funds` | `schedule_for_payday` on the next **1st or 7th** (guess; it does not see `salary_day`) |
| `technical_downtime` | wait ~4h, then retry at 6h and 12h, **no message** |
| `temporary_lockout` | backoff retries at 2h / 6h / 24h, no message |
| `limit_exceeded` | daily cap → retry after 00:30; structural reason strings → instrument update |
| `session_expiry` | immediate retry if mandate, else payment link in minutes |
| `customer_input_error` | payment link in minutes (agent cannot complete the form) |
| `instrument_invalid` | instrument update immediately, **zero debits** |
| `issuer_decline` | payment link / method switch, **zero debits** |
| `mandate_failure` | mandate reauth, **zero debits** |

`wait_for_downtime_recovery` is a no-op in the simulator (log only). Recovery on downtime still needs a **retry after the rail heals**, which is why the policy waits *then* retries.

### 3.4 Guardrails (`agent/guardrails.py`)

Every proposed action is gated. On reject: log it, fall back, **never execute the rejected action**.

| Gate | Rule |
|---|---|
| Mandate | no active mandate → debit actions rejected |
| Attempt budget | `attempt_number >= max_attempts` for the class → stop |
| Opt-out | only terminal actions |
| Quiet hours | no messages 21:00–09:00; shifted to 09:00 |
| Contact cap | max 3 messages / customer / week |
| Cooling-off | promise-to-pay date blocks contact (wired; unused until a reply parser exists) |
| Value | amount ≥ Rs 25,000 → escalate |

Downtime and lockout **do not** fall back to a message. Messaging a blameless customer is the thing the taxonomy forbids.

`wait_for_downtime_recovery` does not require a mandate (it is restraint, not a debit). `retry_debit` and `schedule_for_payday` do. That is how `mandate_failure` and no-mandate payday retries produce live rejections without a staged demo.

### 3.5 Loop, audit, eval (`agent/loop.py`, `audit/log.py`, `eval/run_agent.py`)

1. Load **visible** CSVs only into diagnose / policy / gate
2. Build a timestamped schedule
3. Gate each step; write SQLite rows for both allows and rejects
4. Convert approved decisions to simulator actions
5. Simulate treatment payments against hidden ground truth
6. Score recovery, waste, messages, net value against A/B/C

Audit DB: `audit/log.db` (gitignored). One row per proposed decision: payment, attempt, time, class, action, args, gate result/reason, executed flag, outcome.

Example from a real run — `PAY_00003`:

```
2026-08-07T10:00:00  insufficient_funds  schedule_for_payday  rejected  mandate_gate
2026-08-07T10:00:00  insufficient_funds  send_payment_link    allowed   ok
```

---

## 4. Numbers on the canonical batch (n = 1,000, seed 42)

Treatment n = 813. Control n = 187. Control recovery = 20.9%. Source: `results_after_rebaseline.json` (Fix 7 snapshot: `results_before_rebaseline.json`).

| Policy | Recovery | Lift | Wasted | Imposs | Messages | m/rec | Opt-outs | Net Rs |
|---|---|---|---|---|---|---|---|---|
| Control | 20.9% | — | 0 | 0 | 0 | 0 | 0 | 77,878 |
| A | 26.8% | +6.0 pp | 142 | 417 | 0 | 0 | 0 | 1,072,537 |
| B | 32.5% | +11.6 pp | 426 | 1,094 | 813 | 3.08 | 0 | 1,377,187 |
| C | 32.2% | +11.4 pp | 569 | 1,427 | 3,297 | 12.58 | 329 | 85,761 |
| **Agent** | **41.5%** | **+20.6 pp** | **0** | **0** | **951** | **2.82** | 0 | **1,657,339** |

Control net is gross recovered (zero costs); rupee figures on that arm are noisy.

### How to read the agent vs B

The agent beats B on recovery **and** on efficiency:

- wasted debits **0** (from 426)
- impossible debits **0** (from 1,094)
- messages **951** vs **813**; messages per recovery **2.82 vs 3.08** (exceeds B on raw sends; still beats B on the ratio)
- downtime-with-mandate messages **0**
- **27** gate rejections; **46** high-value rows flagged for review (policy still runs)

The customer-action 6h follow-up moved messages 639 → 951 and m/rec 2.04 → 2.82 while recovery went 38.6% → 41.5%. Both went up. Restraint was never “fewest messages” — it is no messages where they don’t help. Channel mix: 373 SMS / 464 WhatsApp / 114 email (preferred_channel). B is 813 SMS.

Where diagnosis actually changes the action:

| Class | n | B | Agent | Why |
|---|---|---|---|---|
| `instrument_invalid` | 91 | 1.1% | **23.1%** | ask for a new instrument; stop retrying a dead card; 6h follow-up |
| `mandate_failure` | 14 | 14.3% | **42.9%** | reauth, not debit; 6h follow-up |
| `insufficient_funds` | 198 | 21.7% | **29.3%** | nearest 1st/7th/15th (not hidden `salary_day`; 29.3% ≪ 45% leak tripwire) |
| `technical_downtime` | 115 | 77.4% | **86.1%** | backoff after wait; no SMS if mandate |
| `temporary_lockout` | 27 | 59.3% | **81.5%** | exponential 2h/8h/32h |
| `session_expiry` | 66 | 34.8% | **43.9%** | immediate then 6h (holds on 5/5 seeds) |
| `customer_input_error` | 122 | 40.2% | **51.6%** | link then +6h |
| `limit_exceeded` | 42 | **59.5%** | 54.8% | 00:30 ladder; B’s 24/72/120h still leads |

B still leads on `limit_exceeded` (59.5% vs 54.8% on 42 payments). Its 24/72/120h retries catch daily-cap resets the two-step 00:30 ladder misses. Matching it is possible; schedule-tuning stops here.

Our schedules encode two stated priors — Indian salaries cluster at month-start, and issuer lockout windows are undocumented so backoff beats a fixed wait. Both hold across five unseen seeds, and we deliberately did not tune to the simulator's actual window bounds. The residual risk is that we've had several iterations on this data and the baselines have had none; on real data we'd expect the gap to narrow.

---

## 5. Tests and how to re-run

`tests/test_day3.py` (stdlib `unittest`):

- identity on all rows
- failed retry does not suppress later natural recovery
- Baseline C triggers opt-outs
- 74 reasons diagnose once; unknown raises
- action schema accept / reject
- mandate, attempt, opt-out, quiet hours, contact cap, cooling-off, value gates
- agent import boundary (no `simulator` / latents / natural_recovery)
- one no-mandate payday payment produces a rejection then a fallback

```bash
python -m generator.generate --n 1000
python -m eval.run_baselines
python -m eval.check_seeds
python -m eval.run_agent
python -m unittest tests.test_day3
```

---

## 6. What was deliberately not done

- No ML (Day 5)
- No LLM copy (Day 5)
- No dashboard (Day 6)
- No live Razorpay or real SMS
- Policy was not tuned to the simulator’s hidden window bounds to chase B. The residual risk is several iterations on this data vs none for the baselines.

---

## 7. Files added or substantially changed

| Path | Role |
|---|---|
| `config/costs.py` | Locked economics |
| `simulator/response.py` | Message counts, triggered opt-out |
| `baselines/aggressive_dunning.py` | Baseline C |
| `eval/metrics.py` | Shared scoring |
| `eval/run_baselines.py` | A/B/C + honest diagnostics |
| `eval/check_seeds.py` | NSF multi-seed check |
| `eval/run_agent.py` | Agent vs baselines |
| `generator/generate.py` | Optional `--out` directory |
| `agent/diagnose.py` | Reason → class |
| `agent/actions.py` | Schema |
| `agent/policy.py` | Taxonomy rules |
| `agent/guardrails.py` | Gate + fallback |
| `agent/loop.py` | Diagnose → plan → gate |
| `audit/log.py` | SQLite decision log |
| `tests/test_day3.py` | Acceptance tests |
| `README.md` | Canonical numbers |
