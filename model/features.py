"""
Visible-only feature builder for the propensity model.

Nothing here may read generator.latents, generator.natural_recovery,
payments_hidden, or ground_truth. Those columns are listed so the leak
test can assert FEATURES does not intersect them.
"""

from __future__ import annotations

import math
from datetime import datetime

# Payment-level recovery used to be the label (smeared across steps).
# Labels are now converting-step: see model.labels.converting_step_labels.
FEATURES = [
    "amount", "log_amount", "method", "failure_class",
    "has_active_mandate", "attempt_number",
    "days_until_due", "failed_hour", "failed_day_of_month",
    "tenure_months", "past_payment_count", "past_failure_count",
    "failure_ratio",
    "payments_per_month",
    "lifetime_value", "preferred_channel", "opted_out",
    "action_type", "channel", "delay_hours", "step_index",
]

CATEGORICAL = [
    "method", "failure_class", "preferred_channel", "action_type", "channel",
]

# Columns that must never appear in FEATURES. Source: the hidden CSVs / latents.
LATENT_COLUMNS = {
    "salary_day", "true_intent_to_pay", "reattempt_propensity",
    "annoyance_threshold", "resp_sms", "resp_whatsapp", "resp_email",
    "tech_savviness",
}
PAYMENT_HIDDEN_COLUMNS = {
    "downtime_ends_at", "lockout_ends_at", "limit_resets_at",
    "is_structural_limit", "is_deliberate_abandon",
}
GROUND_TRUTH_COLUMNS = {
    "would_have_recovered_naturally", "natural_recovery_date", "p_natural_used",
    "arm",
}


def hidden_columns() -> set[str]:
    return LATENT_COLUMNS | PAYMENT_HIDDEN_COLUMNS | GROUND_TRUTH_COLUMNS


def _bool(v) -> int:
    return int(str(v).strip().lower() in {"true", "1", "yes"})


def _float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def extract(
    vis,
    customer: dict,
    *,
    action_type: str,
    channel: str,
    delay_hours: float,
    step_index: int,
) -> dict:
    """One scoring row from merchant-visible fields plus the proposed action."""
    amount = _float(getattr(vis, "amount", 0))
    tenure = max(_float(customer.get("tenure_months"), 1.0), 1.0)
    past_pay = _float(customer.get("past_payment_count"), 0.0)
    past_fail = _float(customer.get("past_failure_count"), 0.0)
    failed_at = datetime.fromisoformat(str(vis.failed_at))
    due_raw = getattr(vis, "invoice_due_date", None)
    if due_raw:
        due = datetime.fromisoformat(str(due_raw)).date()
        days_until_due = (due - failed_at.date()).days
    else:
        days_until_due = 0
    return {
        "amount": amount,
        "log_amount": math.log1p(max(amount, 0.0)),
        "method": str(getattr(vis, "method", "") or ""),
        "failure_class": str(getattr(vis, "failure_class", "") or ""),
        "has_active_mandate": _bool(getattr(vis, "has_active_mandate", False)),
        "attempt_number": int(getattr(vis, "attempt_number", 1) or 1),
        "days_until_due": days_until_due,
        "failed_hour": failed_at.hour,
        "failed_day_of_month": failed_at.day,
        "tenure_months": tenure,
        "past_payment_count": past_pay,
        "past_failure_count": past_fail,
        "failure_ratio": past_fail / past_pay if past_pay else 0.0,
        "payments_per_month": past_pay / tenure,
        "lifetime_value": _float(customer.get("lifetime_value"), 0.0),
        "preferred_channel": str(customer.get("preferred_channel") or "sms"),
        "opted_out": _bool(customer.get("opted_out", False)),
        "action_type": action_type,
        "channel": channel or "",
        "delay_hours": float(delay_hours),
        "step_index": int(step_index),
    }


def delay_hours_of(vis, at, decision) -> float:
    if "delay_hours" in decision.args:
        return float(decision.args["delay_hours"])
    failed_at = datetime.fromisoformat(str(vis.failed_at))
    return max(0.0, (at - failed_at).total_seconds() / 3600.0)
