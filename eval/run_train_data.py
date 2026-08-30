"""
Generate training logs on seeds never used for evaluation.

    python -m eval.run_train_data

Seeds 101–108 only. Not 42, 1, 2, 7, 99, 123. Writes data/train_S and
decisions.csv with 30% channel exploration so the model sees all three
channels. Canonical data/ is not touched.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from agent.ml_options import MlOptions
from eval.metrics import ROOT, load_world
from eval.run_agent import run_agent
from generator.generate import generate

# Held-out forever: 42, 1, 2, 7, 99, 123.
TRAIN_SEEDS = (101, 102, 103, 104, 105, 106, 107, 108)
EXPLORE = 0.30


def main() -> int:
    for seed in TRAIN_SEEDS:
        out = ROOT / "data" / f"train_{seed}"
        print(f"  generate seed={seed} -> {out.relative_to(ROOT)}")
        generate(1000, seed=seed, out_dir=out)
        world = load_world(out)
        ml = MlOptions(
            use_model=False,
            explore_channel=EXPLORE,
            rng=random.Random(seed),
        )
        log_path = out / "decisions.csv"
        agent, conn = run_agent(world, ml=ml, log_path=log_path, seed=seed)
        conn.close()
        print(f"    logged {log_path}  treatment rec={agent.recovery_rate:.1%}  "
              f"n={agent.n}")
    print("  data/ untouched. eval seeds were not generated.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
