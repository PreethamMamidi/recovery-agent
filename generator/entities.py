"""
Customers and payments.

Everything here splits into VISIBLE (what a real merchant sees, and therefore
what the agent may read) and HIDDEN (mechanism that drives outcomes).
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import random

from .config import (FailureClass, ERROR_REASONS, METHODS, METHOD_WEIGHTS,
                     STRUCTURAL_LIMIT_REASONS, MANDATE_FRACTION)


# --------------------------------------------------------------- customers
@dataclass
class CustomerVisible:
    customer_id: str
    tenure_months: int
    past_payment_count: int
    past_failure_count: int
    preferred_channel: str
    opted_out: bool
    lifetime_value: int

    def as_row(self) -> dict:
        return asdict(self)


def make_customer(cid: str, rng: random.Random, latents) -> CustomerVisible:
    tenure = rng.choices([1, 3, 6, 12, 24, 36], [.18, .20, .22, .20, .13, .07])[0]

    # Past behaviour is a PROXY for true_intent_to_pay - correlated, not equal.
    # This is the trace the Day 5 model learns from.
    base_rate = 1.2 * latents.true_intent_to_pay + rng.uniform(-0.15, 0.15)
    payments = max(1, int(tenure * max(0.2, min(1.0, base_rate))))
    failures = int(payments * rng.uniform(0.02, 0.25))

    return CustomerVisible(
        customer_id=cid,
        tenure_months=tenure,
        past_payment_count=payments,
        past_failure_count=failures,
        preferred_channel=rng.choices(["sms", "whatsapp", "email"], [.35, .50, .15])[0],
        opted_out=rng.random() < 0.04,
        lifetime_value=int(payments * rng.uniform(400, 3500)),
    )


# --------------------------------------------------------------- payments
@dataclass
class PaymentVisible:
    payment_id: str
    customer_id: str
    amount: int
    method: str
    failed_at: str
    error_reason: str
    failure_class: str
    has_active_mandate: bool
    attempt_number: int
    invoice_due_date: str
    arm: str                      # control | treatment

    def as_row(self) -> dict:
        return asdict(self)


@dataclass
class PaymentHidden:
    """Payment-level mechanism. Simulator only."""
    payment_id: str
    downtime_ends_at: str | None      # technical_downtime
    lockout_ends_at: str | None       # temporary_lockout
    limit_resets_at: str | None       # limit_exceeded (daily caps only)
    is_structural_limit: bool         # limit_exceeded sub-rule: waiting never helps
    is_deliberate_abandon: bool       # session_expiry sub-rule: they chose to cancel

    def as_row(self) -> dict:
        return asdict(self)


def make_payment(pid: str, cust: CustomerVisible, fc: FailureClass,
                 rng: random.Random, period_start: datetime
                 ) -> tuple[PaymentVisible, PaymentHidden]:

    failed_at = period_start + timedelta(
        days=rng.randint(0, 27), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))

    reason = rng.choice(ERROR_REASONS[fc.class_id])

    # Mandate presence. mandate_failure is a SETUP failure, so by definition
    # there is no active mandate yet.
    if fc.class_id == "mandate_failure":
        has_mandate = False
    else:
        has_mandate = rng.random() < MANDATE_FRACTION

    amount = int(rng.choices(
        [rng.randint(99, 500), rng.randint(500, 2500),
         rng.randint(2500, 10000), rng.randint(10000, 60000)],
        [0.30, 0.40, 0.22, 0.08])[0])

    # ---- hidden mechanism -------------------------------------------------
    downtime_ends = lockout_ends = limit_resets = None
    structural = deliberate = False

    if fc.class_id == "technical_downtime":
        downtime_ends = failed_at + timedelta(hours=rng.uniform(0.5, 9.0))
    elif fc.class_id == "temporary_lockout":
        # Issuer-specific and undocumented. Wide spread on purpose - the agent
        # cannot know this, which is why backoff beats a fixed wait.
        lockout_ends = failed_at + timedelta(hours=rng.uniform(0.25, 26.0))
    elif fc.class_id == "limit_exceeded":
        structural = reason in STRUCTURAL_LIMIT_REASONS
        if not structural:
            nxt = (failed_at + timedelta(days=1)).replace(hour=0, minute=30, second=0)
            limit_resets = nxt
    elif fc.class_id == "session_expiry":
        deliberate = reason in ("payment_cancelled",)

    vis = PaymentVisible(
        payment_id=pid,
        customer_id=cust.customer_id,
        amount=amount,
        method=rng.choices(METHODS, METHOD_WEIGHTS)[0],
        failed_at=failed_at.isoformat(timespec="seconds"),
        error_reason=reason,
        failure_class=fc.class_id,
        has_active_mandate=has_mandate,
        attempt_number=1,
        invoice_due_date=(failed_at + timedelta(days=rng.randint(0, 7))).date().isoformat(),
        arm="treatment",   # assigned in generate.py
    )
    hid = PaymentHidden(
        payment_id=pid,
        downtime_ends_at=downtime_ends.isoformat(timespec="seconds") if downtime_ends else None,
        lockout_ends_at=lockout_ends.isoformat(timespec="seconds") if lockout_ends else None,
        limit_resets_at=limit_resets.isoformat(timespec="seconds") if limit_resets else None,
        is_structural_limit=structural,
        is_deliberate_abandon=deliberate,
    )
    return vis, hid
