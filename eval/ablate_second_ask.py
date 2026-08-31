"""
Three-way second-ask ablation. No taxonomy / policy.py edits.

    python -m eval.ablate_second_ask

  rules                         published floor, no extra ask
  unconditional_second_ask      rules + 6h follow-up on the four
                                customer-action classes, everyone, no model
  second_ask                    same extra ask, model-filtered
                                (live if artifacts exist, else the published
                                eval/ml_second_ask_compare.json row)

Seed 42 uses data/. Other eval seeds go to a temp dir.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from agent.ml_options import MlOptions
from eval.metrics import DATA, ROOT, identity_mismatches, load_world
from eval.run_agent import run_agent
from generator.generate import generate
from model.score import MODEL_PATH

EVAL_SEEDS = (42, 1, 2, 7, 99, 123)
PUBLISHED_MODEL = ROOT / "eval" / "ml_second_ask_compare.json"


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
        "by_class_recovery": {
            cid: round(b["recovered"] / b["n"], 6) if b.get("n") else 0.0
            for cid, b in p.by_class.items()
        },
        "by_class_messages": {
            cid: int(b.get("messages", 0)) for cid, b in p.by_class.items()
        },
    }


def _published_model(seed: int) -> dict | None:
    if not PUBLISHED_MODEL.exists():
        return None
    payload = json.loads(PUBLISHED_MODEL.read_text(encoding="utf-8"))
    for row in payload.get("rows", []):
        if row.get("seed") == seed:
            return row["model"]
    return None


def main() -> int:
    have_model = MODEL_PATH.exists()
    tmp = Path(tempfile.mkdtemp(prefix="ablate_2nd_"))
    rows = []
    try:
        print(f"\n  model second_ask: "
              f"{'live artifact' if have_model else 'published compare json'}")
        print(f"  {'seed':>6}{'rules':>9}{'uncond':>9}{'model':>9}"
              f"{'u-r':>8}{'m-u':>8}{'w/i':>6}")
        print("  " + "-" * 55)
        for seed in EVAL_SEEDS:
            world = _eval_world(seed, tmp)
            ident = identity_mismatches(world)
            rules, conn = run_agent(world)
            conn.close()
            uncond, conn = run_agent(
                world,
                ml=MlOptions(use_model=False, app="unconditional_second_ask"),
            )
            conn.close()
            if have_model:
                model, conn = run_agent(
                    world,
                    ml=MlOptions(use_model=True, app="second_ask"),
                )
                conn.close()
                model_pack = _pack(model, ident)
                model_rec = model.recovery_rate
                model_waste = model.wasted_debits + model.impossible_debits
            else:
                model_pack = _published_model(seed) or {}
                model_rec = float(model_pack.get("recovery", 0.0))
                model_waste = int(model_pack.get("wasted", 0)) + int(
                    model_pack.get("impossible", 0))
            waste = (uncond.wasted_debits + uncond.impossible_debits + model_waste)
            print(f"  {seed:>6}{rules.recovery_rate:>9.1%}{uncond.recovery_rate:>9.1%}"
                  f"{model_rec:>9.1%}"
                  f"{uncond.recovery_rate - rules.recovery_rate:>+8.1%}"
                  f"{model_rec - uncond.recovery_rate:>+8.1%}"
                  f"{'ok' if waste == 0 and ident == 0 else 'FAIL':>6}")
            rows.append({
                "seed": seed,
                "rules": _pack(rules, ident),
                "unconditional": _pack(uncond, ident),
                "model": model_pack,
                "model_source": "live" if have_model else "published_json",
            })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out = ROOT / "eval" / "ml_second_ask_ablation.json"
    out.write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")
    print(f"\n  wrote {out.relative_to(ROOT)}\n")
    bad = any(
        (not r["unconditional"]["identity_ok"])
        or r["unconditional"]["wasted"] or r["unconditional"]["impossible"]
        for r in rows
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
