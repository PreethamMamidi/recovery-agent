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

from .config import (load_failure_classes, CONFIG_PATH, CONTROL_ARM_FRACTION,
                     MEASUREMENT_WINDOW_DAYS, RANDOM_SEED)
from .latents import make_latents
from .entities import make_customer, make_payment
from .natural_recovery import (
    DEFAULT_INTENT_WEIGHT,
    DEFAULT_REATTEMPT_WEIGHT,
    natural_recovery,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# Canonical coefficients for p_reattempts. Sensitivity may shift them ±0.1
# via generate kwargs; leaving the defaults keeps data/ bit-identical.
# The p_resolves sweep is a different lever (config CSV) and is never mixed
# with this one, so a gap move can be attributed to one change.
REATTEMPT_WEIGHT_DEFAULT = DEFAULT_REATTEMPT_WEIGHT
INTENT_WEIGHT_DEFAULT = DEFAULT_INTENT_WEIGHT


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _repo_path(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def generate(n_payments: int, seed: int = RANDOM_SEED,
             out_dir: Path | None = None,
             config_path: Path | None = None,
             peak_hours: bool = False,
             reattempt_weight: float = REATTEMPT_WEIGHT_DEFAULT,
             intent_weight: float = INTENT_WEIGHT_DEFAULT) -> dict:
    """
    Write a batch. Defaults reproduce the published data/ batch (canonical
    CSV, uniform failed_at hours, 0.35/0.45 reattempt mix). Robustness runs
    pass --config / --out / --peak-hours so data/ stays the headline.
    """
    data = Path(out_dir) if out_dir is not None else DATA
    rng = random.Random(seed)
    # Default CONFIG_PATH is the estimated mix. Calibrated NPCI weights live
    # in failure_classes_calibrated.csv (weights only — p_resolves untouched).
    cfg = Path(config_path) if config_path is not None else CONFIG_PATH
    if not cfg.is_absolute():
        cfg = ROOT / cfg
    classes = load_failure_classes(cfg)
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

        vis, hid = make_payment(pid, cust, fc, rng, period_start,
                                peak_hours=peak_hours)

        # Arm assignment is a property of the PAYMENT, decided at generation
        # time - not something the eval script works out later. It is a random
        # slice of every class and customer type, never a selected category.
        vis.arm = "control" if rng.random() < CONTROL_ARM_FRACTION else "treatment"

        recovered, when, p_used = natural_recovery(
            vis, hid, latents[cust.customer_id], fc, rng,
            reattempt_weight=reattempt_weight, intent_weight=intent_weight)

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
                    help="Output directory (default: data/). Robustness batches "
                         "must pass this so the published data/ mix is not overwritten.")
    ap.add_argument(
        "--config", type=str, default=str(CONFIG_PATH.relative_to(ROOT)),
        help="Failure-class CSV. Default is the estimated mix that reproduces "
             "data/. Pass config/failure_classes_calibrated.csv for the NPCI "
             "weight-only robustness mix (FinBox/NPCI 81.7%% BD / 18.3%% TD; "
             "NACH inadequate-balance; Business Standard top reasons; largest "
             "weight Δ is 0.05). Do not point this at a p_resolves-shifted "
             "file in the same run as a weight change — two experiments, "
             "two directories.")
    ap.add_argument(
        "--peak-hours", action="store_true", default=False,
        help="Weight failed_at toward 19:00–22:00 (Razorpay 8–12pp evening "
             "drop). Off by default so canonical data/ stays uniform-hours. "
             "Use only for a separate data/calibrated_peak/ batch — never "
             "mixed into the weight-only calibrated run.")
    ap.add_argument(
        "--reattempt-weight", type=float, default=REATTEMPT_WEIGHT_DEFAULT,
        help="Coefficient on reattempt_propensity in p_reattempts "
             f"(default {REATTEMPT_WEIGHT_DEFAULT}). Sensitivity-only; "
             "canonical generate must leave this unset.")
    ap.add_argument(
        "--intent-weight", type=float, default=INTENT_WEIGHT_DEFAULT,
        help="Coefficient on true_intent_to_pay in p_reattempts "
             f"(default {INTENT_WEIGHT_DEFAULT}). Sensitivity-only; "
             "canonical generate must leave this unset.")
    args = ap.parse_args()

    s = generate(
        args.n, args.seed,
        _repo_path(args.out) if args.out else None,
        config_path=_repo_path(args.config),
        peak_hours=args.peak_hours,
        reattempt_weight=args.reattempt_weight,
        intent_weight=args.intent_weight,
    )

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
