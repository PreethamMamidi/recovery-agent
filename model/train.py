"""
Fit LGBMClassifier on train seeds 101–108. Hyperparameters are exactly
those in day5-plan.md — no search on eval seeds.

    python -m model.train
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from eval.metrics import ROOT
from model.features import CATEGORICAL, FEATURES
from model.score import ARTIFACT_DIR, CAL_PATH, META_PATH, MODEL_PATH
from model.split import split_customers

TRAIN_SEEDS = (101, 102, 103, 104, 105, 106, 107, 108)
# day5-plan.md §2.1 — do not retune on 42/1/2/7/99/123.
LGBM_PARAMS = dict(
    n_estimators=300,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=30,
    class_weight="balanced",
)


def _load_logs() -> pd.DataFrame:
    frames = []
    for seed in TRAIN_SEEDS:
        path = ROOT / "data" / f"train_{seed}" / "decisions.csv"
        if not path.exists():
            raise FileNotFoundError(f"missing {path} — run python -m eval.run_train_data")
        frames.append(pd.read_csv(path))
    return pd.concat(frames, ignore_index=True)


def _prepare(df: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, pd.Series]:
    X = df[features].copy()
    y = df["recovered"].astype(int)
    for col in CATEGORICAL:
        if col not in X.columns:
            continue
        X[col] = X[col].astype(str).fillna("")
        X[col] = X[col].astype("category")
    return X, y


def _deciles(y_true, y_pred, n: int = 10) -> list[dict]:
    order = y_pred.argsort()
    y_true = y_true.to_numpy()[order]
    y_pred = y_pred[order]
    rows = []
    size = max(1, len(y_true) // n)
    for i in range(n):
        lo = i * size
        hi = len(y_true) if i == n - 1 else (i + 1) * size
        sl_t, sl_p = y_true[lo:hi], y_pred[lo:hi]
        if len(sl_t) == 0:
            continue
        rows.append({
            "decile": i + 1,
            "n": int(len(sl_t)),
            "mean_pred": round(float(sl_p.mean()), 4),
            "mean_actual": round(float(sl_t.mean()), 4),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    import lightgbm as lgb
    import joblib
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.isotonic import IsotonicRegression

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--drop", nargs="*", default=[],
        help="Feature names to drop (e.g. amount lifetime_value). "
             "Does not overwrite the default artifact.")
    args = ap.parse_args(argv)

    features = [f for f in FEATURES if f not in set(args.drop)]
    write = not args.drop

    raw = _load_logs()
    train_ids, val_ids = split_customers(raw["customer_id"].astype(str).tolist())
    if train_ids & val_ids:
        raise RuntimeError("customer split leaked")
    train_df = raw[raw["customer_id"].astype(str).isin(train_ids)]
    val_df = raw[raw["customer_id"].astype(str).isin(val_ids)]
    X_train, y_train = _prepare(train_df, features)
    X_val, y_val = _prepare(val_df, features)
    cats = [c for c in CATEGORICAL if c in features]
    cat_values = {c: list(X_train[c].cat.categories) for c in cats}
    for c in cats:
        X_val[c] = pd.Categorical(X_val[c].astype(str), categories=cat_values[c])

    pos = float(y_train.mean())
    model = lgb.LGBMClassifier(**LGBM_PARAMS, verbosity=-1)
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], eval_metric="auc")

    p_val = model.predict_proba(X_val)[:, 1]
    auc = float(roc_auc_score(y_val, p_val))
    pr_auc = float(average_precision_score(y_val, p_val))
    importances = dict(zip(features, (float(x) for x in model.feature_importances_)))
    top = sorted(importances.items(), key=lambda kv: -kv[1])
    calib = _deciles(y_val, p_val)

    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p_val, y_val)
    p_cal = iso.predict(p_val)
    cal_deciles = _deciles(y_val, p_cal)
    # sklearn wrapper kept so channel EV can load CalibratedClassifierCV
    calibrated = CalibratedClassifierCV(model, method="isotonic", cv="prefit")
    calibrated.fit(X_val, y_val)

    print(f"\n  features {len(features)}  dropped {args.drop or 'none'}")
    print(f"  train rows {len(X_train)}  val rows {len(X_val)}"
          f"  (customers {len(train_ids)}/{len(val_ids)})")
    print(f"  positive rate  train {pos:.3f}  val {float(y_val.mean()):.3f}")
    print(f"  ROC-AUC  {auc:.3f}")
    print(f"  PR-AUC   {pr_auc:.3f}")
    print("  importances")
    for name, val in top:
        print(f"    {name:<22} {val:.1f}")
    print("  calibration (val deciles, raw)")
    print(f"    {'dec':>5}{'n':>7}{'pred':>9}{'actual':>9}")
    for row in calib:
        print(f"    {row['decile']:>5}{row['n']:>7}"
              f"{row['mean_pred']:>9.3f}{row['mean_actual']:>9.3f}")
    print("  calibration (val deciles, isotonic)")
    for row in cal_deciles:
        print(f"    {row['decile']:>5}{row['n']:>7}"
              f"{row['mean_pred']:>9.3f}{row['mean_actual']:>9.3f}")

    leak = auc > 0.90
    usable = 0.70 <= auc < 0.90
    if leak:
        print("\n  STOP  AUC > 0.90 — hunt a leak; do not drive EV.\n")
        return 1
    if not usable:
        print("\n  NOTE  AUC outside 0.70–0.90 — converting-step labels are "
              "sparse; still writing so apps can be measured.\n")
    if auc > 0.85:
        print("\n  NOTE  AUC > 0.85 hoped band; below leak tripwire. "
              "Inspect importances (hidden columns must not dominate).\n")
    if top[0][0] == "failure_class":
        print("\n  NOTE  failure_class is the top importance — taxonomy relearn risk.\n")

    if write:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        model.booster_.save_model(str(MODEL_PATH))
        joblib.dump(iso, CAL_PATH)
        joblib.dump(calibrated, ARTIFACT_DIR / "calibrated.joblib")
        meta = {
            "train_seeds": list(TRAIN_SEEDS),
            "n_train_rows": int(len(X_train)),
            "n_val_rows": int(len(X_val)),
            "n_train_customers": len(train_ids),
            "n_val_customers": len(val_ids),
            "params": LGBM_PARAMS,
            "features": features,
            "positive_rate_train": round(pos, 4),
            "positive_rate_val": round(float(y_val.mean()), 4),
            "roc_auc": round(auc, 4),
            "pr_auc": round(pr_auc, 4),
            "label": "converting_step",
            "feature_importances": importances,
            "calibration_deciles": calib,
            "calibration_deciles_isotonic": cal_deciles,
            "categorical_values": cat_values,
            "usable_for_ev": usable,
            "leak_suspect": leak,
            "above_hoped_band": auc > 0.85,
            "failure_class_dominates": top[0][0] == "failure_class",
        }
        META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"\n  wrote {MODEL_PATH.relative_to(ROOT)}\n")
    else:
        print("\n  (no artifact written)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
