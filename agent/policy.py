"""Rule-based policy. Visible data + taxonomy only. No hidden latents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.actions import Decision, decision
from generator.config import STRUCTURAL_LIMIT_REASONS, load_failure_classes


@dataclass(frozen=True)
class Planned:
    at: datetime
    decision: Decision


def next_payday_guess(failed_at: datetime) -> datetime:
    """Next 1st or 7th after failed_at, 10:00. Does not use salary_day."""
    candidates: list[datetime] = []
    start_month = failed_at.replace(day=1, hour=10, minute=0, second=0, microsecond=0)
    for offset in range(0, 4):
        month = (start_month.month - 1 + offset) % 12 + 1
        year = start_month.year + (start_month.month - 1 + offset) // 12
        for day in (1, 7):
            cand = datetime(year, month, day, 10, 0, 0)
            if cand > failed_at:
                candidates.append(cand)
    return min(candidates)


def next_limit_reset(failed_at: datetime) -> datetime:
    nxt = (failed_at + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
    if failed_at.hour == 0 and failed_at.minute < 30:
        nxt = failed_at.replace(hour=0, minute=30, second=0, microsecond=0)
    return nxt


def _channel(preferred: str) -> str:
    if preferred in {"sms", "whatsapp", "email"}:
        return preferred
    return "sms"


def plan(vis, customer: dict, diagnosed_class: str) -> list[Planned]:
    """Build an open-loop schedule from the taxonomy row."""
    failed_at = datetime.fromisoformat(vis.failed_at)
    ch = _channel(customer.get("preferred_channel", "sms"))
    fc = load_failure_classes()[diagnosed_class]
    has_mandate = bool(vis.has_active_mandate)

    if diagnosed_class == "insufficient_funds":
        when = next_payday_guess(failed_at)
        return [Planned(when, decision("schedule_for_payday",
                                       target_date=when.date().isoformat()))]

    if diagnosed_class == "technical_downtime":
        wait_at = failed_at + timedelta(hours=4)
        return [
            Planned(wait_at, decision("wait_for_downtime_recovery", recheck_hours=4)),
            Planned(failed_at + timedelta(hours=6),
                    decision("retry_debit", delay_hours=6)),
            Planned(failed_at + timedelta(hours=12),
                    decision("retry_debit", delay_hours=12)),
        ]

    if diagnosed_class == "temporary_lockout":
        delays = [2, 6, 24][: fc.max_attempts]
        return [
            Planned(failed_at + timedelta(hours=h),
                    decision("retry_debit", delay_hours=h))
            for h in delays
        ]

    if diagnosed_class == "limit_exceeded":
        if vis.error_reason in STRUCTURAL_LIMIT_REASONS:
            return [Planned(failed_at + timedelta(minutes=5),
                            decision("request_instrument_update", channel=ch))]
        when = next_limit_reset(failed_at)
        delay = max(1, int((when - failed_at).total_seconds() // 3600))
        return [Planned(when, decision("retry_debit", delay_hours=delay))]

    if diagnosed_class == "session_expiry":
        if has_mandate:
            return [Planned(failed_at + timedelta(minutes=1),
                            decision("retry_debit", delay_hours=0))]
        return [Planned(failed_at + timedelta(minutes=2),
                        decision("send_payment_link", channel=ch))]

    if diagnosed_class == "customer_input_error":
        return [Planned(failed_at + timedelta(minutes=2),
                        decision("send_payment_link", channel=ch))]

    if diagnosed_class == "instrument_invalid":
        return [Planned(failed_at + timedelta(minutes=2),
                        decision("request_instrument_update", channel=ch))]

    if diagnosed_class == "issuer_decline":
        return [Planned(failed_at + timedelta(minutes=2),
                        decision("send_payment_link", channel=ch))]

    if diagnosed_class == "mandate_failure":
        return [Planned(failed_at + timedelta(minutes=2),
                        decision("request_mandate_reauth", channel=ch))]

    raise ValueError(f"no policy for class {diagnosed_class!r}")
