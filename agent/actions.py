"""Bounded action schema. Nothing free-form leaves this module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


ALLOWED_ACTIONS = {
    "retry_debit": {"delay_hours": int},
    "schedule_for_payday": {"target_date": "date"},
    "wait_for_downtime_recovery": {"recheck_hours": int},
    "send_payment_link": {"channel": ["sms", "whatsapp", "email"]},
    "request_instrument_update": {"channel": ["sms", "whatsapp", "email"]},
    "request_mandate_reauth": {"channel": ["sms", "whatsapp", "email"]},
    "send_reminder": {"template_id": str, "channel": ["sms", "whatsapp", "email"]},
    "escalate": {"reason": str},
    "mark_uncollectible": {"reason": str},
}

AUTONOMOUS = {
    "retry_debit",
    "schedule_for_payday",
    "wait_for_downtime_recovery",
}
MESSAGE_ACTIONS = {
    "send_payment_link",
    "request_instrument_update",
    "request_mandate_reauth",
    "send_reminder",
}
TERMINAL = {"escalate", "mark_uncollectible"}
DEBIT_ACTIONS = {"retry_debit", "schedule_for_payday"}


@dataclass(frozen=True)
class Decision:
    action: str
    args: dict


def _is_date(v) -> bool:
    if isinstance(v, date) and not isinstance(v, datetime):
        return True
    if isinstance(v, str):
        try:
            date.fromisoformat(v)
            return True
        except ValueError:
            return False
    return False


def validate(payload: dict) -> Decision:
    if not isinstance(payload, dict):
        raise TypeError("decision must be a dict")
    extra = set(payload) - {"action", "args"}
    if extra:
        raise ValueError(f"unexpected keys: {sorted(extra)}")
    action = payload.get("action")
    args = payload.get("args", {})
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    if not isinstance(args, dict):
        raise TypeError("args must be a dict")
    schema = ALLOWED_ACTIONS[action]
    missing = set(schema) - set(args)
    extra_args = set(args) - set(schema)
    if missing:
        raise ValueError(f"{action} missing args {sorted(missing)}")
    if extra_args:
        raise ValueError(f"{action} unexpected args {sorted(extra_args)}")
    for key, spec in schema.items():
        val = args[key]
        if spec is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise TypeError(f"{action}.{key} must be int")
        elif spec is str:
            if not isinstance(val, str) or not val:
                raise TypeError(f"{action}.{key} must be a non-empty str")
        elif spec == "date":
            if not _is_date(val):
                raise TypeError(f"{action}.{key} must be an ISO date")
        elif isinstance(spec, list):
            if val not in spec:
                raise ValueError(f"{action}.{key} must be one of {spec}")
        else:
            raise RuntimeError(f"bad schema for {action}.{key}")
    return Decision(action=action, args=dict(args))


def decision(action: str, **args) -> Decision:
    return validate({"action": action, "args": args})
