"""
Run Baselines A, B, C on the generated batch.

    python -m eval.run_baselines

Headline lift uses the randomized control arm.
Per-class diagnostics use each treatment payment's own ground-truth
counterfactual (would_have_recovered_naturally), so n matches the
treatment slice rather than a thin control slice.
"""

from __future__ import annotations

import argparse
import json
import sys

from baselines.aggressive_dunning import schedule as schedule_c
from baselines.fixed_retry import schedule as schedule_a
from baselines.retry_plus_sms import schedule as schedule_b
from eval.metrics import (
    DATA,
    control_totals,
    identity_mismatches,
    load_world,
    rate,
    resolve_data,
    run_schedule,
)


def _print_headline(ctl, *policies) -> None:
    print(f"\n  identity  action=None vs ground_truth : "
          f"{'OK' if ctl._ident_ok else 'FAIL'}")
    print(f"  control n={ctl.n} recovery {ctl.recovery_rate:.1%}"
          f"  (randomized arm - headline only)")
    print()
    print(f"  {'policy':<28}{'n':>6}{'rec':>8}{'lift':>8}{'wasted':>8}"
          f"{'imposs':>8}{'msgs':>7}{'m/rec':>8}{'optout':>8}{'net Rs':>12}")
    print("  " + "-" * 101)
    print(f"  {'control':<28}{ctl.n:>6}{ctl.recovery_rate:>8.1%}"
          f"{'-':>8}{0:>8}{0:>8}{0:>7}{0:>8}{0:>8}{ctl.net_value:>12,.0f}")
    for p in policies:
        lift = p.recovery_rate - ctl.recovery_rate
        print(f"  {p.name:<28}{p.n:>6}{p.recovery_rate:>8.1%}"
              f"{lift:>+8.1%}{p.wasted_debits:>8}{p.impossible_debits:>8}"
              f"{p.messages:>7}"
              f"{p.messages_per_recovery:>8.2f}{p.opted_out_triggered:>8}"
              f"{p.net_value:>12,.0f}")
    print("  " + "-" * 101)
    print(f"  control net = gross recovered (zero costs), n={ctl.n}; "
          f"rupee figures on this arm are noisy — headline is recovery-rate lift")


def _print_per_class(classes, *policies) -> None:
    names = [p.name for p in policies]
    print("\n  per-class diagnostics on TREATMENT rows")
    print("  natural = that row's would_have_recovered_naturally (full GT, same n)")
    header = f"  {'class':<22}{'n':>5}{'natural':>9}" + "".join(
        f"{nm[:10]:>10}" for nm in names)
    print(header)
    print("  " + "-" * len(header))
    for cid in classes:
        bucket0 = policies[0].by_class.get(cid)
        if not bucket0:
            continue
        n = bucket0["n"]
        nat = rate(bucket0["natural"], n)
        cells = "".join(
            f"{rate(p.by_class.get(cid, {}).get('recovered', 0), n):>10.1%}"
            for p in policies)
        print(f"  {cid:<22}{n:>5}{nat:>9.1%}{cells}")
    print("  " + "-" * len(header))

    print("\n  per-class wasted / impossible debits / messages / opt-outs (treatment)")
    print(f"  {'class':<22}" + "".join(
        f"{nm[:8]:>8}{'imp':>6}{'msg':>6}{'oo':>5}" for nm in names))
    print("  " + "-" * (22 + 25 * len(names)))
    for cid in classes:
        if cid not in policies[0].by_class:
            continue
        line = f"  {cid:<22}"
        for p in policies:
            b = p.by_class.get(cid, {})
            line += (f"{b.get('wasted', 0):>8}{b.get('impossible', 0):>6}"
                     f"{b.get('messages', 0):>6}{b.get('opt_outs', 0):>5}")
        print(line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data", default=str(DATA.relative_to(DATA.parent)),
        help="Batch directory. Default data/ is the published canonical batch; "
             "robustness runs pass e.g. data/calibrated/seed_42 so headline "
             "numbers stay attached to data/. Taxonomy for policy is always "
             "the canonical CSV; this batch's p_resolves is in ground_truth.csv.")
    args = ap.parse_args(argv)
    data = resolve_data(args.data)
    world = load_world(data)
    ident = identity_mismatches(world)
    ctl = control_totals(world)
    ctl._ident_ok = ident == 0  # type: ignore[attr-defined]
    a = run_schedule(world, schedule_a, "A retry@24h")
    b = run_schedule(world, schedule_b, "B SMS+3 retries")
    c = run_schedule(world, schedule_c, "C aggressive")

    summary = json.loads((data / "generation_summary.json").read_text())
    print(f"\n  {len(world['pay_vis'])} payments · control {ctl.n}  |  "
          f"treatment {a.n}")
    print(f"  generation-summary control recovery : "
          f"{summary['natural_recovery_control_arm']:.1%}")

    _print_headline(ctl, a, b, c)
    _print_per_class(world["classes"], a, b, c)

    errors = []
    if ident:
        errors.append(f"identity failed on {ident} rows")
    expected = summary["natural_recovery_control_arm"]
    if abs(ctl.recovery_rate - expected) > 0.005:
        errors.append(f"control rate {ctl.recovery_rate:.3f} != summary {expected:.3f}")
    for p in (a, b, c):
        if p.recovery_rate <= 0.0 or p.recovery_rate >= 0.95:
            errors.append(f"{p.name} recovery {p.recovery_rate:.1%} is broken")
    if c.opted_out_triggered <= 0:
        errors.append("Baseline C did not trigger any opt-outs - annoyance untested")
    worse = any(
        rate(c.by_class.get(cid, {}).get("recovered", 0), c.by_class.get(cid, {}).get("n", 1))
        < rate(b.by_class.get(cid, {}).get("recovered", 0), b.by_class.get(cid, {}).get("n", 1))
        for cid in b.by_class
        if b.by_class[cid]["n"]
    )
    if not worse:
        errors.append("Baseline C was not worse than B on any class")

    if errors:
        print("\n  GATE FAIL")
        for e in errors:
            print(f"    - {e}")
        print()
        return 1

    print("\n  GATE OK  identity holds, A/B/C in (0%, 95%), C fires opt-outs\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
