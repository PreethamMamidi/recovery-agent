"""Flags for the optional propensity layer. Default is the Day 3 rule path."""

from __future__ import annotations

from dataclasses import dataclass
import random

# Model apps stack in this order. A later app includes the earlier ones.
# channel: rewrite message channel to max EV
# suppress: also drop the message when best EV < 0
# second_ask: score/drop the rule 6h follow-up on customer-action classes
# unconditional_second_ask: leftover alias; the follow-up is now in policy.py
ML_APPS = ("channel", "suppress", "second_ask", "unconditional_second_ask")

# Customer-action classes whose rule schedule has an immediate ask plus a
# 6h follow-up. Session already had that pair in Fix 7; do not double it.
SECOND_ASK_CLASSES = frozenset({
    "customer_input_error",
    "issuer_decline",
    "instrument_invalid",
    "mandate_failure",
})


@dataclass
class MlOptions:
    # Off by default so `python -m eval.run_agent` still is the published floor.
    use_model: bool = False
    app: str = "channel"
    # Train-only: randomly replace preferred_channel so the model sees all
    # three channels. Must stay 0.0 on eval seeds 42/1/2/7/99/123.
    explore_channel: float = 0.0
    rng: random.Random | None = None
    # Optional: apply val-set isotonic calibration at score time.
    calibrated: bool = False
    # Filled by the loop when messages are dropped (class_id, reason).
    dropped: list | None = None
    # Run B: rank second-ask p(step=2) and keep those above this percentile.
    # None keeps the EV floor (p2 * amount > cost).
    p2_percentile: float | None = None
    p2_threshold: float | None = None
