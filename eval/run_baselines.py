"""
Run Baseline A and B on the generated batch.

    python -m eval.run_baselines

Gates:
  - action=None matches ground_truth.csv on every row
  - control recovery matches the generation summary
  - neither baseline is 0% or >= 95%
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from generator.config import load_failure_classes
from simulator.response import (
    latents_from_row,
    payment_hidden_from_row,
    payment_visible_from_row,
    respond,
)
from baselines.fixed_retry import schedule as schedule_a
from baselines.retry_plus_sms import schedule as schedule_b

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _index(rows: list[dict], key: str) -> dict[str, dict]:
    return {r[key]: r for r in rows}


def _rate(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def _print_table(title: str, rows: list[tuple]) -> None:
    print(f"\n  {title}")
    print(f"  {'class':<22}{'n':>6}{'recovered':>12}{'wasted':>10}")
    print("  " + "-" * 50)
    for cid, n, rec, wasted in rows:
        print(f"  {cid:<22}{n:>6}{rec:>12.1%}{wasted:>10}")
    print("  " + "-" * 50)


def main() -> int:
    classes = load_failure_classes()
    pay_vis = _read_csv(DATA / "payments_visible.csv")
    pay_hid = _index(_read_csv(DATA / "payments_hidden.csv"), "payment_id")
    customers = _index(_read_csv(DATA / "customers_visible.csv"), "customer_id")
    latents = _index(_read_csv(DATA / "customers_latent.csv"), "customer_id")
    truth = _index(_read_csv(DATA / "ground_truth.csv"), "payment_id")
    summary = json.loads((DATA / "generation_summary.json").read_text())

    identity_fail = 0
    control_flags: list[bool] = []
    by_class: dict[str, dict] = defaultdict(lambda: {
        "n_ctl": 0, "rec_ctl": 0,
        "n_t": 0,
        "rec_a": 0, "rec_b": 0,
        "wasted_a": 0, "wasted_b": 0,
        "debits_a": 0, "debits_b": 0,
    })
    a_flags: list[bool] = []
    b_flags: list[bool] = []
    wasted_a = wasted_b = 0
    debits_a = debits_b = 0

    for row in pay_vis:
        vis = payment_visible_from_row(row)
        hid = payment_hidden_from_row(pay_hid[vis.payment_id])
        lat = latents_from_row(latents[vis.customer_id])
        fc = classes[vis.failure_class]
        gt = truth[vis.payment_id]
        opted = str(customers[vis.customer_id]["opted_out"]).strip().lower() in {
            "true", "1", "yes"}

        ident = respond(vis, hid, lat, fc, gt, actions=[], opted_out=opted)
        gt_rec = ident.recovered
        raw_rec = str(gt.get("would_have_recovered_naturally", "")).strip().lower() in {
            "true", "1", "yes"}
        raw_date = (gt.get("natural_recovery_date") or "").strip()
        ident_date = ident.recovered_at or ""
        if ident.recovered != raw_rec or ident_date != raw_date:
            identity_fail += 1

        bucket = by_class[vis.failure_class]

        if vis.arm == "control":
            control_flags.append(gt_rec)
            bucket["n_ctl"] += 1
            bucket["rec_ctl"] += int(gt_rec)
            continue

        # Treatment: run both baselines. Control never receives actions.
        out_a = respond(vis, hid, lat, fc, gt, schedule_a(vis), opted_out=opted)
        out_b = respond(vis, hid, lat, fc, gt, schedule_b(vis), opted_out=opted)

        bucket["n_t"] += 1
        bucket["rec_a"] += int(out_a.recovered)
        bucket["rec_b"] += int(out_b.recovered)
        bucket["wasted_a"] += out_a.wasted_debits
        bucket["wasted_b"] += out_b.wasted_debits
        bucket["debits_a"] += out_a.debit_attempts
        bucket["debits_b"] += out_b.debit_attempts
        a_flags.append(out_a.recovered)
        b_flags.append(out_b.recovered)
        wasted_a += out_a.wasted_debits
        wasted_b += out_b.wasted_debits
        debits_a += out_a.debit_attempts
        debits_b += out_b.debit_attempts

    ctl = _rate(control_flags)
    rec_a = _rate(a_flags)
    rec_b = _rate(b_flags)
    expected_ctl = summary["natural_recovery_control_arm"]

    print(f"\n  {len(pay_vis)} payments · control {len(control_flags)}  |  "
          f"treatment {len(a_flags)}")
    print(f"  identity  action=None vs ground_truth : "
          f"{'OK' if identity_fail == 0 else f'FAIL  {identity_fail} mismatches'}")
    print(f"  control recovery                    : {ctl:.1%}  "
          f"(generation summary {expected_ctl:.1%})")
    print(f"  baseline A  retry @ 24h             : {rec_a:.1%}"
          f"  lift {rec_a - ctl:+.1%}")
    print(f"  baseline B  SMS + 3 retries         : {rec_b:.1%}"
          f"  lift {rec_b - ctl:+.1%}")
    print(f"  wasted debits A / B                 : {wasted_a} / {wasted_b}  "
          f"(of {debits_a} / {debits_b} debit attempts)")

    rows_a = []
    rows_b = []
    for cid in sorted(by_class, key=lambda c: -(by_class[c]["rec_a"] / by_class[c]["n_t"]
                                                if by_class[c]["n_t"] else 0)):
        b = by_class[cid]
        n = b["n_t"]
        if n == 0:
            continue
        rows_a.append((cid, n, b["rec_a"] / n, b["wasted_a"]))
        rows_b.append((cid, n, b["rec_b"] / n, b["wasted_b"]))

    # print A ordered by A's recovery, B ordered by B's
    _print_table("Baseline A — treatment, per class", rows_a)
    rows_b.sort(key=lambda r: -r[2])
    _print_table("Baseline B — treatment, per class", rows_b)

    print("\n  control vs baselines, per class (treatment n)")
    print(f"  {'class':<22}{'n':>6}{'control':>10}{'A':>10}{'B':>10}")
    print("  " + "-" * 58)
    for cid in sorted(classes):
        b = by_class[cid]
        n = b["n_t"]
        if n == 0:
            continue
        ctl_c = b["rec_ctl"] / b["n_ctl"] if b["n_ctl"] else 0.0
        print(f"  {cid:<22}{n:>6}{ctl_c:>10.1%}{b['rec_a']/n:>10.1%}{b['rec_b']/n:>10.1%}")
    print("  " + "-" * 58)

    errors = []
    if identity_fail:
        errors.append(f"identity failed on {identity_fail} rows")
    if abs(ctl - expected_ctl) > 0.005:
        errors.append(f"control rate {ctl:.3f} != generation summary {expected_ctl:.3f}")
    for name, rate in (("A", rec_a), ("B", rec_b)):
        if rate <= 0.0 or rate >= 0.95:
            errors.append(f"baseline {name} recovery {rate:.1%} is broken (0% or >=95%)")

    if errors:
        print("\n  GATE FAIL")
        for e in errors:
            print(f"    - {e}")
        print()
        return 1

    print("\n  GATE OK  identity holds, baselines in (0%, 95%)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
