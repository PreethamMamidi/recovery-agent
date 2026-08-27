"""Baseline C — aggressive dunning.

Five messages over 14 days plus a spray of retries. Class-blind on purpose.
This exists to fire the annoyance penalty that A and B never reach.
"""

from datetime import datetime, timedelta

from simulator.response import Action


def schedule(vis) -> list[Action]:
    failed_at = datetime.fromisoformat(vis.failed_at)
    messages = [1, 24, 72, 168, 288]          # 1h, 1d, 3d, 7d, 12d
    retries = [6, 24, 48, 96]
    actions = [
        Action(
            name="send_reminder",
            at=failed_at + timedelta(hours=h),
            args={"channel": "sms", "template_id": "generic"},
        )
        for h in messages
    ]
    actions.extend(
        Action(
            name="retry_debit",
            at=failed_at + timedelta(hours=h),
            args={"delay_hours": h},
        )
        for h in retries
    )
    return actions
