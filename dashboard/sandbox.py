"""Single-payment sandbox. Dashboard-only — does not run the batch.

The agent path is diagnose → policy → gate (build_schedule). The simulator
and latents are imported here, never from agent/.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from types import SimpleNamespace

from agent.actions import DEBIT_ACTIONS, MESSAGE_ACTIONS
from agent.diagnose import diagnose
from agent.guardrails import VALUE_ESCALATE_INR
from agent.loop import build_schedule
from generator.config import (
    ERROR_REASONS,
    STRUCTURAL_LIMIT_REASONS,
    load_failure_classes,
)
from generator.latents import make_latents
from generator.natural_recovery import natural_recovery
from simulator.response import Action, latents_from_row, payment_hidden_from_row, respond

FAILED_AT = datetime(2026, 8, 20, 10, 0, 0)

CLASS_IDS = list(load_failure_classes())

PRESETS = {
    "Expired card": {
        "failure_class": "instrument_invalid",
        "error_reason": "card_expired",
        "amount": 2400,
        "has_active_mandate": True,
        "tenure_months": 12,
        "past_payment_count": 8,
        "opted_out": False,
    },
    "Bank downtime": {
        "failure_class": "technical_downtime",
        "error_reason": "bank_not_available",
        "amount": 1800,
        "has_active_mandate": True,
        "tenure_months": 6,
        "past_payment_count": 4,
        "opted_out": False,
    },
    "No mandate": {
        "failure_class": "insufficient_funds",
        "error_reason": "insufficient_funds",
        "amount": 3500,
        "has_active_mandate": False,
        "tenure_months": 18,
        "past_payment_count": 10,
        "opted_out": False,
    },
}

PRESET_NOTES = {
    "Expired card": "Retry is futile — the agent asks for a new instrument.",
    "Bank downtime": "Mandate on file: wait, send nothing, then retry.",
    "No mandate": "Payday debit is blocked; fallback is a payment link.",
}


@dataclass(frozen=True)
class Invented:
    failure_class: str
    error_reason: str
    amount: int
    has_active_mandate: bool
    tenure_months: int
    past_payment_count: int
    opted_out: bool


def _rng(payload: str) -> random.Random:
    h = hashlib.md5(payload.encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _hidden_for(fc, reason: str, failed_at: datetime, rng: random.Random,
                payment_id: str):
    downtime_ends = lockout_ends = limit_resets = None
    structural = deliberate = False
    if fc.class_id == "technical_downtime":
        downtime_ends = failed_at + timedelta(hours=rng.uniform(0.5, 9.0))
    elif fc.class_id == "temporary_lockout":
        lockout_ends = failed_at + timedelta(hours=rng.uniform(0.25, 26.0))
    elif fc.class_id == "limit_exceeded":
        structural = reason in STRUCTURAL_LIMIT_REASONS
        if not structural:
            limit_resets = (failed_at + timedelta(days=1)).replace(
                hour=0, minute=30, second=0, microsecond=0)
    elif fc.class_id == "session_expiry":
        deliberate = reason == "payment_cancelled"
    return SimpleNamespace(
        payment_id=payment_id,
        downtime_ends_at=downtime_ends.isoformat(timespec="seconds") if downtime_ends else None,
        lockout_ends_at=lockout_ends.isoformat(timespec="seconds") if lockout_ends else None,
        limit_resets_at=limit_resets.isoformat(timespec="seconds") if limit_resets else None,
        is_structural_limit=structural,
        is_deliberate_abandon=deliberate,
    )


def diagnosis_table(error_reason: str, diagnosed: str) -> list[dict]:
    fc = load_failure_classes()[diagnosed]
    return [{
        "error_reason": error_reason,
        "diagnosed": diagnosed,
        "bucket": fc.bucket,
        "retry_viable": fc.retry_viable,
        "optimal_delay": fc.optimal_delay,
        "ask_delay": fc.ask_delay,
    }]


def plan_table(steps) -> list[dict]:
    rows = []
    for step in steps:
        proposed = step.proposed.action
        executed = step.executed.action if step.executed is not None else "—"
        rows.append({
            "at": step.at.isoformat(timespec="seconds"),
            "proposed": proposed,
            "executed": executed,
            "gate": step.gate_result,
            "reason": step.gate_reason,
        })
    return rows


def plan_table_from_audit(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "at": r["timestamp"],
            "proposed": r["chosen_action"],
            "executed": r["chosen_action"] if r["executed"] else "—",
            "gate": r["gate_result"],
            "reason": r["gate_reason"],
        })
    return out


_SKIP_PRIMARY = {
    "—",
    "mark_uncollectible",
    "escalate",
    "pre_debit_notification",
}


def _parse_at(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return None


def fmt_day(raw) -> str:
    dt = _parse_at(raw)
    if dt is None:
        return "—"
    return f"{dt.day} {dt.strftime('%b')}"


def _primary_row(plan: list[dict]) -> dict | None:
    for row in plan:
        if row.get("executed") not in _SKIP_PRIMARY:
            return row
    for row in plan:
        if row.get("proposed") not in _SKIP_PRIMARY:
            return row
    return plan[0] if plan else None


def _decision_action(row: dict | None) -> str:
    if row is None:
        return "—"
    executed = row.get("executed")
    if executed not in _SKIP_PRIMARY:
        return executed
    return row.get("proposed") or "—"


def decision_why(diagnosed: str, action: str, gate: str = "",
                 reason: str = "") -> str:
    if gate == "rejected" and reason:
        return f"blocked: {reason}"
    if diagnosed == "insufficient_funds" and action == "retry_debit":
        return "payday-anchored retry; agent cannot see salary_day"
    if diagnosed == "insufficient_funds" and action == "send_payment_link":
        return "no mandate — cannot debit; asked instead"
    if diagnosed == "instrument_invalid":
        return "instrument is dead; a retry cannot fix it"
    if diagnosed == "technical_downtime" and action == "wait_for_downtime_recovery":
        return "rail self-heals; wait, then retry"
    if diagnosed == "technical_downtime" and action == "retry_debit":
        return "outage should have cleared; retry the mandate"
    if diagnosed == "mandate_failure":
        return "setup failure, not a debit failure; ask to re-authorize"
    if diagnosed == "issuer_decline":
        return "issuer refused; same card, same answer"
    if diagnosed == "customer_input_error":
        return "instrument is fine; the customer needs to re-enter details"
    if diagnosed == "session_expiry":
        return "nothing is broken; retry before the window closes"
    if diagnosed == "limit_exceeded":
        return "daily cap resets on a clock; wait for 00:30"
    if diagnosed == "temporary_lockout":
        return "lockout clears with time; attempts do not"
    if action and action != "—":
        return action.replace("_", " ")
    return "—"


def guardrail_lines(
    *,
    has_mandate: bool,
    attempt: int,
    max_attempts: int,
    opted_out: bool,
    amount: int,
    plan: list[dict],
) -> list[dict]:
    """Standing checks. Passed ones stay visible — not only the failures."""
    proposed_debit = any(r.get("proposed") in DEBIT_ACTIONS for r in plan)
    executed_debit = any(r.get("executed") in DEBIT_ACTIONS for r in plan)
    if has_mandate:
        mandate = {"ok": True, "text": "mandate present"}
    elif executed_debit:
        mandate = {"ok": False, "text": "no mandate — debit executed"}
    elif proposed_debit:
        mandate = {"ok": True, "text": "no mandate — debit not executed"}
    else:
        mandate = {"ok": True, "text": "no mandate — asked instead of debiting"}

    attempt_ok = attempt < max_attempts
    value_ok = amount < VALUE_ESCALATE_INR
    if value_ok:
        value = {"ok": True, "text": f"under ₹{VALUE_ESCALATE_INR:,} escalate threshold"}
    else:
        value = {"ok": True, "text": f"flagged for review (≥ ₹{VALUE_ESCALATE_INR:,})"}

    return [
        mandate,
        {"ok": attempt_ok, "text": f"attempt {attempt} of {max_attempts}"},
        {"ok": not opted_out, "text": "not opted out" if not opted_out else "opted out"},
        value,
    ]


def outcome_lines(outcome: dict) -> dict:
    amount = int(outcome.get("amount") or 0)
    if outcome.get("recovered"):
        agent = f"recovered ₹{amount:,} on {fmt_day(outcome.get('recovered_at'))}"
    else:
        agent = "not recovered"
    if outcome.get("natural"):
        natural = f"recovered on {fmt_day(outcome.get('natural_at'))}"
    else:
        natural = "not recovered"
    return {"agent": agent, "no_intervention": natural}


def build_card(
    *,
    payment_id: str,
    amount: int,
    error_reason: str,
    diagnosed: str,
    has_mandate: bool,
    attempt: int,
    opted_out: bool,
    plan: list[dict],
    outcome: dict,
) -> dict:
    fc = load_failure_classes()[diagnosed]
    row = _primary_row(plan)
    action = _decision_action(row)
    target = fmt_day(row["at"]) if row else "—"
    gate = row.get("gate", "") if row else ""
    reason = row.get("reason", "") if row else ""
    return {
        "headline": (
            f"{payment_id} · ₹{amount:,} · {diagnosed} · "
            f"mandate: {'yes' if has_mandate else 'no'}"
        ),
        "diagnosis": {
            "error_reason": error_reason or "—",
            "failure class": diagnosed,
            "bucket": fc.bucket,
            "retry viable": fc.retry_viable,
        },
        "decision": {
            "action": action,
            "target": target,
            "why": decision_why(diagnosed, action, gate, reason),
        },
        "guardrails": guardrail_lines(
            has_mandate=has_mandate,
            attempt=attempt,
            max_attempts=fc.max_attempts,
            opted_out=opted_out,
            amount=amount,
            plan=plan,
        ),
        "outcome": outcome_lines(outcome),
    }


def _to_sim_action(step) -> Action:
    d = step.executed
    return Action(name=d.action, at=step.at, args=dict(d.args))


def run_invented(spec: dict | Invented) -> dict:
    """diagnose → policy → gate → simulator on one invented payment."""
    if isinstance(spec, dict):
        spec = Invented(**{k: spec[k] for k in Invented.__dataclass_fields__})
    diagnosed = diagnose(spec.error_reason)
    fc = load_failure_classes()[diagnosed]
    payload = (
        f"{spec.error_reason}|{spec.amount}|{spec.has_active_mandate}|"
        f"{spec.tenure_months}|{spec.past_payment_count}|{spec.opted_out}"
    )
    rng = _rng(payload)
    pid = "PAY_SANDBOX"
    cid = "CUST_SANDBOX"
    vis = SimpleNamespace(
        payment_id=pid,
        customer_id=cid,
        amount=int(spec.amount),
        method="card",
        failed_at=FAILED_AT.isoformat(timespec="seconds"),
        error_reason=spec.error_reason,
        failure_class=diagnosed,
        has_active_mandate=bool(spec.has_active_mandate),
        attempt_number=1,
        invoice_due_date=(FAILED_AT + timedelta(days=7)).date().isoformat(),
        arm="treatment",
    )
    hid = _hidden_for(fc, spec.error_reason, FAILED_AT, rng, pid)
    lat = make_latents(cid, rng)
    cust = {
        "preferred_channel": "sms",
        "opted_out": bool(spec.opted_out),
        "tenure_months": int(spec.tenure_months),
        "past_payment_count": int(spec.past_payment_count),
        "past_failure_count": 1,
        "lifetime_value": max(500, int(spec.past_payment_count) * 400),
    }
    recovered, when, p_used = natural_recovery(vis, hid, lat, fc, rng)
    truth = {
        "would_have_recovered_naturally": recovered,
        "natural_recovery_date": when,
        "p_natural_used": p_used,
    }
    _, steps = build_schedule(vis, cust)
    sim_actions = [_to_sim_action(s) for s in steps if s.executed is not None]
    out = respond(vis, hid, lat, fc, truth, sim_actions, opted_out=spec.opted_out)
    hidden = asdict(lat)
    hidden.update({
        "downtime_ends_at": hid.downtime_ends_at,
        "lockout_ends_at": hid.lockout_ends_at,
        "limit_resets_at": hid.limit_resets_at,
        "is_structural_limit": hid.is_structural_limit,
        "is_deliberate_abandon": hid.is_deliberate_abandon,
    })
    diagnosis = diagnosis_table(spec.error_reason, diagnosed)
    plan = plan_table(steps)
    outcome = {
        "recovered": bool(out.recovered),
        "recovered_at": out.recovered_at,
        "source": out.source,
        "amount": int(vis.amount),
        "natural": bool(recovered),
        "natural_at": when,
        "p_natural": p_used,
    }
    return {
        "mode": "invented",
        "diagnosed": diagnosed,
        "diagnosis": diagnosis,
        "plan": plan,
        "outcome": outcome,
        "hidden": hidden,
        "card": build_card(
            payment_id=pid,
            amount=int(vis.amount),
            error_reason=spec.error_reason,
            diagnosed=diagnosed,
            has_mandate=bool(spec.has_active_mandate),
            attempt=int(vis.attempt_number),
            opted_out=bool(spec.opted_out),
            plan=plan,
            outcome=outcome,
        ),
    }


@lru_cache(maxsize=1)
def _world():
    from eval.metrics import load_world
    return load_world()


def run_batch(payment_id: str, header: dict, audit_rows: list[dict]) -> dict:
    """Same card as invented, using the precomputed batch — no new schedule."""
    world = _world()
    vis_row = next((r for r in world["pay_vis"] if r["payment_id"] == payment_id), None)
    error_reason = header.get("error_reason", "")
    diagnosed = diagnose(error_reason) if error_reason else header.get("failure_class", "")
    diagnosis = diagnosis_table(error_reason, diagnosed) if error_reason else []
    truth = world["truth"].get(payment_id, {})
    hidden = {}
    opted_out = False
    has_mandate = bool(header.get("has_active_mandate"))
    attempt = 1
    amount = int(header.get("amount") or 0)
    if vis_row is not None:
        hid = payment_hidden_from_row(world["pay_hid"][payment_id])
        lat = latents_from_row(world["latents"][vis_row["customer_id"]])
        hidden = asdict(lat)
        hidden.update({
            "downtime_ends_at": hid.downtime_ends_at,
            "lockout_ends_at": hid.lockout_ends_at,
            "limit_resets_at": hid.limit_resets_at,
            "is_structural_limit": hid.is_structural_limit,
            "is_deliberate_abandon": hid.is_deliberate_abandon,
        })
        cust = world["customers"].get(vis_row["customer_id"], {})
        opted_out = str(cust.get("opted_out", "")).strip().lower() in {"true", "1", "yes"}
        has_mandate = str(vis_row.get("has_active_mandate", "")).strip().lower() in {
            "true", "1", "yes",
        }
        attempt = int(vis_row.get("attempt_number") or 1)
        amount = int(float(vis_row.get("amount") or amount))
        error_reason = vis_row.get("error_reason") or error_reason
        diagnosed = diagnose(error_reason) if error_reason else diagnosed
    plan = plan_table_from_audit(audit_rows)
    outcome = {
        "recovered": bool(header.get("recovered")),
        "recovered_at": header.get("recovered_at"),
        "source": header.get("source") or "none",
        "amount": amount,
        "natural": str(truth.get("would_have_recovered_naturally", "")).lower()
        in {"true", "1", "yes"},
        "natural_at": truth.get("natural_recovery_date") or None,
        "p_natural": truth.get("p_natural_used"),
    }
    card = {}
    if diagnosed:
        card = build_card(
            payment_id=payment_id,
            amount=amount,
            error_reason=error_reason,
            diagnosed=diagnosed,
            has_mandate=has_mandate,
            attempt=attempt,
            opted_out=opted_out,
            plan=plan,
            outcome=outcome,
        )
    return {
        "mode": "batch",
        "diagnosed": diagnosed,
        "diagnosis": diagnosis,
        "plan": plan,
        "outcome": outcome,
        "hidden": hidden,
        "card": card,
    }


def message_count(plan: list[dict]) -> int:
    return sum(1 for r in plan if r["executed"] in MESSAGE_ACTIONS)
