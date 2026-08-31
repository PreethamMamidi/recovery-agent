"""Shared scoring for calibration / sensitivity batches.

Eval always loads the canonical taxonomy for policy. Each batch's
p_resolves (and therefore natural recovery) is already in that batch's
ground_truth.csv, so we never point load_world at a robustness CSV.
"""

from __future__ import annotations

from pathlib import Path

from baselines.retry_plus_sms import schedule as schedule_b
from eval.metrics import (
    control_totals,
    identity_mismatches,
    load_world,
    rate,
    run_schedule,
)
from eval.run_agent import run_agent

# Same six seeds as the published seed-robustness check, plus the
# canonical headline seed. Python list so PowerShell never needs `for`.
ROBUSTNESS_SEEDS = (42, 1, 2, 7, 99, 123)

# Published data/ (seed 42, estimated mix). Robustness tables compare to this;
# they must not replace it.
PUBLISHED_AGENT = 0.386
PUBLISHED_B = 0.325
PUBLISHED_GAP = 0.061  # Fix 7: 38.6 vs 32.5. Live floor is 41.6; these runs were not repeated.
PUBLISHED_CONTROL = 0.209


def evaluate_batch(data: Path, expect_n: int | None = 1000) -> dict:
    """Identity, wasted/impossible, NSF tripwire, then Agent / B / gap."""
    world = load_world(data)
    ident = identity_mismatches(world)
    ctl = control_totals(world)
    b = run_schedule(world, schedule_b, "B SMS+3 retries")
    agent, conn = run_agent(world)
    conn.close()
    nsf = agent.by_class.get("insufficient_funds", {})
    nsf_n = nsf.get("n", 0)
    nsf_rate = rate(nsf.get("recovered", 0), nsf_n)
    n = len(world["pay_vis"])
    n_ok = True if expect_n is None else n == expect_n
    gates_ok = (
        ident == 0
        and agent.wasted_debits == 0
        and agent.impossible_debits == 0
        and nsf_rate < 0.45
        and n_ok
    )
    return {
        "n": n,
        "identity_ok": ident == 0,
        "identity_mismatches": ident,
        "control": round(ctl.recovery_rate, 6),
        "b": round(b.recovery_rate, 6),
        "agent": round(agent.recovery_rate, 6),
        "gap": round(agent.recovery_rate - b.recovery_rate, 6),
        "wasted": agent.wasted_debits,
        "impossible": agent.impossible_debits,
        "nsf": round(nsf_rate, 6),
        "nsf_n": nsf_n,
        "nsf_ok": nsf_rate < 0.45,
        "gates_ok": gates_ok,
    }
