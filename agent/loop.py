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
from agent.messaging import generate, trai_category
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
    category: str = "service"
    shifted: bool = False


def _record(steps, at, proposed, executed, result, reason, diagnosed, attempt,
            flagged: bool = False, *, category: str = "service",
            shifted: bool = False) -> None:
    steps.append(Step(
        at=at, proposed=proposed, executed=executed,
        gate_result=result, gate_reason=reason,
        diagnosed_class=diagnosed, attempt_number=attempt,
        flagged_for_review=flagged,
        category=category, shifted=shifted,
    ))


def _close(steps, at: datetime, diagnosed: str, ctx: RunContext,
           flagged: bool, reason: str) -> None:
    """Write a terminal action. Silence here is how a case disappears from the log."""
    stop = decision("mark_uncollectible", reason=reason)
    _record(steps, at + timedelta(seconds=1), stop, stop, "allowed", "ok",
            diagnosed, ctx.attempt_number, flagged)


def _close_exhausted(steps, at: datetime, diagnosed: str, ctx: RunContext,
                     flagged: bool) -> None:
    _close(steps, at, diagnosed, ctx, flagged, "attempt_budget")


def _ensure_terminal(steps, *, failed_at: datetime, diagnosed: str,
                     ctx: RunContext, flagged: bool) -> None:
    """Every payment ends in mark_uncollectible or escalate. No silent run-out."""
    executed = [s for s in steps if s.executed]
    last = executed[-1] if executed else None
    if last is not None and last.executed.action in TERMINAL:
        return
    if last is None:
        reason = "no_viable_action"
        at = steps[-1].at if steps else failed_at
    else:
        reason = "schedule_exhausted"
        at = last.at
    _close(steps, at, diagnosed, ctx, flagged, reason)


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


def second_ask_p2(vis, customer: dict, *, calibrated: bool = False) -> float | None:
    """p(step=2) for a proposed extra ask, or None if this payment has none."""
    diagnosed = diagnose(vis.error_reason)
    items = list(plan(vis, customer, diagnosed))
    messages = [it for it in items if it.decision.action in MESSAGE_ACTIONS]
    if diagnosed not in SECOND_ASK_CLASSES or len(messages) < 2:
        return None
    from model.features import delay_hours_of
    from model.score import best_channel
    second = messages[1]
    delay = delay_hours_of(vis, second.at, second.decision)
    _ch, p2, _value = best_channel(
        vis, customer,
        action_type=second.decision.action,
        delay_hours=delay,
        step_index=1,
        calibrated=calibrated,
    )
    return p2


def _rewrite_messages(vis, customer: dict, items: list[Planned],
                      ml: MlOptions) -> list[Planned]:
    """Rules still decide the action type. Model may change channel or drop a send."""
    if not ml.use_model and ml.explore_channel <= 0:
        return items

    out: list[Planned] = []
    step_index = 0
    first_p: float | None = None
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
        from model.score import best_channel, best_channel_lift
        delay = delay_hours_of(vis, item.at, proposed)
        cls = str(getattr(vis, "failure_class", "") or "")
        if (ml.app == "second_ask" and first_p is not None
                and cls in SECOND_ASK_CLASSES):
            ch, p2, value = best_channel_lift(
                vis, customer, p_first=first_p,
                action_type=proposed.action,
                delay_hours=delay,
                step_index=step_index,
                calibrated=ml.calibrated,
            )
            drop = (
                p2 <= ml.p2_threshold
                if ml.p2_threshold is not None
                else value < 0
            )
            if drop:
                if ml.dropped is not None:
                    reason = "p2_quartile" if ml.p2_threshold is not None else "p2_floor"
                    ml.dropped.append((cls, reason))
                step_index += 1
                continue
        else:
            ch, p, value = best_channel(
                vis, customer,
                action_type=proposed.action,
                delay_hours=delay,
                step_index=step_index,
                calibrated=ml.calibrated,
            )
            if first_p is None:
                first_p = p
            if ml.app in {"suppress", "second_ask"} and value < 0:
                if ml.dropped is not None:
                    ml.dropped.append((cls, "ev_floor"))
                step_index += 1
                continue
        proposed = _with_channel(proposed, ch)
        out.append(Planned(item.at, proposed))
        step_index += 1
    return out


def _trai_category(vis, customer: dict, proposed: Decision) -> str:
    if proposed.action not in MESSAGE_ACTIONS:
        return "service"
    return trai_category(generate(vis, customer).body)


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
    items = _rewrite_messages(vis, customer, items, ml)

    for item in items:
        at = item.at
        if at < failed_at or at > window_end:
            continue
        proposed = item.decision
        planned_at = at
        category = _trai_category(vis, customer, proposed)
        at = apply_quiet_hours_shift(at, proposed, category=category)
        shifted = at != planned_at
        gate = check(vis, customer, diagnosed, proposed, ctx, at,
                     category=category)

        if gate.allowed and gate.executed is not None:
            _record(steps, at, proposed, gate.executed, "allowed", gate.reason,
                    diagnosed, ctx.attempt_number, flagged,
                    category=category, shifted=shifted)
            if _consume(ctx, gate.executed, fc.max_attempts):
                if gate.executed.action not in TERMINAL:
                    _close_exhausted(steps, at, diagnosed, ctx, flagged)
                break
            continue

        _record(steps, at, proposed, None, "rejected", gate.reason,
                diagnosed, ctx.attempt_number, flagged,
                category=category, shifted=shifted)

        fallback = gate.executed
        if fallback is None:
            continue
        fb_cat = _trai_category(vis, customer, fallback)
        fb_planned = at
        fb_at = apply_quiet_hours_shift(at, fallback, category=fb_cat)
        fb_gate = check(vis, customer, diagnosed, fallback, ctx, fb_at,
                        category=fb_cat)
        use = fallback if (fb_gate.allowed or fallback.action in TERMINAL) else None
        if use is None:
            continue
        _record(steps, fb_at, fallback, use, "allowed",
                fb_gate.reason if fb_gate.allowed else "fallback_terminal",
                diagnosed, ctx.attempt_number, flagged,
                category=fb_cat, shifted=fb_at != fb_planned)
        if _consume(ctx, use, fc.max_attempts):
            if use.action not in TERMINAL:
                _close_exhausted(steps, fb_at, diagnosed, ctx, flagged)
            break

    _ensure_terminal(
        steps, failed_at=failed_at, diagnosed=diagnosed, ctx=ctx, flagged=flagged,
    )
    return diagnosed, steps
