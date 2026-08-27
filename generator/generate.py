"""
Generate the synthetic batch.

    python -m generator.generate --n 1000

Writes five files to data/:

  payments_visible.csv   <- the agent may read this
  customers_visible.csv  <- the agent may read this
  payments_hidden.csv    <- SIMULATOR ONLY
  customers_latent.csv   <- SIMULATOR ONLY
  ground_truth.csv       <- SIMULATOR ONLY, write once, never read while modelling

The visible/hidden split is the most important structural decision in the repo.
If the agent can physically read the hidden files, you can leak by accident.
"""

import argparse
import csv
import json
import random
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import (load_failure_classes, CONTROL_ARM_FRACTION,
                     MEASUREMENT_WINDOW_DAYS, RANDOM_SEED)
from .latents import make_latents
from .entities import make_customer, make_payment
from .natural_recovery import natural_recovery

DATA = Path(__file__).resolve().parents[1] / "data"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def generate(n_payments: int, seed: int = RANDOM_SEED,
             out_dir: Path | None = None) -> dict:
    data = Path(out_dir) if out_dir is not None else DATA
    rng = random.Random(seed)
    classes = load_failure_classes()
    data.mkdir(parents=True, exist_ok=True)

    class_ids = list(classes)
    weights = [classes[c].gen_weight for c in class_ids]

    # Fewer customers than payments: some customers fail more than once, which
    # is what makes past_failure_count a meaningful feature later.
    n_customers = int(n_payments * 0.75)

    customers, latents = {}, {}
    for i in range(n_customers):
        cid = f"CUST_{i:05d}"
        lat = make_latents(cid, rng)
        latents[cid] = lat
        customers[cid] = make_customer(cid, rng, lat)

    period_start = datetime(2026, 8, 1)
    cust_ids = list(customers)

    pay_vis, pay_hid, truth = [], [], []
    for i in range(n_payments):
        pid = f"PAY_{i:05d}"
        cust = customers[rng.choice(cust_ids)]
        fc = classes[rng.choices(class_ids, weights)[0]]

        vis, hid = make_payment(pid, cust, fc, rng, period_start)

        # Arm assignment is a property of the PAYMENT, decided at generation
        # time - not something the eval script works out later. It is a random
        # slice of every class and customer type, never a selected category.
        vis.arm = "control" if rng.random() < CONTROL_ARM_FRACTION else "treatment"

        recovered, when, p_used = natural_recovery(vis, hid, latents[cust.customer_id], fc, rng)

        pay_vis.append(vis.as_row())
        pay_hid.append(hid.as_row())
        truth.append({
            "payment_id": pid,
            "failure_class": fc.class_id,
            "arm": vis.arm,
            "would_have_recovered_naturally": recovered,
            "natural_recovery_date": when,
            "p_natural_used": p_used,
        })

    write_csv(data / "payments_visible.csv", pay_vis)
    write_csv(data / "customers_visible.csv", [c.as_row() for c in customers.values()])
    write_csv(data / "payments_hidden.csv", pay_hid)
    write_csv(data / "customers_latent.csv", [l.as_row() for l in latents.values()])
    write_csv(data / "ground_truth.csv", truth)

    return summarise(truth, pay_vis, classes, n_customers, data)


def summarise(truth, pay_vis, classes, n_customers, data: Path | None = None) -> dict:
    by_class = Counter(t["failure_class"] for t in truth)
    control = [t for t in truth if t["arm"] == "control"]
    nat_all = sum(t["would_have_recovered_naturally"] for t in truth) / len(truth)
    nat_ctl = (sum(t["would_have_recovered_naturally"] for t in control) / len(control)
               if control else 0.0)

    per_class = {}
    for cid in classes:
        rows = [t for t in truth if t["failure_class"] == cid]
        if rows:
            per_class[cid] = {
                "n": len(rows),
                "share": round(len(rows) / len(truth), 4),
                "natural_recovery": round(
                    sum(r["would_have_recovered_naturally"] for r in rows) / len(rows), 4),
                "mean_p": round(sum(r["p_natural_used"] for r in rows) / len(rows), 4),
            }

    # Expected control rate from the priors - the sanity check.
    expected = sum(classes[c].gen_weight * per_class[c]["mean_p"]
                   for c in per_class)

    summary = {
        "n_payments": len(truth),
        "n_customers": n_customers,
        "measurement_window_days": MEASUREMENT_WINDOW_DAYS,
        "control_n": len(control),
        "treatment_n": len(truth) - len(control),
        "natural_recovery_all": round(nat_all, 4),
        "natural_recovery_control_arm": round(nat_ctl, 4),
        "expected_from_priors": round(expected, 4),
        "mandate_share": round(sum(p["has_active_mandate"] for p in pay_vis) / len(pay_vis), 4),
        "per_class": per_class,
    }
    out = data if data is not None else DATA
    (out / "generation_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=RANDOM_SEED)
    ap.add_argument("--out", type=str, default=None,
                    help="Output directory (default: data/)")
    args = ap.parse_args()

    s = generate(args.n, args.seed, Path(args.out) if args.out else None)

    print(f"\n  {s['n_payments']} payments · {s['n_customers']} customers "
          f"· window {s['measurement_window_days']}d")
    print(f"  control {s['control_n']}  |  treatment {s['treatment_n']}"
          f"  |  mandate share {s['mandate_share']:.0%}\n")
    print(f"  {'class':<22}{'n':>6}{'share':>9}{'nat.rec':>10}{'mean p':>9}")
    print("  " + "-" * 56)
    for cid, v in sorted(s["per_class"].items(), key=lambda kv: -kv[1]["natural_recovery"]):
        print(f"  {cid:<22}{v['n']:>6}{v['share']:>9.1%}"
              f"{v['natural_recovery']:>10.1%}{v['mean_p']:>9.3f}")
    print("  " + "-" * 56)
    print(f"\n  natural recovery, whole batch : {s['natural_recovery_all']:.1%}")
    print(f"  natural recovery, control arm : {s['natural_recovery_control_arm']:.1%}")
    print(f"  expected from priors          : {s['expected_from_priors']:.1%}")
    gap = abs(s['natural_recovery_control_arm'] - s['expected_from_priors'])
    print(f"  {'OK' if gap < 0.07 else 'CHECK'}  gap {gap:.1%} "
          f"(sanity check: control arm should track the priors)\n")


if __name__ == "__main__":
    main()
