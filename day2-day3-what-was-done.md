# What was implemented — Day 2 fixes and Day 3 agent

This note is a walkthrough of the work after the generator (Day 1) and first simulator/baselines (Day 2). It records what changed, why, and what the numbers mean. It is not a plan; it is a record of what is in the repo now.

---

## 1. What was already there

Day 1 produced a 1,000-payment synthetic batch with a visible/hidden split. Day 2 added `simulator/response.py` and two class-blind baselines:

- **A** — one retry at 24h, no message (34.3% recovery)
- **B** — one generic SMS plus retries at 24h / 72h / 120h (40.1% recovery)

B was a strong baseline on purpose. The remaining work was: close measurement holes, prove the over-contact penalty, then ship a bounded rule-based agent that wins on **efficiency** even if it does not beat B on raw recovery.

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

Result on the treatment arm (n = 808):

| | Recovery | Wasted debits | Messages | Opt-outs | Net Rs |
|---|---|---|---|---|---|
| B | 40.1% | 700 | 808 | 0 | 1,470,786 |
| C | 40.1% | 936 | 2,990 | **298** | **204,159** |

Same recovery, ~7× worse net value. The stopping-rule story is now a measured fact, not a comment in the code.

### 2.4 Honest per-class numbers (`eval/metrics.py`, `eval/run_baselines.py`)

The old table used the **control slice** per class (~192 payments across 9 classes). `mandate_failure` and `instrument_invalid` control rates sat on a handful of rows.

Now:

- **Headline lift** = treatment recovery − randomized control arm (n = 192). This is the causal number.
- **Per-class diagnostics** = each *treatment* payment compared to its own `would_have_recovered_naturally`. Same n as A/B/C. Not mixed with the control slice.

Eval also prints wasted debits, messages, messages per recovery, triggered opt-outs, recovered rupees, cost, and net value.

### 2.5 The `insufficient_funds` inversion (`eval/check_seeds.py`)

On seed 42 the control *slice* for this class looked like 17.1% vs B 15.2%. That looked like “retrying hurts.”

`generator.generate` now accepts `--out` so other seeds can be written to temp dirs without touching `data/`.

Five other seeds: B is **at or above** both the control slice and same-row natural every time. On the canonical batch, same-row natural is 12.7% and B is 15.2%.

**Conclusion:** a failed 24h retry is a no-op on the payday path. The earlier gap was small-n noise in the control slice, not a simulator bug. The simulator was not changed.

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

Treatment n = 808. Control n = 192. Control recovery = 16.1%.

| Policy | Recovery | Lift | Wasted debits | Messages | Opt-outs | Net Rs |
|---|---|---|---|---|---|---|
| Control | 16.1% | — | 0 | 0 | 0 | 198,314 |
| A | 34.3% | +18.1 pp | 235 | 0 | 0 | 1,312,711 |
| B | **40.1%** | +24.0 pp | 700 | 808 | 0 | **1,470,786** |
| C | 40.1% | +24.0 pp | 936 | 2,990 | 298 | 204,159 |
| **Agent** | 33.4% | +17.3 pp | **0** | **396** | 0 | 994,580 |

### How to read the agent vs B

The agent does **not** beat B on recovery. Day 3 still counts as a win on the efficiency gates:

- wasted debits **0** (from 700)
- messages **396** (from 808)
- downtime messages **0**
- **202** gate rejections in the audit log

Where diagnosis actually changes the action:

| Class | n | B | Agent | Why |
|---|---|---|---|---|
| `instrument_invalid` | 96 | 3.1% | **18.8%** | ask for a new instrument; stop retrying a dead card |
| `mandate_failure` | 12 | 8.3% | **25.0%** | reauth, not debit |
| `insufficient_funds` | 204 | 15.2% | **17.6%** | 1st/7th heuristic (not hidden `salary_day`; 17.6% ≪ 45% leak tripwire) |
| `technical_downtime` | 117 | 78.6% | 73.5% | same idea as B (retry after heal), no SMS cost |
| `customer_input_error` | 121 | 76.0% | 31.4% | link only; B also sprays mandate retries the taxonomy says the agent cannot complete |

B’s extra recovery is mostly “retry everything, including classes where retry is waste.” The agent refuses that trade.

---

## 5. Tests and how to re-run

`tests/test_day3.py` (stdlib `unittest`, 20 tests):

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
- Policy was not tuned for hours to chase B’s 40.1%. The number is written down; the delta to Day 7 is the next slide.

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
