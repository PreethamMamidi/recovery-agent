"""
Hidden response function. The agent must NEVER import this module.

    P(pay | customer, failure_class, action, timing, attempt_number)

The simulator reasons about hidden facts (downtime_ends_at, salary_day,
annoyance_threshold). It does not know or care what the policy *meant* by
an action — `schedule_for_payday` is just a debit at a timestamp.

Identity: respond(..., actions=[]) looks up ground_truth.csv. It does not
re-roll. That is the only way to match the generator's existing draws.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
import hashlib
import random

from generator.config import FailureClass, MEASUREMENT_WINDOW_DAYS, RANDOM_SEED
from generator.latents import CustomerLatents
from generator.natural_recovery import salary_lands_in_window

# Same-instrument debit never works on these until a customer-action
# replaces the instrument or re-authorises the mandate.
SAME_INSTRUMENT_DEAD = {"instrument_invalid", "issuer_decline", "mandate_failure"}
MESSAGE_ACTIONS = {
    "send_payment_link",
    "request_instrument_update",
    "request_mandate_reauth",
    "send_reminder",
}
TERMINAL_ACTIONS = {"escalate", "mark_uncollectible"}
DEBIT_ACTIONS = {"retry_debit", "schedule_for_payday"}


@dataclass
class Action:
    name: str
    at: datetime
    args: dict = field(default_factory=dict)


@dataclass
class Outcome:
    recovered: bool
    recovered_at: str | None
    source: str                  # natural | action | none
    contacts: int
    opted_out: bool
    annoyed: bool
    debit_attempts: int
    wasted_debits: int
    impossible_debits: int = 0
    messages_sms: int = 0
    messages_whatsapp: int = 0
    messages_email: int = 0
    opted_out_triggered: bool = False
    log: list = field(default_factory=list)

    @property
    def messages_total(self) -> int:
        return self.messages_sms + self.messages_whatsapp + self.messages_email


@dataclass
class _State:
    has_mandate: bool
    instrument_replaced: bool = False
    mandate_reauthed: bool = False
    contacts: int = 0
    opted_out: bool = False
    started_opted_out: bool = False
    annoyed: bool = False
    recovered: bool = False
    recovered_at: datetime | None = None
    source: str = "none"
    debit_attempts: int = 0
    wasted_debits: int = 0
    impossible_debits: int = 0
    stopped: bool = False
    messages_sms: int = 0
    messages_whatsapp: int = 0
    messages_email: int = 0
    log: list = field(default_factory=list)

    @property
    def opted_out_triggered(self) -> bool:
        return self.opted_out and not self.started_opted_out


def _rng_for(payment_id: str) -> random.Random:
    h = hashlib.md5(f"{RANDOM_SEED}:{payment_id}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _parse_bool(v) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def _parse_dt(v) -> datetime | None:
    if v is None or str(v).strip() == "":
        return None
    return datetime.fromisoformat(str(v))


def payment_hidden_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        payment_id=row["payment_id"],
        downtime_ends_at=row.get("downtime_ends_at") or None,
        lockout_ends_at=row.get("lockout_ends_at") or None,
        limit_resets_at=row.get("limit_resets_at") or None,
        is_structural_limit=_parse_bool(row.get("is_structural_limit", False)),
        is_deliberate_abandon=_parse_bool(row.get("is_deliberate_abandon", False)),
    )


def payment_visible_from_row(row: dict) -> SimpleNamespace:
    return SimpleNamespace(
        payment_id=row["payment_id"],
        customer_id=row["customer_id"],
        amount=int(row["amount"]),
        method=row["method"],
        failed_at=row["failed_at"],
        error_reason=row["error_reason"],
        failure_class=row["failure_class"],
        has_active_mandate=_parse_bool(row["has_active_mandate"]),
        attempt_number=int(row["attempt_number"]),
        invoice_due_date=row["invoice_due_date"],
        arm=row["arm"],
    )


def latents_from_row(row: dict) -> CustomerLatents:
    return CustomerLatents(
        customer_id=row["customer_id"],
        salary_day=int(row["salary_day"]),
        true_intent_to_pay=float(row["true_intent_to_pay"]),
        reattempt_propensity=float(row["reattempt_propensity"]),
        annoyance_threshold=int(row["annoyance_threshold"]),
        resp_sms=float(row["resp_sms"]),
        resp_whatsapp=float(row["resp_whatsapp"]),
        resp_email=float(row["resp_email"]),
        tech_savviness=float(row["tech_savviness"]),
    )


def _truth_recovered(truth: dict) -> tuple[bool, datetime | None]:
    recovered = _parse_bool(truth.get("would_have_recovered_naturally", False))
    when = _parse_dt(truth.get("natural_recovery_date"))
    if recovered and when is None:
        recovered = False
    return recovered, when


def _attention_decay(fc: FailureClass, failed_at: datetime, at: datetime) -> float:
    """Session / typo recoveries decay if we wait. Other classes: no decay."""
    if fc.class_id not in {"session_expiry", "customer_input_error"}:
        return 1.0
    hours = (at - failed_at).total_seconds() / 3600.0
    if hours <= 1:
        return 1.0
    if hours <= 24:
        return 0.55
    return 0.25


def _same_instrument_dead(fc: FailureClass, state: _State) -> bool:
    if fc.class_id not in SAME_INSTRUMENT_DEAD:
        return False
    if fc.class_id == "mandate_failure":
        return not state.mandate_reauthed
    return not state.instrument_replaced


def _blocker_gone(vis, hid, latents: CustomerLatents, fc: FailureClass,
                  at: datetime, state: _State, rng: random.Random) -> bool:
    """Has the underlying problem actually cleared at `at`?"""
    if _same_instrument_dead(fc, state):
        return False

    cid = fc.class_id
    failed_at = datetime.fromisoformat(vis.failed_at)

    if cid == "technical_downtime":
        ends = datetime.fromisoformat(hid.downtime_ends_at)
        return at >= ends

    if cid == "temporary_lockout":
        ends = datetime.fromisoformat(hid.lockout_ends_at)
        return at >= ends

    if cid == "limit_exceeded":
        if hid.is_structural_limit:
            # Cap is the cap; a smaller amount sometimes still fits.
            return rng.random() < 0.15
        resets = datetime.fromisoformat(hid.limit_resets_at)
        return at >= resets

    if cid == "insufficient_funds":
        return salary_lands_in_window(failed_at, latents.salary_day, at)

    # session_expiry / customer_input_error: nothing is broken.
    # instrument_invalid / issuer_decline / mandate_failure: veto already
    # applied above; if we got here the customer replaced / re-authed.
    return True


def _convert(state: _State, latents: CustomerLatents, rng: random.Random,
             extra: float = 1.0) -> bool:
    p = latents.true_intent_to_pay * extra
    if state.annoyed:
        p *= 0.4
    return rng.random() < max(0.0, min(1.0, p))


def _count_message(state: _State, channel: str) -> None:
    if channel == "whatsapp":
        state.messages_whatsapp += 1
    elif channel == "email":
        state.messages_email += 1
    else:
        state.messages_sms += 1


def _deliver_message(state: _State, latents: CustomerLatents, channel: str,
                     rng: random.Random) -> bool:
    """Increment contacts. Return True if the customer picked up."""
    state.contacts += 1
    _count_message(state, channel)
    if state.contacts > latents.annoyance_threshold:
        state.annoyed = True
        if rng.random() < 0.5:
            state.opted_out = True
            return False
    if state.opted_out:
        return False
    return rng.random() < latents.responsiveness(channel)


def _mark_recovered(state: _State, at: datetime, source: str) -> None:
    state.recovered = True
    state.recovered_at = at
    state.source = source
    state.stopped = True


def _try_debit(vis, hid, latents, fc, at, state, rng) -> bool:
    """Attempt a debit that the world can actually send (mandate already checked)."""
    state.debit_attempts += 1
    if fc.retry_viable == "never":
        state.wasted_debits += 1
    if state.opted_out:
        return False
    if not _blocker_gone(vis, hid, latents, fc, at, state, rng):
        return False
    return _convert(state, latents, rng)


def _try_customer_pay(vis, hid, latents, fc, at, state, rng) -> bool:
    """Customer-initiated pay (link / reminder). No mandate required."""
    if state.opted_out:
        return False
    if not _blocker_gone(vis, hid, latents, fc, at, state, rng):
        return False
    failed_at = datetime.fromisoformat(vis.failed_at)
    decay = _attention_decay(fc, failed_at, at)
    return _convert(state, latents, rng, extra=decay)


def _apply_action(action: Action, vis, hid, latents, fc, state, rng) -> None:
    name = action.name
    at = action.at
    args = action.args or {}

    if name == "_natural":
        if not state.recovered and not state.annoyed and not state.opted_out:
            _mark_recovered(state, at, "natural")
            state.log.append({"at": at.isoformat(), "action": "_natural", "ok": True})
        return

    if name in TERMINAL_ACTIONS:
        state.stopped = True
        state.log.append({"at": at.isoformat(), "action": name, "ok": True})
        return

    if name == "wait_for_downtime_recovery":
        state.log.append({"at": at.isoformat(), "action": name, "ok": True})
        return

    if name in DEBIT_ACTIONS:
        # No stored authorisation → the debit is never sent. Not an attempt,
        # not a failure, not a ₹2 cost. Distinct from wasted_debits.
        if not state.has_mandate:
            state.impossible_debits += 1
            state.log.append({
                "at": at.isoformat(),
                "action": name,
                "ok": False,
                "impossible": True,
            })
            return
        ok = _try_debit(vis, hid, latents, fc, at, state, rng)
        state.log.append({"at": at.isoformat(), "action": name, "ok": ok})
        if ok:
            _mark_recovered(state, at, "action")
        return

    if name in MESSAGE_ACTIONS:
        channel = args.get("channel", "sms")
        picked_up = _deliver_message(state, latents, channel, rng)
        ok = False

        if picked_up and name == "request_instrument_update":
            if rng.random() < latents.tech_savviness:
                state.instrument_replaced = True
                ok = _try_customer_pay(vis, hid, latents, fc, at, state, rng)

        elif picked_up and name == "request_mandate_reauth":
            if rng.random() < latents.tech_savviness:
                state.mandate_reauthed = True
                state.has_mandate = True
                ok = _try_customer_pay(vis, hid, latents, fc, at, state, rng)

        elif picked_up and name in {"send_payment_link", "send_reminder"}:
            ok = _try_customer_pay(vis, hid, latents, fc, at, state, rng)

        state.log.append({
            "at": at.isoformat(),
            "action": name,
            "picked_up": picked_up,
            "ok": ok,
        })
        if ok:
            _mark_recovered(state, at, "action")
        return

    raise ValueError(f"unknown action: {name}")


def respond(
    vis,
    hid,
    latents: CustomerLatents,
    fc: FailureClass,
    truth: dict,
    actions: list[Action] | None,
    opted_out: bool = False,
    rng: random.Random | None = None,
) -> Outcome:
    """
    Simulate a payment's 14-day window.

    Empty / None actions → identity: return ground_truth without re-rolling.
    """
    nat_ok, nat_at = _truth_recovered(truth)

    if not actions:
        return Outcome(
            recovered=nat_ok,
            recovered_at=nat_at.isoformat(timespec="seconds") if nat_at else None,
            source="natural" if nat_ok else "none",
            contacts=0,
            opted_out=opted_out,
            annoyed=False,
            debit_attempts=0,
            wasted_debits=0,
            impossible_debits=0,
        )

    failed_at = datetime.fromisoformat(vis.failed_at)
    window_end = failed_at + timedelta(days=MEASUREMENT_WINDOW_DAYS)
    rng = rng or _rng_for(vis.payment_id)

    state = _State(
        has_mandate=bool(vis.has_active_mandate),
        opted_out=opted_out,
        started_opted_out=opted_out,
    )

    events = [a for a in actions
              if failed_at <= a.at <= window_end]
    if nat_ok and nat_at is not None and failed_at <= nat_at <= window_end:
        events.append(Action("_natural", nat_at, {}))
    events.sort(key=lambda a: a.at)

    for action in events:
        if state.recovered:
            break
        if state.stopped and action.name != "_natural":
            continue
        _apply_action(action, vis, hid, latents, fc, state, rng)

    return Outcome(
        recovered=state.recovered,
        recovered_at=(state.recovered_at.isoformat(timespec="seconds")
                      if state.recovered_at else None),
        source=state.source,
        contacts=state.contacts,
        opted_out=state.opted_out,
        annoyed=state.annoyed,
        debit_attempts=state.debit_attempts,
        wasted_debits=state.wasted_debits,
        impossible_debits=state.impossible_debits,
        messages_sms=state.messages_sms,
        messages_whatsapp=state.messages_whatsapp,
        messages_email=state.messages_email,
        opted_out_triggered=state.opted_out_triggered,
        log=state.log,
    )
