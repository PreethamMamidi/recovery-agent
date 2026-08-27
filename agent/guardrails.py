"""Hard gate. Rejected actions are never executed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.actions import (
    AUTONOMOUS,
    DEBIT_ACTIONS,
    MESSAGE_ACTIONS,
    TERMINAL,
    Decision,
    decision,
)
from generator.config import load_failure_classes

QUIET_START_HOUR = 21   # 21:00 inclusive
QUIET_END_HOUR = 9      # 09:00 exclusive  → quiet is [21, 09)
MAX_MESSAGES_PER_WEEK = 3
VALUE_ESCALATE_INR = 25000

AUTONOMOUS_NEED_MANDATE = DEBIT_ACTIONS  # wait is restraint, not a debit


@dataclass
class GateResult:
    allowed: bool
    reason: str
    executed: Decision | None
    fallback_of: Decision | None = None


@dataclass
class RunContext:
    attempt_number: int
    messages_this_week: int
    opted_out: bool
    promise_to_pay_until: datetime | None = None
    last_contact_at: datetime | None = None


def _in_quiet_hours(at: datetime) -> bool:
    return at.hour >= QUIET_START_HOUR or at.hour < QUIET_END_HOUR


def _next_open_window(at: datetime) -> datetime:
    if at.hour < QUIET_END_HOUR:
        return at.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    nxt = (at + timedelta(days=1)).replace(
        hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    return nxt


def _safe_fallback(vis, customer: dict, diagnosed_class: str,
                   original: Decision, ctx: RunContext, at: datetime) -> Decision:
    ch = customer.get("preferred_channel", "sms")
    if ch not in {"sms", "whatsapp", "email"}:
        ch = "sms"
    if ctx.opted_out:
        return decision("mark_uncollectible", reason="opted_out")
    if original.action in TERMINAL:
        return original
    if vis.amount >= VALUE_ESCALATE_INR:
        return decision("escalate", reason="value_threshold")
    fc = load_failure_classes()[diagnosed_class]
    if ctx.attempt_number >= fc.max_attempts:
        return decision("mark_uncollectible", reason="attempt_budget")
    # Downtime / lockout: do not message a blameless customer.
    if diagnosed_class in {"technical_downtime", "temporary_lockout"}:
        return decision("wait_for_downtime_recovery", recheck_hours=6)
    # No mandate / quiet hours / contact cap: ask the customer instead of debiting.
    if diagnosed_class == "instrument_invalid":
        return decision("request_instrument_update", channel=ch)
    if diagnosed_class == "mandate_failure":
        return decision("request_mandate_reauth", channel=ch)
    return decision("send_payment_link", channel=ch)


def check(vis, customer: dict, diagnosed_class: str, planned: Decision,
          ctx: RunContext, at: datetime) -> GateResult:
    fc = load_failure_classes()[diagnosed_class]

    if vis.amount >= VALUE_ESCALATE_INR and planned.action not in TERMINAL:
        fb = decision("escalate", reason="value_threshold")
        return GateResult(False, "value_threshold", fb, planned)

    if ctx.opted_out and planned.action not in TERMINAL:
        fb = decision("mark_uncollectible", reason="opted_out")
        return GateResult(False, "opted_out", fb, planned)

    if ctx.attempt_number >= fc.max_attempts and planned.action not in TERMINAL:
        fb = decision("mark_uncollectible", reason="attempt_budget")
        return GateResult(False, "attempt_budget", fb, planned)

    if planned.action in AUTONOMOUS_NEED_MANDATE and not vis.has_active_mandate:
        fb = _safe_fallback(vis, customer, diagnosed_class, planned, ctx, at)
        return GateResult(False, "mandate_gate", fb, planned)

    if planned.action in MESSAGE_ACTIONS:
        if ctx.promise_to_pay_until is not None and at < ctx.promise_to_pay_until:
            fb = decision("wait_for_downtime_recovery", recheck_hours=24)
            return GateResult(False, "cooling_off", fb, planned)
        if ctx.messages_this_week >= MAX_MESSAGES_PER_WEEK:
            fb = decision("mark_uncollectible", reason="contact_frequency")
            return GateResult(False, "contact_frequency", fb, planned)
        if _in_quiet_hours(at):
            # Not a different action — caller should shift `at`. Signal via reason.
            return GateResult(False, "quiet_hours", None, planned)

    if planned.action in AUTONOMOUS and planned.action not in AUTONOMOUS_NEED_MANDATE:
        pass

    return GateResult(True, "ok", planned, None)


def apply_quiet_hours_shift(at: datetime, planned: Decision) -> datetime:
    if planned.action in MESSAGE_ACTIONS and _in_quiet_hours(at):
        return _next_open_window(at)
    return at
