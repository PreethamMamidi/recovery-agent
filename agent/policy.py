"""Rule-based policy. Visible data + taxonomy only. No hidden latents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.actions import Decision, decision
from generator.config import (
    MEASUREMENT_WINDOW_DAYS,
    STRUCTURAL_LIMIT_REASONS,
    load_failure_classes,
)


@dataclass(frozen=True)
class Planned:
    at: datetime
    decision: Decision


PAYDAY_DAYS = (1, 7, 15)   # calendar heuristic; not salary_day


def next_payday_guess(failed_at: datetime) -> datetime:
    """Earliest 1st / 7th / 15th after failed_at, 10:00. Does not use salary_day."""
    window = failed_at + timedelta(days=365)
    found = nearest_paydays(failed_at, window, n=1)
    if not found:
        raise RuntimeError("no payday candidate in the next year")
    return found[0]


def nearest_paydays(failed_at: datetime, window_end: datetime,
                    days: tuple[int, ...] = PAYDAY_DAYS, n: int = 3
                    ) -> list[datetime]:
    """The n nearest heuristic paydays after failed_at that fit in the window.

    Ordered by proximity, not by a preferred calendar sequence. Does not pad.
    """
    start_month = failed_at.replace(day=1, hour=10, minute=0, second=0, microsecond=0)
    found: list[datetime] = []
    for offset in range(0, 5):
        month = (start_month.month - 1 + offset) % 12 + 1
        year = start_month.year + (start_month.month - 1 + offset) // 12
        for day in days:
            try:
                cand = datetime(year, month, day, 10, 0, 0)
            except ValueError:
                continue
            if failed_at < cand <= window_end:
                found.append(cand)
    found.sort()
    uniq: list[datetime] = []
    for cand in found:
        if not uniq or cand != uniq[-1]:
            uniq.append(cand)
        if len(uniq) == n:
            break
    return uniq


def next_limit_reset(failed_at: datetime) -> datetime:
    nxt = (failed_at + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
    if failed_at.hour == 0 and failed_at.minute < 30:
        nxt = failed_at.replace(hour=0, minute=30, second=0, microsecond=0)
    return nxt


def _limit_resets(failed_at: datetime, n: int, window_end: datetime) -> list[datetime]:
    """Next n daily 00:30 boundaries after failed_at that fit in the window."""
    found: list[datetime] = []
    cursor = failed_at
    for _ in range(n):
        cursor = next_limit_reset(cursor)
        if cursor > window_end:
            break
        found.append(cursor)
    return found


def _debit_at(failed_at: datetime, hours: list[int]) -> list[Planned]:
    return [
        Planned(failed_at + timedelta(hours=h),
                decision("retry_debit", delay_hours=h))
        for h in hours
    ]


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
        window_end = failed_at + timedelta(days=MEASUREMENT_WINDOW_DAYS)
        paydays = nearest_paydays(failed_at, window_end, n=fc.max_attempts)
        if has_mandate:
            if not paydays:
                paydays = [failed_at + timedelta(hours=24)]
            return [
                Planned(when, decision(
                    "retry_debit",
                    delay_hours=max(1, int((when - failed_at).total_seconds() // 3600)),
                ))
                for when in paydays
            ]
        when = paydays[0] if paydays else failed_at + timedelta(hours=24)
        return [Planned(when, decision("send_payment_link", channel=ch))]

    if diagnosed_class == "technical_downtime":
        # Exponential-ish backoff. Wait does not consume max_attempts.
        if has_mandate:
            delays = [4, 10, 24][: fc.max_attempts]
            return [
                Planned(failed_at + timedelta(hours=delays[0]),
                        decision("wait_for_downtime_recovery",
                                 recheck_hours=delays[0])),
                *_debit_at(failed_at, delays),
            ]
        clear_at = failed_at + timedelta(hours=6)
        return [
            Planned(clear_at, decision("wait_for_downtime_recovery", recheck_hours=6)),
            Planned(clear_at, decision("send_payment_link", channel="sms")),
            Planned(clear_at + timedelta(hours=24),
                    decision("send_payment_link", channel="whatsapp")),
        ]

    if diagnosed_class == "temporary_lockout":
        # 4x exponential: covers a multi-hour issuer lockout without
        # encoding a specific hidden window. max_attempts = 3.
        if has_mandate:
            return _debit_at(failed_at, [2, 8, 32][: fc.max_attempts])
        cool_at = failed_at + timedelta(hours=24)
        return [
            Planned(cool_at, decision("wait_for_downtime_recovery", recheck_hours=24)),
            Planned(cool_at, decision("send_payment_link", channel="sms")),
            Planned(cool_at + timedelta(hours=24),
                    decision("send_payment_link", channel="whatsapp")),
        ]

    if diagnosed_class == "limit_exceeded":
        if vis.error_reason in STRUCTURAL_LIMIT_REASONS:
            return [Planned(failed_at + timedelta(minutes=5),
                            decision("request_instrument_update", channel=ch))]
        window_end = failed_at + timedelta(days=MEASUREMENT_WINDOW_DAYS)
        resets = _limit_resets(failed_at, n=fc.max_attempts, window_end=window_end)
        if has_mandate:
            return [
                Planned(when, decision(
                    "retry_debit",
                    delay_hours=max(1, int((when - failed_at).total_seconds() // 3600)),
                ))
                for when in resets
            ]
        when = resets[0] if resets else next_limit_reset(failed_at)
        return [Planned(when, decision("send_payment_link", channel=ch))]

    if diagnosed_class == "session_expiry":
        if has_mandate:
            return [
                Planned(failed_at + timedelta(minutes=1),
                        decision("retry_debit", delay_hours=0)),
                Planned(failed_at + timedelta(hours=6),
                        decision("retry_debit", delay_hours=6)),
            ][: fc.max_attempts]
        return [
            Planned(failed_at + timedelta(minutes=2),
                    decision("send_payment_link", channel=ch)),
            Planned(failed_at + timedelta(hours=6),
                    decision("send_payment_link", channel=ch)),
        ][: fc.max_attempts]

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
