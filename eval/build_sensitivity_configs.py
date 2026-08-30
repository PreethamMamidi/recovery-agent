"""
Build p_resolves ±0.1 configs from the canonical CSV.

    python -m eval.build_sensitivity_configs

Writes config/sensitivity_pessimistic.csv and sensitivity_optimistic.csv.
Does not hand-edit config/failure_classes.csv. Canonical sensitivity row
is the existing data/ batch (same seed 42, same priors) — do not regenerate it.

All classes shift together: this tests a systematically different world,
not per-class attribution. Clamped to [0, 1].
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "config" / "failure_classes.csv"
DELTA = 0.1  # one-tenth; enough to move control, not a rewrite of the taxonomy


def _shift(path: Path, delta: float) -> list[dict]:
    with CANONICAL.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            p = float(row["p_resolves"])
            # Clamp so 1.00+0.1 does not become a nonsense prior, and 0.10-0.1
            # does not go negative. Every other column, including gen_weight,
            # is copied verbatim so this lever stays independent of calibration.
            row["p_resolves"] = f"{max(0.0, min(1.0, p + delta)):.2f}"
            rows.append(row)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return rows


def main() -> int:
    pes = ROOT / "config" / "sensitivity_pessimistic.csv"
    opt = ROOT / "config" / "sensitivity_optimistic.csv"
    _shift(pes, -DELTA)
    _shift(opt, +DELTA)
    print(f"  wrote {pes.relative_to(ROOT)}  (p_resolves - {DELTA})")
    print(f"  wrote {opt.relative_to(ROOT)}  (p_resolves + {DELTA})")
    print("  canonical row = existing data/  (do not regenerate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
