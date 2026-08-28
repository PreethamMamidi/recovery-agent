"""
Run the rule-based agent on the treatment arm.

    python -m eval.run_agent

Loads visible CSVs into diagnose/policy/guardrails. Hidden files are
touched only here, at the evaluation boundary.
"""

from __future__ import annotations

import sys

from agent.actions import Decision
from agent.loop import build_schedule
from audit.log import count_flagged, count_rejections, fetch_payment, log_decision, reset
from baselines.aggressive_dunning import schedule as schedule_c
from baselines.fixed_retry import schedule as schedule_a
from baselines.retry_plus_sms import schedule as schedule_b
from eval.metrics import (
    PolicyTotals,
    add_row,
    control_totals,
    identity_mismatches,
    load_world,
    parse_bool,
    rate,
    run_schedule,
    score_outcome,
)
from eval.run_baselines import _print_headline, _print_per_class
from simulator.response import (
    Action,
    latents_from_row,
    payment_hidden_from_row,
    payment_visible_from_row,
    respond,
)


def _to_sim_action(at, decision: Decision) -> Action:
    return Action(name=decision.action, at=at, args=dict(decision.args))


def run_agent(world) -> tuple[PolicyTotals, object]:
    totals = PolicyTotals(name="agent")
    conn = reset()
    classes = world["classes"]
    downtime_messages = 0
    traced_id = None

    for row in world["pay_vis"]:
        vis = payment_visible_from_row(row)
        if vis.arm != "treatment":
            continue
        cust = world["customers"][vis.customer_id]
        diagnosed, steps = build_schedule(vis, cust)

        sim_actions = []
        for step in steps:
            executed = step.executed is not None
            log_decision(
                conn,
                payment_id=vis.payment_id,
                attempt_number=step.attempt_number,
                timestamp=step.at.isoformat(timespec="seconds"),
                failure_class=step.diagnosed_class,
                chosen_action=step.proposed.action,
                action_args=step.proposed.args,
                gate_result=step.gate_result,
                gate_reason=step.gate_reason,
                executed=executed,
                flagged_for_review=step.flagged_for_review,
            )
            if step.executed is not None:
                sim_actions.append(_to_sim_action(step.at, step.executed))
                if (diagnosed == "technical_downtime"
                        and vis.has_active_mandate
                        and step.executed.action in {
                            "send_reminder", "send_payment_link",
                            "request_instrument_update", "request_mandate_reauth"}):
                    downtime_messages += 1

        hid = payment_hidden_from_row(world["pay_hid"][vis.payment_id])
        lat = latents_from_row(world["latents"][vis.customer_id])
        fc = classes[vis.failure_class]
        gt = world["truth"][vis.payment_id]
        opted = parse_bool(cust["opted_out"])
        out = respond(vis, hid, lat, fc, gt, sim_actions, opted_out=opted)
        add_row(totals, score_outcome(vis, cust, gt, out))

        if traced_id is None and any(s.gate_result == "rejected" for s in steps):
            traced_id = vis.payment_id

        for step in steps:
            if step.executed is None:
                continue
            conn.execute(
                """UPDATE decisions SET outcome = ?, cost = ?
                   WHERE payment_id = ? AND timestamp = ? AND chosen_action = ?""",
                (
                    "recovered" if out.recovered else "not_recovered",
                    None,
                    vis.payment_id,
                    step.at.isoformat(timespec="seconds"),
                    step.executed.action,
                ),
            )

    conn.commit()
    totals.downtime_messages = downtime_messages  # type: ignore[attr-defined]
    totals.rejections = count_rejections(conn)  # type: ignore[attr-defined]
    totals.flagged_for_review = count_flagged(conn)  # type: ignore[attr-defined]
    totals.traced_id = traced_id  # type: ignore[attr-defined]
    totals.conn = conn  # type: ignore[attr-defined]
    return totals, conn


def _print_trace(conn, payment_id: str) -> None:
    rows = fetch_payment(conn, payment_id)
    print(f"\n  end-to-end trace  {payment_id}  ({len(rows)} decisions)")
    print(f"  {'at':<22}{'class':<22}{'action':<28}{'gate':<10}{'reason'}")
    print("  " + "-" * 90)
    for r in rows:
        print(f"  {r['timestamp']:<22}{r['failure_class']:<22}"
              f"{r['chosen_action']:<28}{r['gate_result']:<10}{r['gate_reason']}")


def main() -> int:
    world = load_world()
    ident = identity_mismatches(world)
    ctl = control_totals(world)
    ctl._ident_ok = ident == 0  # type: ignore[attr-defined]
    a = run_schedule(world, schedule_a, "A retry@24h")
    b = run_schedule(world, schedule_b, "B SMS+3 retries")
    c = run_schedule(world, schedule_c, "C aggressive")
    agent, conn = run_agent(world)

    print(f"\n  {len(world['pay_vis'])} payments · control {ctl.n}  |  "
          f"treatment {agent.n}")
    _print_headline(ctl, a, b, c, agent)
    _print_per_class(world["classes"], a, b, c, agent)

    print(f"\n  agent wasted debits     : {agent.wasted_debits}")
    print(f"  agent impossible debits : {agent.impossible_debits}")
    print(f"  agent messages          : {agent.messages}  (B={b.messages})")
    print(f"  downtime messages       : {agent.downtime_messages}")  # type: ignore[attr-defined]
    print(f"  gate rejections logged  : {agent.rejections}")  # type: ignore[attr-defined]
    print(f"  flagged for review      : {agent.flagged_for_review}")  # type: ignore[attr-defined]

    errors = []
    if ident:
        errors.append(f"identity failed on {ident} rows")
    if agent.recovery_rate <= 0 or agent.recovery_rate >= 0.95:
        errors.append(f"agent recovery {agent.recovery_rate:.1%} is broken")
    if agent.wasted_debits >= 50:
        errors.append(f"wasted debits {agent.wasted_debits} (want < 50)")
    if agent.messages > b.messages:
        errors.append(f"messages {agent.messages} exceeded B {b.messages}")
    if agent.downtime_messages:  # type: ignore[attr-defined]
        errors.append(f"technical_downtime messages={agent.downtime_messages}")
    if agent.rejections <= 0:  # type: ignore[attr-defined]
        errors.append("no gate rejections in the audit log")
    nsf = agent.by_class.get("insufficient_funds", {})
    nsf_rate = rate(nsf.get("recovered", 0), nsf.get("n", 0))
    if nsf_rate > 0.45:
        errors.append(f"insufficient_funds {nsf_rate:.1%} looks leaked (>45%)")

    if agent.traced_id:  # type: ignore[attr-defined]
        _print_trace(conn, agent.traced_id)

    conn.close()

    if errors:
        print("\n  GATE FAIL")
        for e in errors:
            print(f"    - {e}")
        print()
        return 1

    note = ""
    if agent.recovery_rate + 1e-9 < b.recovery_rate:
        note = ("  recovery is at or below B - lead with efficiency "
                f"({agent.wasted_debits} wasted vs B {b.wasted_debits})\n")
    print(f"\n  GATE OK  wasted debits {agent.wasted_debits}, "
          f"messages {agent.messages}, rejections {agent.rejections}")  # type: ignore[attr-defined]
    print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
