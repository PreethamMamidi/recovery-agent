"""Recovery Agent HTTP API. Imports agent/ the same way the dashboard does."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from agent.diagnose import REASON_TO_CLASS
from agent.loop import build_schedule
from api.schemas import PaymentChain, WebhookResult
from api.security import verify_signature
from api.store import (
    event_seen,
    log_decisions,
    log_unknown_reason,
    record_event,
)
from api.visible import build_visible_from_webhook, notes_of

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
AUDIT = RESULTS / "audit.db"
ENV_PATH = ROOT / ".env"

app = FastAPI(title="Recovery Agent API")


def _load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


_load_dotenv()


def _webhook_secret() -> str:
    return os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


def _jsonable(args: dict) -> dict:
    out = {}
    for key, val in args.items():
        if hasattr(val, "isoformat"):
            out[key] = val.isoformat()
        else:
            out[key] = val
    return out


def _event_id(request: Request, event: dict, entity: dict) -> str:
    header = request.headers.get("X-Razorpay-Event-Id")
    if header:
        return header
    if event.get("id"):
        return str(event["id"])
    pay_id = entity.get("id") or "pay_unknown"
    created = event.get("created_at") or entity.get("created_at") or "0"
    return f"{pay_id}:{created}"


def fetch_chain(payment_id: str) -> list[dict]:
    """Read-only, per-request. Same columns the dashboard timeline uses."""
    if not AUDIT.exists():
        return []
    conn = sqlite3.connect(f"file:{AUDIT}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """SELECT timestamp, failure_class, chosen_action, action_args,
                      gate_result, gate_reason, executed, outcome, flagged_for_review
               FROM decisions WHERE payment_id = ? ORDER BY id""",
            (payment_id,),
        )
        rows = []
        for raw in cur.fetchall():
            row = dict(raw)
            try:
                row["action_args"] = json.loads(row["action_args"] or "{}")
            except json.JSONDecodeError:
                row["action_args"] = {}
            rows.append(row)
        return rows
    finally:
        conn.close()


def _steps_to_actions(steps) -> list[dict]:
    out = []
    for step in steps:
        decision = step.executed if step.executed is not None else step.proposed
        args = _jsonable(dict(decision.args))
        out.append({
            "action": decision.action,
            "args": args,
            "gate": step.gate_result,
            "reason": step.gate_reason,
            "args_json": json.dumps(args),
        })
    return out


@app.get("/metrics")
def metrics():
    path = RESULTS / "agent.json"
    if not path.exists():
        raise HTTPException(404, "agent.json not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/payments/{payment_id}", response_model=PaymentChain)
def get_chain(payment_id: str):
    rows = fetch_chain(payment_id)
    if not rows:
        raise HTTPException(404, "no decision chain for this payment")
    return {"payment_id": payment_id, "decisions": rows}


@app.post("/webhook", response_model=WebhookResult)
async def handle_failure(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Razorpay-Signature")
    if not sig or not verify_signature(raw, sig, _webhook_secret()):
        raise HTTPException(401, "invalid signature")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid json")

    try:
        entity = event["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        raise HTTPException(400, "not a payment.failed payload")

    event_id = _event_id(request, event, entity)

    if event_seen(event_id):
        return {"status": "duplicate", "event_id": event_id}

    reason = entity.get("error_reason")
    if reason is None or reason not in REASON_TO_CLASS:
        record_event(event_id)
        log_unknown_reason(event_id, reason if reason is None else str(reason))
        return {
            "status": "accepted",
            "event_id": event_id,
            "internal_payment_id": notes_of(entity).get("internal_payment_id"),
            "failure_class": "unknown",
            "actions": [],
        }

    visible, customer = build_visible_from_webhook(entity, event)
    fclass, steps = build_schedule(visible, customer)
    actions = _steps_to_actions(steps)

    record_event(event_id)
    log_decisions(event_id, visible.payment_id, fclass, actions)

    return {
        "status": "accepted",
        "event_id": event_id,
        "internal_payment_id": notes_of(entity).get("internal_payment_id"),
        "failure_class": fclass,
        "actions": [
            {"action": a["action"], "args": a["args"],
             "gate": a["gate"], "reason": a["reason"]}
            for a in actions
        ],
    }
