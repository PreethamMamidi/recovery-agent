"""Flags for the optional propensity layer. Default is the Day 3 rule path."""

from __future__ import annotations

from dataclasses import dataclass
import random

# Apps stack in this order. A later app includes the earlier ones.
# channel: rewrite message channel to max EV
# suppress: also drop the message when best EV < 0
# second_ask: also propose a 6h second message on one-shot customer-action classes
ML_APPS = ("channel", "suppress", "second_ask")

# Classes that today have a single customer-action message. Session already
# has a 6h second step; do not double it.
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
