"""
NPCI-weight calibration across seeds. Does not touch data/.

    python -m eval.run_calibration
    python -m eval.run_calibration --peak-hours

Weight-only batches go to data/calibrated/seed_S. Evening-peak is a
separate --peak-hours batch under data/calibrated_peak/ so a gap move
can be attributed to one change. Canonical gap is the published
38.6 vs 32.5 (+6.1).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval.metrics import ROOT
from eval.robustness import (
    PUBLISHED_AGENT,
    PUBLISHED_B,
    PUBLISHED_GAP,
    ROBUSTNESS_SEEDS,
    evaluate_batch,
)
from generator.generate import generate

CALIBRATED_CONFIG = ROOT / "config" / "failure_classes_calibrated.csv"
OUT_WEIGHTS = ROOT / "data" / "calibrated"
OUT_PEAK = ROOT / "data" / "calibrated_peak"
RESULTS_JSON = ROOT / "eval" / "calibration_results.json"


def _run_seeds(out_root: Path, peak_hours: bool) -> list[dict]:
    rows = []
    for seed in ROBUSTNESS_SEEDS:
        out = out_root / f"seed_{seed}"
        print(f"  generate seed={seed} -> {out.relative_to(ROOT)}"
              f"{'  peak-hours' if peak_hours else ''}")
        generate(
            1000, seed=seed, out_dir=out,
            config_path=CALIBRATED_CONFIG,
            peak_hours=peak_hours,
        )
        rec = evaluate_batch(out)
        rec["seed"] = seed
        rec["peak_hours"] = peak_hours
        rows.append(rec)
        gate = "OK" if rec["gates_ok"] else "FAIL"
        print(f"    {gate}  ident={rec['identity_mismatches']}  "
              f"wasted={rec['wasted']} imposs={rec['impossible']}  "
              f"NSF={rec['nsf']:.1%}  "
              f"agent={rec['agent']:.1%}  B={rec['b']:.1%}  "
              f"gap={rec['gap']:+.1%}")
    return rows


def _print_table(title: str, rows: list[dict]) -> None:
    print(f"\n  {title}")
    print(f"  {'seed':>6}{'agent':>9}{'B':>9}{'gap':>9}{'ctl':>9}{'gates':>8}")
    print("  " + "-" * 50)
    for r in rows:
        print(f"  {r['seed']:>6}{r['agent']:>9.1%}{r['b']:>9.1%}"
              f"{r['gap']:>+9.1%}{r['control']:>9.1%}"
              f"{'OK' if r['gates_ok'] else 'FAIL':>8}")
    print("  " + "-" * 50)
    n = len(rows)
    mean_gap = sum(r["gap"] for r in rows) / n
    print(f"  published canonical  agent {PUBLISHED_AGENT:.1%}  "
          f"B {PUBLISHED_B:.1%}  gap {PUBLISHED_GAP:+.1%}")
    print(f"  mean calibrated gap  {mean_gap:+.1%}  "
          f"(vs published {PUBLISHED_GAP:+.1%})\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--peak-hours", action="store_true", default=False,
        help="After the weight-only run, also generate a separate evening-peak "
             "batch under data/calibrated_peak/. Off by default; never mixed "
             "into the weight-only directory.")
    args = ap.parse_args(argv)

    if not CALIBRATED_CONFIG.exists():
        print(f"missing {CALIBRATED_CONFIG}", file=sys.stderr)
        return 1

    print("\n  NPCI-calibrated weights  (p_resolves unchanged, data/ untouched)")
    weight_rows = _run_seeds(OUT_WEIGHTS, peak_hours=False)
    _print_table("weight-only (NPCI gen_weight)", weight_rows)

    payload = {
        "published": {
            "agent": PUBLISHED_AGENT, "b": PUBLISHED_B, "gap": PUBLISHED_GAP,
        },
        "weight_only": weight_rows,
        "peak_hours": None,
    }

    rc = 0 if all(r["gates_ok"] for r in weight_rows) else 1

    if args.peak_hours:
        if rc:
            print("  skip --peak-hours: weight-only gates failed "
                  "(one change at a time, and only if the first run is clean)\n")
        else:
            print("  evening-peak batch  (calibrated weights + 19:00–22:00; "
                  "separate directory)")
            peak_rows = _run_seeds(OUT_PEAK, peak_hours=True)
            _print_table("calibrated + evening peak", peak_rows)
            payload["peak_hours"] = peak_rows
            if not all(r["gates_ok"] for r in peak_rows):
                rc = 1

    RESULTS_JSON.write_text(json.dumps(payload, indent=2))
    print(f"  wrote {RESULTS_JSON.relative_to(ROOT)}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
