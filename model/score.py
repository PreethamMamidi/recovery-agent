"""
Load the fitted booster and score P(recover | visible, action, channel).

Imported by agent/loop when --use-model is on. Must not import simulator,
generator.latents, or generator.natural_recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

from config.costs import MESSAGE_COST
from model.features import CATEGORICAL, FEATURES, extract

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "model" / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "propensity.txt"
META_PATH = ARTIFACT_DIR / "propensity_meta.json"

CHANNELS = ("sms", "whatsapp", "email")

_booster = None
_meta = None


def ev(p: float, amount: float, channel: str) -> float:
    return p * float(amount) - MESSAGE_COST[channel]


def _load():
    global _booster, _meta
    if _booster is not None:
        return _booster, _meta
    if not MODEL_PATH.exists() or not META_PATH.exists():
        raise FileNotFoundError(
            f"no propensity artifact at {MODEL_PATH} — run python -m model.train"
        )
    import lightgbm as lgb  # lazy so --use-model off does not need the wheel
    import pandas as pd
    _booster = lgb.Booster(model_file=str(MODEL_PATH))
    _meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    _meta["_pd"] = pd
    return _booster, _meta


def _frame(rows: list[dict]):
    booster, meta = _load()
    pd = meta["_pd"]
    frame = pd.DataFrame(rows, columns=FEATURES)
    cats = meta.get("categorical_values", {})
    for col in CATEGORICAL:
        frame[col] = pd.Categorical(frame[col], categories=cats.get(col, None))
    return booster, frame


def predict_proba(vis, customer: dict, *, action_type: str, channel: str,
                  delay_hours: float, step_index: int) -> float:
    row = extract(
        vis, customer, action_type=action_type, channel=channel,
        delay_hours=delay_hours, step_index=step_index,
    )
    booster, frame = _frame([row])
    pred = booster.predict(frame)
    return float(pred[0])


def best_channel(vis, customer: dict, *, action_type: str, delay_hours: float,
                 step_index: int) -> tuple[str, float, float]:
    """Return (channel, p, ev) with the highest expected value."""
    best_ch, best_p, best_ev = "sms", 0.0, float("-inf")
    amount = float(vis.amount)
    for ch in CHANNELS:
        p = predict_proba(
            vis, customer, action_type=action_type, channel=ch,
            delay_hours=delay_hours, step_index=step_index,
        )
        value = ev(p, amount, ch)
        if value > best_ev:
            best_ch, best_p, best_ev = ch, p, value
    return best_ch, best_p, best_ev
