"""
Re-generate into temp dirs for a few seeds and check whether
insufficient_funds baseline-B recovery sits below the control slice.

Does not overwrite data/.

    python -m eval.check_seeds
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from baselines.retry_plus_sms import schedule as schedule_b
from eval.metrics import load_world, rate, run_schedule
from eval.run_agent import run_agent
from generator.generate import generate


SEEDS = (1, 2, 7, 99, 123)


def nsf_sign(world) -> dict:
    b = run_schedule(world, schedule_b, "B")
    bucket = b.by_class.get("insufficient_funds", {"n": 0, "recovered": 0, "natural": 0})
    n = bucket["n"]
    rec_b = rate(bucket["recovered"], n)
    rec_nat = rate(bucket["natural"], n)
    ctl_n = ctl_rec = 0
    for row in world["pay_vis"]:
        if row["failure_class"] != "insufficient_funds" or row["arm"] != "control":
            continue
        ctl_n += 1
        if str(world["truth"][row["payment_id"]]["would_have_recovered_naturally"]).lower() == "true":
            ctl_rec += 1
    rec_ctl = rate(ctl_rec, ctl_n)
    agent, conn = run_agent(world)
    conn.close()
    nsf_a = agent.by_class.get("insufficient_funds", {"n": 0, "recovered": 0})
    rec_agent = rate(nsf_a.get("recovered", 0), nsf_a.get("n", 0) or n)
    viable = {}
    for cid in ("technical_downtime", "temporary_lockout",
                "limit_exceeded", "session_expiry"):
        ab = agent.by_class.get(cid, {"n": 0, "recovered": 0, "natural": 0})
        an = ab.get("n", 0)
        viable[cid] = {
            "n": an,
            "agent": rate(ab.get("recovered", 0), an),
            "natural": rate(ab.get("natural", 0), an),
        }
    return {
        "n_t": n,
        "b": rec_b,
        "natural_same_rows": rec_nat,
        "n_ctl": ctl_n,
        "control_slice": rec_ctl,
        "b_minus_natural": rec_b - rec_nat,
        "b_minus_control": rec_b - rec_ctl,
        "agent": rec_agent,
        "agent_minus_natural": rec_agent - rec_nat,
        "agent_overall": agent.recovery_rate,
        "viable": viable,
    }


def main() -> int:
    print("\n  insufficient_funds: B vs agent vs same-row natural")
    print(f"  {'seed':>6}{'n_t':>6}{'B':>8}{'agent':>8}{'natural':>10}"
          f"{'ag-nat':>9}{'nsf>45':>8}")
    print("  " + "-" * 55)
    viable_rows = []
    flips_vs_ctl = 0
    always_below_natural = True
    agent_gain_seeds = 0
    leak = False
    lockout_gain_seeds = 0
    for seed in SEEDS:
        tmp = Path(tempfile.mkdtemp(prefix=f"seed{seed}_"))
        try:
            generate(1000, seed=seed, out_dir=tmp)
            world = load_world(tmp)
            s = nsf_sign(world)
            print(f"  {seed:>6}{s['n_t']:>6}{s['b']:>8.1%}{s['agent']:>8.1%}"
                  f"{s['natural_same_rows']:>10.1%}"
                  f"{s['agent_minus_natural']:>+9.1%}"
                  f"{'LEAK' if s['agent'] > 0.45 else 'ok':>8}")
            v = s["viable"]
            viable_rows.append((seed, v, s["agent_overall"]))
            if s["b_minus_control"] >= 0:
                flips_vs_ctl += 1
            if s["b_minus_natural"] > 0.005:
                always_below_natural = False
            if s["agent_minus_natural"] > 0.005:
                agent_gain_seeds += 1
            if s["agent"] > 0.45:
                leak = True
            lock = v["temporary_lockout"]
            if lock["agent"] - lock["natural"] > 0.005:
                lockout_gain_seeds += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("  " + "-" * 55)
    print("\n  viable-class agent recovery")
    print(f"  {'seed':>6}{'down':>8}{'lock':>8}{'limit':>8}{'sess':>8}{'overall':>9}")
    print("  " + "-" * 47)
    for seed, v, overall in viable_rows:
        print(f"  {seed:>6}{v['technical_downtime']['agent']:>8.1%}"
              f"{v['temporary_lockout']['agent']:>8.1%}"
              f"{v['limit_exceeded']['agent']:>8.1%}"
              f"{v['session_expiry']['agent']:>8.1%}"
              f"{overall:>9.1%}")
    print("  " + "-" * 47)

    if flips_vs_ctl and flips_vs_ctl < len(SEEDS):
        vs_ctl = "NOISE  control-slice sign flips across seeds"
    elif flips_vs_ctl == 0:
        vs_ctl = "STABLE  B below control slice on every seed (thin-n possible)"
    else:
        vs_ctl = "STABLE  B at or above control slice on every seed"

    vs_nat = ("MECHANISM  B below same-row natural every seed"
              if always_below_natural and flips_vs_ctl is not None
              else "B is not systematically below same-row natural")
    print(f"\n  vs control slice : {vs_ctl}")
    print(f"  vs same-row GT   : {vs_nat}")
    print(f"  agent NSF > natural on {agent_gain_seeds}/{len(SEEDS)} seeds"
          f"{'  LEAK TRIPWIRE' if leak else '  (tripwire 45% clear)'}")
    print(f"  agent lockout > natural on {lockout_gain_seeds}/{len(SEEDS)} seeds")
    print("  Fair test is B vs same-row natural. Near-zero B-nat means a")
    print("  failed 24h retry does not consume the payday natural path.\n")
    return 1 if leak else 0


if __name__ == "__main__":
    raise SystemExit(main())
