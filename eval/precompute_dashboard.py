"""Write results/*.json and results/audit.db. The dashboard reads these.

    python -m eval.precompute_dashboard

Runs the batch once. Streamlit never does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent.actions import MESSAGE_ACTIONS
from agent.messaging import generate, load_demo_cases
from agent.ml_options import MlOptions
from audit.log import (
    DEFAULT_DB,
    count_close_reasons,
    count_gate_reasons,
    log_decision,
)
from baselines.aggressive_dunning import schedule as schedule_c
from baselines.fixed_retry import schedule as schedule_a
from baselines.retry_plus_sms import schedule as schedule_b
from config.costs import DEBIT_COST, MESSAGE_COST
from eval.metrics import control_totals, identity_mismatches, load_world, run_schedule
from eval.run_agent import run_agent
from eval.snapshot import policy_snapshot
from simulator.response import payment_visible_from_row

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
AUDIT_COPY = RESULTS / "audit.db"


def _cost_breakdown(p) -> dict:
    debits = p.debit_attempts * DEBIT_COST
    sms = p.messages_sms * MESSAGE_COST["sms"]
    wa = p.messages_whatsapp * MESSAGE_COST["whatsapp"]
    email = p.messages_email * MESSAGE_COST["email"]
    opt = float(p.cost) - debits - sms - wa - email
    return {
        "debits": round(debits, 2),
        "sms": round(sms, 2),
        "whatsapp": round(wa, 2),
        "email": round(email, 2),
        "opt_out": round(opt, 2),
        "total": round(p.cost, 2),
    }


def _from_conn(conn) -> dict:
    gates = count_gate_reasons(conn)
    if "offer_beyond_policy" not in gates:
        gates["offer_beyond_policy"] = 0
    closes = count_close_reasons(conn)
    for key in ("schedule_exhausted", "attempt_budget", "opted_out", "no_viable_action"):
        closes.setdefault(key, 0)
    return {"close_reasons": closes, "gate_rejections": gates}


def pack(p, *, extra: dict | None = None, conn=None) -> dict:
    payload = policy_snapshot(p, extra=extra)
    payload["channel_mix"] = {
        "sms": p.messages_sms,
        "whatsapp": p.messages_whatsapp,
        "email": p.messages_email,
    }
    payload["cost_breakdown"] = _cost_breakdown(p)
    payload["messages_by_class"] = {
        cid: int(b.get("messages", 0)) for cid, b in p.by_class.items()
    }
    payload["close_reasons"] = {
        "schedule_exhausted": 0,
        "attempt_budget": 0,
        "opted_out": 0,
        "no_viable_action": 0,
    }
    payload["gate_rejections"] = {"offer_beyond_policy": 0}
    if conn is not None:
        payload.update(_from_conn(conn))
    return payload


def _downtime_mandate_messages(world, schedule_fn) -> int:
    n = 0
    for row in world["pay_vis"]:
        vis = payment_visible_from_row(row)
        if vis.arm != "treatment":
            continue
        if vis.failure_class != "technical_downtime" or not vis.has_active_mandate:
            continue
        for act in schedule_fn(vis):
            if act.name in MESSAGE_ACTIONS:
                n += 1
    return n


def _write(name: str, payload: dict) -> None:
    path = RESULTS / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}")


def _seed_staged(conn) -> dict[str, dict]:
    """PAY_HV / PAY_LV live in the same audit db as the batch."""
    headers = {}
    for case in load_demo_cases()["bounded_offers"]:
        if case.get("from_batch"):
            continue
        from types import SimpleNamespace
        vis = SimpleNamespace(
            payment_id=case["id"],
            amount=case["amount"],
            failure_class=case["failure_class"],
        )
        cust = {"lifetime_value": case["lifetime_value"]}
        msg = generate(vis, cust, name=case.get("name", "there"))
        args = {
            "channel": "sms",
            "policy_id": msg.policy_id,
            "template_id": msg.template_id,
        }
        log_decision(
            conn,
            payment_id=case["id"],
            attempt_number=1,
            timestamp="2026-08-10T10:02:00",
            failure_class=case["failure_class"],
            chosen_action="send_payment_link",
            action_args=args,
            gate_result="allowed",
            gate_reason="ok",
            executed=True,
        )
        headers[case["id"]] = {
            "amount": int(case["amount"]),
            "method": "",
            "failed_at": "2026-08-10T10:00:00",
            "error_reason": "insufficient_funds",
            "failure_class": case["failure_class"],
            "has_active_mandate": False,
            "customer_id": "",
            "preferred_channel": "sms",
            "recovered": False,
            "recovered_at": None,
            "source": "staged",
            "staged": True,
            "body": msg.body,
        }
    conn.commit()
    return headers


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    world = load_world()
    ident = identity_mismatches(world)
    if ident:
        raise SystemExit(f"identity failed on {ident} rows")

    ctl = control_totals(world)
    a = run_schedule(world, schedule_a, "A retry@24h")
    b = run_schedule(world, schedule_b, "B SMS+3 retries")
    c = run_schedule(world, schedule_c, "C aggressive")
    b_dt = _downtime_mandate_messages(world, schedule_b)

    _write("control.json", pack(ctl, extra={"control_n": ctl.n}))
    _write("baseline_a.json", pack(a))
    _write("baseline_b.json", pack(b, extra={"downtime_mandate_messages": b_dt}))
    _write("baseline_c.json", pack(c))

    print("  agent (channel)…")
    ch, conn = run_agent(world, ml=MlOptions(use_model=True, app="channel"))
    _write("agent_channel.json", pack(ch, conn=conn, extra={"ml_app": "channel"}))
    conn.close()

    print("  agent (quartile)…")
    q, conn = run_agent(
        world,
        ml=MlOptions(use_model=True, app="second_ask", p2_percentile=25),
    )
    _write("agent_quartile.json", pack(q, conn=conn, extra={
        "ml_app": "second_ask",
        "p2_percentile": 25,
    }))
    conn.close()

    print("  agent (rules)…")
    agent, conn = run_agent(world)
    extra = {
        "rejections": int(getattr(agent, "rejections", 0)),
        "downtime_messages": int(getattr(agent, "downtime_messages", 0)),
        "downtime_mandate_messages": int(getattr(agent, "downtime_messages", 0)),
        "flagged_for_review": int(getattr(agent, "flagged_for_review", 0)),
        "trai": dict(getattr(agent, "trai", {}) or {}),
        "pre_debit_notifications": int(getattr(agent, "pre_debit_notifications", 0)),
        "pre_debit_window_violations": int(getattr(agent, "pre_debit_window_violations", 0)),
    }
    _write("agent.json", pack(agent, extra=extra, conn=conn))

    staged = _seed_staged(conn)
    headers = dict(getattr(agent, "payment_headers", {}))
    headers.update(staged)
    _write("payments.json", headers)

    conn.close()
    shutil.copy2(DEFAULT_DB, AUDIT_COPY)
    print(f"  wrote {AUDIT_COPY.relative_to(ROOT)}")
    print(f"  agent rec {agent.recovery_rate:.1%}  msgs {agent.messages}  "
          f"wasted {agent.wasted_debits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
