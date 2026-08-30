"""Open-loop planner: diagnose → policy → gate. No simulator imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.actions import DEBIT_ACTIONS, MESSAGE_ACTIONS, TERMINAL, Decision, decision
from agent.diagnose import diagnose
from agent.guardrails import (
    VALUE_ESCALATE_INR,
    RunContext,
    apply_quiet_hours_shift,
    check,
)
from agent.ml_options import SECOND_ASK_CLASSES, MlOptions
from agent.policy import Planned, plan
from generator.config import MEASUREMENT_WINDOW_DAYS, load_failure_classes

CHANNELS = ("sms", "whatsapp", "email")


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
    flagged_for_review: bool = False


def _record(steps, at, proposed, executed, result, reason, diagnosed, attempt,
            flagged: bool = False) -> None:
    steps.append(Step(
        at=at, proposed=proposed, executed=executed,
        gate_result=result, gate_reason=reason,
        diagnosed_class=diagnosed, attempt_number=attempt,
        flagged_for_review=flagged,
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


def _with_channel(proposed: Decision, channel: str) -> Decision:
    args = dict(proposed.args)
    args["channel"] = channel
    return decision(proposed.action, **args)


def _maybe_second_ask(vis, diagnosed: str, items: list[Planned]) -> list[Planned]:
    """Propose a 6h follow-up on one-shot customer-action classes. Model keeps it."""
    if diagnosed not in SECOND_ASK_CLASSES:
        return items
    messages = [it for it in items if it.decision.action in MESSAGE_ACTIONS]
    if len(messages) != 1:
        return items
    first = messages[0]
    follow = Planned(first.at + timedelta(hours=6), first.decision)
    out = list(items)
    out.append(follow)
    return out


def _rewrite_messages(vis, customer: dict, items: list[Planned],
                      ml: MlOptions) -> list[Planned]:
    """Rules still decide the action type. Model may change channel or drop a send."""
    if not ml.use_model and ml.explore_channel <= 0:
        return items

    out: list[Planned] = []
    step_index = 0
    for item in items:
        proposed = item.decision
        if proposed.action not in MESSAGE_ACTIONS:
            out.append(item)
            continue
        if ml.explore_channel > 0 and ml.rng is not None:
            if ml.rng.random() < ml.explore_channel:
                proposed = _with_channel(proposed, ml.rng.choice(CHANNELS))
            out.append(Planned(item.at, proposed))
            step_index += 1
            continue
        if not ml.use_model:
            out.append(item)
            step_index += 1
            continue
        from model.features import delay_hours_of
        from model.score import best_channel
        delay = delay_hours_of(vis, item.at, proposed)
        ch, _p, value = best_channel(
            vis, customer,
            action_type=proposed.action,
            delay_hours=delay,
            step_index=step_index,
        )
        if ml.app in {"suppress", "second_ask"} and value < 0:
            step_index += 1
            continue
        proposed = _with_channel(proposed, ch)
        out.append(Planned(item.at, proposed))
        step_index += 1
    return out


def build_schedule(vis, customer: dict,
                   ml: MlOptions | None = None) -> tuple[str, list[Step]]:
    ml = ml or MlOptions()
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
    flagged = vis.amount >= VALUE_ESCALATE_INR

    items = list(plan(vis, customer, diagnosed))
    if ml.use_model and ml.app == "second_ask":
        items = _maybe_second_ask(vis, diagnosed, items)
    items = _rewrite_messages(vis, customer, items, ml)

    for item in items:
        at = item.at
        if at < failed_at or at > window_end:
            continue
        proposed = item.decision
        at = apply_quiet_hours_shift(at, proposed)
        gate = check(vis, customer, diagnosed, proposed, ctx, at)

        if gate.allowed and gate.executed is not None:
            _record(steps, at, proposed, gate.executed, "allowed", gate.reason,
                    diagnosed, ctx.attempt_number, flagged)
            if _consume(ctx, gate.executed, fc.max_attempts):
                break
            continue

        _record(steps, at, proposed, None, "rejected", gate.reason,
                diagnosed, ctx.attempt_number, flagged)

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
                diagnosed, ctx.attempt_number, flagged)
        if _consume(ctx, use, fc.max_attempts):
            break

    return diagnosed, steps
