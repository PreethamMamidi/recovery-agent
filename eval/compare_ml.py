"""
Rules vs one ML app on the six eval seeds. Does not train.

    python -m eval.compare_ml --ml-app channel

Seed 42 uses existing data/. Seeds 1, 2, 7, 99, 123 are generated into
temp dirs. Channel exploration stays 0. Accept on 5/6 if recovery or
net value beats the rule path on that seed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from agent.ml_options import ML_APPS, MlOptions
from eval.metrics import DATA, ROOT, identity_mismatches, load_world
from eval.run_agent import run_agent
from generator.generate import generate

EVAL_SEEDS = (42, 1, 2, 7, 99, 123)


def _eval_world(seed: int, tmp_root: Path):
    if seed == 42:
        return load_world(DATA)
    out = tmp_root / f"seed_{seed}"
    generate(1000, seed=seed, out_dir=out)
    return load_world(out)


def _pack(p, ident: int) -> dict:
    return {
        "recovery": round(p.recovery_rate, 6),
        "net_value": round(p.net_value, 2),
        "messages": p.messages,
        "wasted": p.wasted_debits,
        "impossible": p.impossible_debits,
        "identity_ok": ident == 0,
        "n_suppressed": int(getattr(p, "n_suppressed", 0)),
        "suppress_by_class": getattr(p, "suppress_by_class", {}),
        "p2_threshold": getattr(p, "p2_threshold", None),
        "p2_candidates_by_class": getattr(p, "p2_candidates_by_class", {}),
        "p2_quartile_by_class": getattr(p, "p2_quartile_by_class", {}),
        "by_class_messages": {
            cid: int(b.get("messages", 0)) for cid, b in p.by_class.items()
        },
        "by_class_n": {cid: int(b.get("n", 0)) for cid, b in p.by_class.items()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ml-app", choices=ML_APPS, default="channel")
    ap.add_argument("--calibrated", action="store_true", default=False)
    ap.add_argument("--p2-percentile", type=float, default=None,
                    help="second_ask only: keep extra asks with p2 above this percentile")
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="ml_eval_"))
    rows = []
    try:
        for seed in EVAL_SEEDS:
            print(f"  seed {seed}")
            world = _eval_world(seed, tmp)
            ident = identity_mismatches(world)
            rules, conn = run_agent(world)
            conn.close()
            model, conn = run_agent(
                world, ml=MlOptions(
                    use_model=True, app=args.ml_app, dropped=[],
                    calibrated=args.calibrated,
                    p2_percentile=args.p2_percentile,
                ),
            )
            conn.close()
            rec_win = model.recovery_rate + 1e-12 >= rules.recovery_rate
            net_win = model.net_value + 1e-6 >= rules.net_value
            win = rec_win or net_win
            row = {
                "seed": seed,
                "win": win,
                "rec_win": rec_win,
                "net_win": net_win,
                "rules": _pack(rules, ident),
                "model": _pack(model, ident),
            }
            rows.append(row)
            print(f"    rules rec={rules.recovery_rate:.1%} net={rules.net_value:,.0f}  "
                  f"model rec={model.recovery_rate:.1%} net={model.net_value:,.0f}  "
                  f"drop={getattr(model, 'n_suppressed', 0)}  "
                  f"{'WIN' if win else 'LOSE'}")
            dropped = getattr(model, "suppress_by_class", {}) or {}
            if dropped:
                bits = " ".join(f"{k}={v}" for k, v in sorted(dropped.items()))
                print(f"    suppress by class: {bits}")
            quartile = getattr(model, "p2_quartile_by_class", {}) or {}
            cand = getattr(model, "p2_candidates_by_class", {}) or {}
            if cand:
                thr = getattr(model, "p2_threshold", None)
                qbits = " ".join(
                    f"{k}={quartile.get(k, 0)}/{v}" for k, v in sorted(cand.items())
                )
                print(f"    p2 threshold={thr:.4f}  quartile: {qbits}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n_win = sum(1 for r in rows if r["win"])
    print(f"\n  {args.ml_app}: {n_win}/6 seeds beat rules on recovery or net")
    print(f"  accept={'YES' if n_win >= 5 else 'NO'}  (bar is 5/6)\n")
    tag = args.ml_app
    if args.p2_percentile is not None:
        tag = f"{args.ml_app}_quartile"
    out = ROOT / "eval" / f"ml_{tag}_compare.json"
    out.write_text(json.dumps({
        "app": args.ml_app,
        "p2_percentile": args.p2_percentile,
        "wins": n_win,
        "accept": n_win >= 5,
        "rows": rows,
    }, indent=2), encoding="utf-8")
    return 0 if n_win >= 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
