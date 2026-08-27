"""Open-loop planner: diagnose → policy → gate. No simulator imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.actions import DEBIT_ACTIONS, MESSAGE_ACTIONS, TERMINAL, Decision
from agent.diagnose import diagnose
from agent.guardrails import RunContext, apply_quiet_hours_shift, check
from agent.policy import plan
from generator.config import MEASUREMENT_WINDOW_DAYS, load_failure_classes


def _opted_out(customer: dict) -> bool:
    return str(customer.get("opted_out", "")).strip().lower() in {"true", "1", "yes"}


@dataclass
class Step:
    at: datetime
    proposed: Decision
    executed: Decision | None
    gate_result: str
    gate_reason: str
    diagnosed_class: str
    attempt_number: int


def _record(steps, at, proposed, executed, result, reason, diagnosed, attempt) -> None:
    steps.append(Step(
        at=at, proposed=proposed, executed=executed,
        gate_result=result, gate_reason=reason,
        diagnosed_class=diagnosed, attempt_number=attempt,
    ))


def _consume(ctx: RunContext, executed: Decision, max_attempts: int) -> bool:
    """Update context. Return True if the schedule should stop."""
    if executed.action in TERMINAL:
        return True
    if executed.action in DEBIT_ACTIONS or executed.action in MESSAGE_ACTIONS:
        ctx.attempt_number += 1
    if executed.action in MESSAGE_ACTIONS:
        ctx.messages_this_week += 1
    return ctx.attempt_number >= max_attempts


def build_schedule(vis, customer: dict) -> tuple[str, list[Step]]:
    diagnosed = diagnose(vis.error_reason)
    fc = load_failure_classes()[diagnosed]
    failed_at = datetime.fromisoformat(vis.failed_at)
    window_end = failed_at + timedelta(days=MEASUREMENT_WINDOW_DAYS)
    ctx = RunContext(
        attempt_number=0,
        messages_this_week=0,
        opted_out=_opted_out(customer),
    )
    steps: list[Step] = []

    for item in plan(vis, customer, diagnosed):
        at = item.at
        if at < failed_at or at > window_end:
            continue
        proposed = item.decision
        at = apply_quiet_hours_shift(at, proposed)
        gate = check(vis, customer, diagnosed, proposed, ctx, at)

        if gate.allowed and gate.executed is not None:
            _record(steps, at, proposed, gate.executed, "allowed", gate.reason,
                    diagnosed, ctx.attempt_number)
            if _consume(ctx, gate.executed, fc.max_attempts):
                break
            continue

        _record(steps, at, proposed, None, "rejected", gate.reason,
                diagnosed, ctx.attempt_number)

        fallback = gate.executed
        if fallback is None:
            continue
        fb_at = apply_quiet_hours_shift(at, fallback)
        fb_gate = check(vis, customer, diagnosed, fallback, ctx, fb_at)
        use = fallback if (fb_gate.allowed or fallback.action in TERMINAL) else None
        if use is None:
            continue
        _record(steps, fb_at, fallback, use, "allowed",
                fb_gate.reason if fb_gate.allowed else "fallback_terminal",
                diagnosed, ctx.attempt_number)
        if _consume(ctx, use, fc.max_attempts):
            break

    return diagnosed, steps
