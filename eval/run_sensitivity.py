"""
p_resolves ±0.1 sensitivity sweep. Does not touch data/.

    python -m eval.run_sensitivity
    python -m eval.run_sensitivity --reattempt-lever

Prediction (also in the writeup): control moves a lot; the agent–B gap
should move little; optimistic compresses headroom because every policy
has less room above a higher natural-recovery floor.

Caveat on the p_resolves lever: downtime, lockout, daily limits, and NSF
resolve from hidden timestamps, not fc.p_resolves, and session/input sit
at 1.00 so +0.1 clamps. That lever therefore moves less than a naive
reading of the CSV suggests. The reattempt-coefficient lever (±0.1 on
0.35/0.45) is the one that actually shifts the person side of the
two-factor formula, and is where we expect control to move.

Canonical condition is the existing data/ batch (seed 42), not a regenerate.
Pessimistic / optimistic × six seeds write to data/sens_{cond}_{seed}.
"""

from __future__ import annotations

import argparse
import json

from eval.build_sensitivity_configs import main as build_configs
from eval.metrics import DATA, ROOT
from eval.robustness import (
    PUBLISHED_AGENT,
    PUBLISHED_B,
    PUBLISHED_CONTROL,
    PUBLISHED_GAP,
    ROBUSTNESS_SEEDS,
    evaluate_batch,
)
from generator.generate import (
    INTENT_WEIGHT_DEFAULT,
    REATTEMPT_WEIGHT_DEFAULT,
    generate,
)

RESULTS_JSON = ROOT / "eval" / "sensitivity_results.json"
# Second lever: shift both p_reattempts coefficients together ±0.1.
# Defaults 0.35 / 0.45 stay canonical. Independent of the p_resolves CSV.
REATTEMPT_DELTA = 0.1


def _print_table(title: str, rows: list[dict]) -> None:
    print(f"\n  {title}")
    print(f"  {'cond':<16}{'seed':>6}{'ctl':>9}{'B':>9}{'agent':>9}{'gap':>9}{'gates':>8}")
    print("  " + "-" * 66)
    for r in rows:
        print(f"  {r['condition']:<16}{r['seed']:>6}{r['control']:>9.1%}"
              f"{r['b']:>9.1%}{r['agent']:>9.1%}{r['gap']:>+9.1%}"
              f"{'OK' if r['gates_ok'] else 'FAIL':>8}")
    print("  " + "-" * 66)


def _mean(rows: list[dict], key: str) -> float:
    return sum(r[key] for r in rows) / len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--reattempt-lever", action="store_true", default=False,
        help="After the p_resolves sweep, also shift p_reattempts coefficients "
             "±0.1 into data/sens_reattempt_{low|high}_{seed}. Canonical "
             "defaults 0.35/0.45 stay unchanged on every other run.")
    args = ap.parse_args(argv)

    build_configs()

    print("\n  sensitivity  p_resolves ±0.1  (canonical row = existing data/)")
    print("  prediction: control moves; gap should not; optimistic compresses headroom")

    rows: list[dict] = []

    # Canonical: do not regenerate. Seed 42 published batch is the row.
    canon = evaluate_batch(DATA)
    canon["seed"] = 42
    canon["condition"] = "canonical"
    rows.append(canon)
    print(f"  canonical data/  ctl={canon['control']:.1%}  B={canon['b']:.1%}  "
          f"agent={canon['agent']:.1%}  gap={canon['gap']:+.1%}  "
          f"{'OK' if canon['gates_ok'] else 'FAIL'}")

    configs = {
        "pessimistic": ROOT / "config" / "sensitivity_pessimistic.csv",
        "optimistic": ROOT / "config" / "sensitivity_optimistic.csv",
    }
    for cond, cfg in configs.items():
        for seed in ROBUSTNESS_SEEDS:
            out = ROOT / "data" / f"sens_{cond}_{seed}"
            print(f"  generate {cond} seed={seed} -> {out.relative_to(ROOT)}")
            generate(1000, seed=seed, out_dir=out, config_path=cfg)
            rec = evaluate_batch(out)
            rec["seed"] = seed
            rec["condition"] = cond
            rows.append(rec)
            gate = "OK" if rec["gates_ok"] else "FAIL"
            print(f"    {gate}  ctl={rec['control']:.1%}  B={rec['b']:.1%}  "
                  f"agent={rec['agent']:.1%}  gap={rec['gap']:+.1%}")

    _print_table("p_resolves sweep", rows)
    pes = [r for r in rows if r["condition"] == "pessimistic"]
    opt = [r for r in rows if r["condition"] == "optimistic"]
    print(f"  mean pessimistic  ctl {_mean(pes, 'control'):.1%}  "
          f"gap {_mean(pes, 'gap'):+.1%}")
    print(f"  published canonical ctl {PUBLISHED_CONTROL:.1%}  "
          f"agent {PUBLISHED_AGENT:.1%}  B {PUBLISHED_B:.1%}  "
          f"gap {PUBLISHED_GAP:+.1%}")
    print(f"  mean optimistic   ctl {_mean(opt, 'control'):.1%}  "
          f"gap {_mean(opt, 'gap'):+.1%}\n")

    payload: dict = {"p_resolves": rows, "reattempt": None}
    rc = 0 if all(r["gates_ok"] for r in rows) else 1

    if args.reattempt_lever:
        if rc:
            print("  skip --reattempt-lever: p_resolves gates failed\n")
        else:
            # Independent lever: canonical class mix, only the reattempt formula.
            re_rows = []
            variants = (
                ("low", REATTEMPT_WEIGHT_DEFAULT - REATTEMPT_DELTA,
                 INTENT_WEIGHT_DEFAULT - REATTEMPT_DELTA),
                ("high", REATTEMPT_WEIGHT_DEFAULT + REATTEMPT_DELTA,
                 INTENT_WEIGHT_DEFAULT + REATTEMPT_DELTA),
            )
            print("  reattempt-coefficient lever  (±0.1 on 0.35/0.45; canonical CSV)")
            for name, rw, iw in variants:
                for seed in ROBUSTNESS_SEEDS:
                    out = ROOT / "data" / f"sens_reattempt_{name}_{seed}"
                    print(f"  generate reattempt_{name} seed={seed} "
                          f"rw={rw:.2f} iw={iw:.2f}")
                    generate(
                        1000, seed=seed, out_dir=out,
                        reattempt_weight=rw, intent_weight=iw,
                    )
                    rec = evaluate_batch(out)
                    rec["seed"] = seed
                    rec["condition"] = f"reattempt_{name}"
                    rec["reattempt_weight"] = round(rw, 2)
                    rec["intent_weight"] = round(iw, 2)
                    re_rows.append(rec)
                    gate = "OK" if rec["gates_ok"] else "FAIL"
                    print(f"    {gate}  ctl={rec['control']:.1%}  "
                          f"gap={rec['gap']:+.1%}")
            _print_table("reattempt coefficients ±0.1", re_rows)
            payload["reattempt"] = re_rows
            if not all(r["gates_ok"] for r in re_rows):
                rc = 1

    RESULTS_JSON.write_text(json.dumps(payload, indent=2))
    print(f"  wrote {RESULTS_JSON.relative_to(ROOT)}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
