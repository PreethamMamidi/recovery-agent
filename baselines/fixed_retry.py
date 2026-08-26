"""Baseline A — one retry at 24h, no messaging. Class-blind on purpose."""

from datetime import datetime, timedelta

from simulator.response import Action


def schedule(vis) -> list[Action]:
    failed_at = datetime.fromisoformat(vis.failed_at)
    return [
        Action(
            name="retry_debit",
            at=failed_at + timedelta(hours=24),
            args={"delay_hours": 24},
        ),
    ]
