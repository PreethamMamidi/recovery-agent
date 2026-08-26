"""Baseline B — generic SMS at 1h, then retries at 24h / 72h / 120h.

Class-blind. Ignores preferred channel. The world, not the policy, rejects
impossible debits.
"""

from datetime import datetime, timedelta

from simulator.response import Action


def schedule(vis) -> list[Action]:
    failed_at = datetime.fromisoformat(vis.failed_at)
    return [
        Action(
            name="send_reminder",
            at=failed_at + timedelta(hours=1),
            args={"channel": "sms", "template_id": "generic"},
        ),
        Action(
            name="retry_debit",
            at=failed_at + timedelta(hours=24),
            args={"delay_hours": 24},
        ),
        Action(
            name="retry_debit",
            at=failed_at + timedelta(hours=72),
            args={"delay_hours": 72},
        ),
        Action(
            name="retry_debit",
            at=failed_at + timedelta(hours=120),
            args={"delay_hours": 120},
        ),
    ]
