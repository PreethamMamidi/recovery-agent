"""
Step-level labels. Credit the converting action, not the payment.

The simulator stamps recovered_at at the converting action's timestamp,
so the credited step is the last executed action with at <= recovered_at
(not strictly <, which would never match).
"""

from __future__ import annotations

from datetime import datetime


def converting_step_labels(
    step_at: list[datetime],
    recovered: bool,
    recovered_at: datetime | None,
    source: str,
) -> list[int]:
    """One 0/1 per step.

    Recovered via an action → last step with at <= recovered_at gets 1.
    Recovered naturally → every step 0 (do not credit the control path).
    Not recovered → every step 0.
    """
    n = len(step_at)
    labels = [0] * n
    if not recovered or source != "action" or recovered_at is None:
        return labels
    credited = None
    for i, at in enumerate(step_at):
        if at <= recovered_at:
            credited = i
    if credited is not None:
        labels[credited] = 1
    return labels
