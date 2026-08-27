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
    return {
        "n_t": n,
        "b": rec_b,
        "natural_same_rows": rec_nat,
        "n_ctl": ctl_n,
        "control_slice": rec_ctl,
        "b_minus_natural": rec_b - rec_nat,
        "b_minus_control": rec_b - rec_ctl,
    }


def main() -> int:
    print("\n  insufficient_funds: B vs control-slice vs same-row natural")
    print(f"  {'seed':>6}{'n_t':>6}{'B':>8}{'natural':>10}{'n_ctl':>7}{'ctl':>8}"
          f"{'B-nat':>9}{'B-ctl':>9}")
    print("  " + "-" * 63)
    flips_vs_ctl = 0
    always_below_natural = True
    for seed in SEEDS:
        tmp = Path(tempfile.mkdtemp(prefix=f"seed{seed}_"))
        try:
            generate(1000, seed=seed, out_dir=tmp)
            world = load_world(tmp)
            s = nsf_sign(world)
            print(f"  {seed:>6}{s['n_t']:>6}{s['b']:>8.1%}{s['natural_same_rows']:>10.1%}"
                  f"{s['n_ctl']:>7}{s['control_slice']:>8.1%}"
                  f"{s['b_minus_natural']:>+9.1%}{s['b_minus_control']:>+9.1%}")
            if s["b_minus_control"] >= 0:
                flips_vs_ctl += 1
            if s["b_minus_natural"] > 0.005:
                always_below_natural = False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("  " + "-" * 63)
    if flips_vs_ctl and flips_vs_ctl < len(SEEDS):
        vs_ctl = "NOISE  control-slice sign flips across seeds"
    elif flips_vs_ctl == 0:
        vs_ctl = "STABLE  B below control slice on every seed (thin-n possible)"
    else:
        vs_ctl = "STABLE  B at or above control slice on every seed"

    vs_nat = ("MECHANISM  B below same-row natural every seed"
              if always_below_natural and flips_vs_ctl is not None
              else "B is not systematically below same-row natural")
    # same-row natural is the fair comparison; B-nat near 0 means failed
    # retries do not suppress the natural path.
    print(f"\n  vs control slice : {vs_ctl}")
    print(f"  vs same-row GT   : {vs_nat}")
    print("  Fair test is B vs same-row natural. Near-zero B-nat means a")
    print("  failed 24h retry does not consume the payday natural path.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
