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
CAL_PATH = ARTIFACT_DIR / "isotonic.joblib"

CHANNELS = ("sms", "whatsapp", "email")

_booster = None
_meta = None
_isotonic = None


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


def _calibrate(p: float) -> float:
    global _isotonic
    if _isotonic is None:
        if not CAL_PATH.exists():
            return p
        import joblib
        _isotonic = joblib.load(CAL_PATH)
    out = float(_isotonic.predict([p])[0])
    return min(1.0, max(0.0, out))


def _frame(rows: list[dict]):
    booster, meta = _load()
    pd = meta["_pd"]
    frame = pd.DataFrame(rows, columns=FEATURES)
    cats = meta.get("categorical_values", {})
    for col in CATEGORICAL:
        frame[col] = pd.Categorical(frame[col], categories=cats.get(col, None))
    return booster, frame


def predict_proba(vis, customer: dict, *, action_type: str, channel: str,
                  delay_hours: float, step_index: int,
                  calibrated: bool = False) -> float:
    row = extract(
        vis, customer, action_type=action_type, channel=channel,
        delay_hours=delay_hours, step_index=step_index,
    )
    booster, frame = _frame([row])
    pred = float(booster.predict(frame)[0])
    if calibrated:
        pred = _calibrate(pred)
    return pred


def best_channel(vis, customer: dict, *, action_type: str, delay_hours: float,
                 step_index: int, calibrated: bool = False
                 ) -> tuple[str, float, float]:
    """Return (channel, p, ev) with the highest expected value."""
    best_ch, best_p, best_ev = "sms", 0.0, float("-inf")
    amount = float(vis.amount)
    for ch in CHANNELS:
        p = predict_proba(
            vis, customer, action_type=action_type, channel=ch,
            delay_hours=delay_hours, step_index=step_index,
            calibrated=calibrated,
        )
        value = ev(p, amount, ch)
        if value > best_ev:
            best_ch, best_p, best_ev = ch, p, value
    return best_ch, best_p, best_ev


def best_channel_lift(vis, customer: dict, *, p_first: float, action_type: str,
                      delay_hours: float, step_index: int,
                      calibrated: bool = False) -> tuple[str, float, float]:
    """Second-ask EV is p(step=2) * amount - cost, not lift vs the first ask."""
    _ = p_first  # kept in the signature; floor is p2 * amount, not lift
    best_ch, best_p, best_ev = "sms", 0.0, float("-inf")
    amount = float(vis.amount)
    for ch in CHANNELS:
        p2 = predict_proba(
            vis, customer, action_type=action_type, channel=ch,
            delay_hours=delay_hours, step_index=step_index,
            calibrated=calibrated,
        )
        value = p2 * amount - MESSAGE_COST[ch]
        if value > best_ev:
            best_ch, best_p, best_ev = ch, p2, value
    return best_ch, best_p, best_ev
