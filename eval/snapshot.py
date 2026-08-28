"""Write control / A / B / C / agent metrics to JSON for before/after diffs.

    python -m eval.snapshot results_before.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from baselines.aggressive_dunning import schedule as schedule_c
from baselines.fixed_retry import schedule as schedule_a
from baselines.retry_plus_sms import schedule as schedule_b
from eval.metrics import (
    PolicyTotals,
    control_totals,
    identity_mismatches,
    load_world,
    rate,
    run_schedule,
)
from eval.run_agent import run_agent
from generator.config import RANDOM_SEED


def _class_block(bucket: dict) -> dict:
    n = bucket.get("n", 0)
    return {
        "n": n,
        "recovered": bucket.get("recovered", 0),
        "recovery_rate": round(rate(bucket.get("recovered", 0), n), 6),
        "natural": bucket.get("natural", 0),
        "natural_rate": round(rate(bucket.get("natural", 0), n), 6),
        "wasted_debits": bucket.get("wasted", 0),
        "impossible_debits": bucket.get("impossible", 0),
        "messages": bucket.get("messages", 0),
        "opt_outs": bucket.get("opt_outs", 0),
        "net_value": round(bucket.get("net_value", 0.0), 2),
    }


def policy_snapshot(p: PolicyTotals, extra: dict | None = None) -> dict:
    out = {
        "name": p.name,
        "n": p.n,
        "recovered": p.recovered,
        "recovery_rate": round(p.recovery_rate, 6),
        "natural": p.natural,
        "natural_rate": round(p.natural_rate, 6),
        "debit_attempts": p.debit_attempts,
        "wasted_debits": p.wasted_debits,
        "impossible_debits": getattr(p, "impossible_debits", 0),
        "messages": p.messages,
        "messages_sms": p.messages_sms,
        "messages_whatsapp": p.messages_whatsapp,
        "messages_email": p.messages_email,
        "messages_per_recovery": round(p.messages_per_recovery, 4),
        "opted_out_triggered": p.opted_out_triggered,
        "recovered_rupees": round(p.recovered_rupees, 2),
        "cost": round(p.cost, 2),
        "net_value": round(p.net_value, 2),
        "by_class": {cid: _class_block(b) for cid, b in p.by_class.items()},
    }
    if extra:
        out.update(extra)
    return out


def collect(label: str) -> dict:
    world = load_world()
    ident = identity_mismatches(world)
    ctl = control_totals(world)
    a = run_schedule(world, schedule_a, "A retry@24h")
    b = run_schedule(world, schedule_b, "B SMS+3 retries")
    c = run_schedule(world, schedule_c, "C aggressive")
    agent, conn = run_agent(world)
    rejections = int(getattr(agent, "rejections", 0))
    downtime_messages = int(getattr(agent, "downtime_messages", 0))
    conn.close()

    nsf = agent.by_class.get("insufficient_funds", {})
    nsf_rate = rate(nsf.get("recovered", 0), nsf.get("n", 0))

    return {
        "label": label,
        "seed": RANDOM_SEED,
        "n_payments": len(world["pay_vis"]),
        "identity_mismatches": ident,
        "control": policy_snapshot(ctl),
        "A": policy_snapshot(a),
        "B": policy_snapshot(b),
        "C": policy_snapshot(c),
        "agent": policy_snapshot(agent, extra={
            "rejections": rejections,
            "downtime_messages": downtime_messages,
            "insufficient_funds_recovery": round(nsf_rate, 6),
            "flagged_for_review": int(getattr(agent, "flagged_for_review", 0)),
        }),
        "per_class": {
            cid: {
                "n": a.by_class[cid]["n"],
                "natural": round(rate(a.by_class[cid]["natural"], a.by_class[cid]["n"]), 6),
                "A": round(rate(a.by_class[cid]["recovered"], a.by_class[cid]["n"]), 6),
                "B": round(rate(b.by_class[cid]["recovered"], b.by_class[cid]["n"]), 6),
                "C": round(rate(c.by_class[cid]["recovered"], c.by_class[cid]["n"]), 6),
                "agent": round(rate(agent.by_class.get(cid, {}).get("recovered", 0),
                                    a.by_class[cid]["n"]), 6),
                "B_wasted": b.by_class[cid].get("wasted", 0),
                "B_impossible": b.by_class[cid].get("impossible", 0),
                "agent_wasted": agent.by_class.get(cid, {}).get("wasted", 0),
                "agent_impossible": agent.by_class.get(cid, {}).get("impossible", 0),
                "A_net_value": round(a.by_class[cid].get("net_value", 0.0), 2),
                "B_net_value": round(b.by_class[cid].get("net_value", 0.0), 2),
                "agent_net_value": round(agent.by_class.get(cid, {}).get("net_value", 0.0), 2),
            }
            for cid in a.by_class
        },
    }


def write_results(path: Path, label: str) -> dict:
    payload = collect(label)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def main() -> int:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results.json")
    label = sys.argv[2] if len(sys.argv) > 2 else dest.stem
    payload = write_results(dest, label)
    print(f"  wrote {dest}  identity={payload['identity_mismatches']}  "
          f"B={payload['B']['recovery_rate']:.1%}  "
          f"agent={payload['agent']['recovery_rate']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
