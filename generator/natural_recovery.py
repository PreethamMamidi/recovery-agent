"""
Natural recovery: what happens when NOBODY does anything.

This is the ground truth behind base_recovery_prob, computed as:

        P(recovers unprompted) = P(problem resolves) x P(customer re-attempts)

Both must happen. The blocker has to go away AND the customer has to bother
trying again. Two independent things, deliberately kept separate:

  P(resolves)     - a property of the FAILURE CLASS   (a rail heals, a card doesn't)
  P(re-attempts)  - a property of the PERSON          (do they bother?)

Collapsing these into one flat number per class hides the structure the Day 5
propensity model needs to learn.

Note the two classes with p_resolves = 1.00 (session_expiry, customer_input_error):
nothing is actually broken there, so their whole base rate is driven by whether
the customer comes back. That falls out of the formula rather than being asserted.

IMPORTANT: this file is HIDDEN GROUND TRUTH. The agent never imports it.
"""

from datetime import datetime, timedelta
import random

from .config import MEASUREMENT_WINDOW_DAYS


def p_resolves_in_window(payment_vis, payment_hid, fc, window_end: datetime) -> float:
    """
    Does the underlying blocker clear inside the measurement window?

    Mostly mechanical - we check the actual hidden timestamps rather than
    rolling against a class constant. That is what makes the agent's guesses
    real bets: it cannot see these times.
    """
    cid = fc.class_id

    if cid == "technical_downtime":
        ends = datetime.fromisoformat(payment_hid.downtime_ends_at)
        return 1.0 if ends <= window_end else 0.0

    if cid == "temporary_lockout":
        ends = datetime.fromisoformat(payment_hid.lockout_ends_at)
        return 1.0 if ends <= window_end else 0.0

    if cid == "limit_exceeded":
        if payment_hid.is_structural_limit:
            return 0.15          # cap is the cap; only a smaller amount helps
        resets = datetime.fromisoformat(payment_hid.limit_resets_at)
        return 1.0 if resets <= window_end else 0.0

    # insufficient_funds is handled by the caller (needs salary_day)
    return fc.p_resolves


def salary_lands_in_window(failed_at: datetime, salary_day: int,
                           window_end: datetime) -> bool:
    """Next salary date at or after failure, within the window."""
    d = failed_at
    for _ in range(3):
        try:
            candidate = d.replace(day=salary_day, hour=10, minute=0, second=0)
        except ValueError:
            return False
        if candidate >= failed_at:
            return candidate <= window_end
        # roll to next month
        d = (d.replace(day=28) + timedelta(days=7)).replace(day=1)
    return False


def p_reattempts(latents, fc, payment_hid) -> float:
    """
    Will the customer bother trying again without being asked?

    Driven by the person, then nudged by how much friction the class imposes.
    """
    base = 0.35 * latents.reattempt_propensity + 0.45 * latents.true_intent_to_pay

    friction = {
        "technical_downtime":   1.00,   # they were mid-purchase, low friction
        "temporary_lockout":    0.95,
        "limit_exceeded":       0.95,
        "insufficient_funds":   0.85,
        "session_expiry":       0.55,   # attention has decayed
        "customer_input_error": 0.45,   # frustrated, gave up
        "issuer_decline":       0.80,   # must find another card
        "instrument_invalid":   0.70 * (0.5 + latents.tech_savviness / 2),
        "mandate_failure":      0.75 * (0.5 + latents.tech_savviness / 2),
    }[fc.class_id]

    if payment_hid.is_deliberate_abandon:
        friction *= 0.35      # they chose to cancel; they are not coming back soon

    return max(0.0, min(1.0, base * friction))


def natural_recovery(payment_vis, payment_hid, latents, fc,
                     rng: random.Random) -> tuple[bool, str | None, float]:
    """
    Returns (recovered, recovery_date_iso, p_used).

    This is the CONTROL-ARM outcome and the counterfactual for every treatment
    payment. The generator writes it to ground_truth.csv once. The simulator,
    when called with action=None, returns exactly this.
    """
    failed_at = datetime.fromisoformat(payment_vis.failed_at)
    window_end = failed_at + timedelta(days=MEASUREMENT_WINDOW_DAYS)

    if fc.class_id == "insufficient_funds":
        p_res = 1.0 if salary_lands_in_window(failed_at, latents.salary_day, window_end) else 0.05
    else:
        p_res = p_resolves_in_window(payment_vis, payment_hid, fc, window_end)

    p_ra = p_reattempts(latents, fc, payment_hid)
    p = p_res * p_ra

    if rng.random() >= p:
        return False, None, round(p, 4)

    # Recovery lands somewhere between resolution and the window edge.
    lag_days = rng.uniform(0.5, MEASUREMENT_WINDOW_DAYS * 0.8)
    return True, (failed_at + timedelta(days=lag_days)).isoformat(timespec="seconds"), round(p, 4)
