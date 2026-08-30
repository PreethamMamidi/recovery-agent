"""
Customers and payments.

Everything here splits into VISIBLE (what a real merchant sees, and therefore
what the agent may read) and HIDDEN (mechanism that drives outcomes).
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import random

from .config import (FailureClass, ERROR_REASONS, reason_weights_for,
                     METHODS, METHOD_WEIGHTS,
                     STRUCTURAL_LIMIT_REASONS)
from .presence import assign_mandate


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


# Razorpay reports an 8–12pp success drop at 7–10 PM from bank load.
# Sampling these hours is a robustness flag (--peak-hours), never the
# canonical path: uniform 0–23 is what produced the published data/ batch.
PEAK_HOURS = (19, 20, 21, 22)  # 19:00–22:00 inclusive
# Share of failures drawn in PEAK_HOURS vs ~4/24 ≈ 17% under uniform.
PEAK_MASS = 0.40
# technical_downtime concentrates further: the published drop is load-related
# rail failure, not a uniform mix shift. Separate from PEAK_MASS so we can
# attribute "more evening" vs "more downtime-in-evening".
DOWNTIME_PEAK_MASS = 0.70


def _draw_hour(rng: random.Random, peak_hours: bool,
               concentrate_downtime: bool) -> int:
    """Uniform 0–23 unless --peak-hours. Same RNG call when peak_hours is off,
    so a no-flag generate still matches data/."""
    if not peak_hours:
        return rng.randint(0, 23)
    mass = DOWNTIME_PEAK_MASS if concentrate_downtime else PEAK_MASS
    if rng.random() < mass:
        return rng.choice(PEAK_HOURS)
    return rng.randint(0, 23)


def make_payment(pid: str, cust: CustomerVisible, fc: FailureClass,
                 rng: random.Random, period_start: datetime,
                 peak_hours: bool = False,
                 ) -> tuple[PaymentVisible, PaymentHidden]:

    # Day first, then hour, then minute — same order as before peak_hours
    # existed, so default False does not reshuffle the canonical RNG stream.
    failed_at = period_start + timedelta(
        days=rng.randint(0, 27),
        hours=_draw_hour(rng, peak_hours, fc.class_id == "technical_downtime"),
        minutes=rng.randint(0, 59))

    reason = rng.choices(ERROR_REASONS[fc.class_id],
                         weights=reason_weights_for(fc.class_id))[0]

    amount = int(rng.choices(
        [rng.randint(99, 500), rng.randint(500, 2500),
         rng.randint(2500, 10000), rng.randint(10000, 60000)],
        [0.30, 0.40, 0.22, 0.08])[0])

    # Presence is a property of the reason code, not the class.
    # Reason and amount are both drawn first (AFA needs the amount).
    has_mandate = assign_mandate(reason, amount, rng)

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
