"""
Run the rule-based agent on the treatment arm.

    python -m eval.run_agent

Loads visible CSVs into diagnose/policy/guardrails. Hidden files are
touched only here, at the evaluation boundary.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from agent.actions import DEBIT_ACTIONS, MESSAGE_ACTIONS, Decision
from agent.loop import build_schedule, second_ask_p2
from agent.messaging import generate
from agent.ml_options import ML_APPS, MlOptions
from audit.log import count_flagged, count_rejections, fetch_payment, log_decision, reset
from baselines.aggressive_dunning import schedule as schedule_c
from baselines.fixed_retry import schedule as schedule_a
from baselines.retry_plus_sms import schedule as schedule_b
from eval.metrics import (
    DATA,
    PolicyTotals,
    add_row,
    control_totals,
    identity_mismatches,
    load_world,
    parse_bool,
    rate,
    resolve_data,
    run_schedule,
    score_outcome,
)
from eval.run_baselines import _print_headline, _print_per_class
from model.features import FEATURES, delay_hours_of, extract
from model.labels import converting_step_labels
from simulator.response import (
    Action,
    latents_from_row,
    payment_hidden_from_row,
    payment_visible_from_row,
    respond,
)


def _to_sim_action(at, decision: Decision) -> Action:
    return Action(name=decision.action, at=at, args=dict(decision.args))


def _log_pre_debit(conn, vis, debit_at: datetime, diagnosed: str,
                   attempt: int) -> dict:
    """RBI e-mandate pre-transaction notice. Audit only — not a sim action."""
    failed_at = datetime.fromisoformat(vis.failed_at)
    notice_at = debit_at - timedelta(hours=24)
    violation = notice_at < failed_at
    log_at = failed_at if violation else notice_at
    args = {
        "merchant": "your merchant",
        "amount": int(vis.amount),
        "debit_at": debit_at.isoformat(timespec="seconds"),
        "e_mandate_ref": f"EM-{vis.payment_id}",
        "reason": vis.error_reason,
        "fields_present": True,
        "window_violation": violation,
    }
    log_decision(
        conn,
        payment_id=vis.payment_id,
        attempt_number=attempt,
        timestamp=log_at.isoformat(timespec="seconds"),
        failure_class=diagnosed,
        chosen_action="pre_debit_notification",
        action_args=args,
        gate_result="allowed",
        gate_reason="pre_debit_window_violation" if violation else "ok",
        executed=True,
    )
    return args


def _trai_counts(steps) -> dict:
    """Executed sends, plus promotional traffic the TRAI window/DND blocked."""
    service = promotional = shifted_or_suppressed = 0
    for step in steps:
        promo = step.category == "promotional" or step.gate_reason in {
            "dnd_registry", "quiet_hours",
        }
        sent = (
            step.executed is not None
            and step.executed.action in MESSAGE_ACTIONS
        )
        blocked = step.gate_reason in {"dnd_registry", "quiet_hours"}
        if sent and promo:
            promotional += 1
            if step.shifted:
                shifted_or_suppressed += 1
        elif sent:
            service += 1
        elif blocked:
            promotional += 1
            shifted_or_suppressed += 1
    return {
        "service": service,
        "promotional": promotional,
        "shifted_or_suppressed": shifted_or_suppressed,
    }


def _decision_row(vis, cust, step, recovered: bool, seed: int | None) -> dict:
    ch = step.executed.args.get("channel", "") if step.executed else ""
    delay = delay_hours_of(vis, step.at, step.executed or step.proposed)
    feats = extract(
        vis, cust,
        action_type=(step.executed or step.proposed).action,
        channel=ch,
        delay_hours=delay,
        step_index=step.attempt_number,
    )
    out = {
        "payment_id": vis.payment_id,
        "customer_id": vis.customer_id,
        "seed": seed if seed is not None else "",
        "recovered": int(recovered),
    }
    out.update(feats)
    return out


def run_agent(world, ml: MlOptions | None = None,
              log_path: Path | None = None,
              seed: int | None = None,
              trace_id: str | None = None) -> tuple[PolicyTotals, object]:
    totals = PolicyTotals(name="agent")
    conn = reset()
    classes = world["classes"]
    downtime_messages = 0
    traced_id = None
    logged: list[dict] = []
    headers: dict[str, dict] = {}
    trai = {"service": 0, "promotional": 0, "shifted_or_suppressed": 0}
    pre_debit_n = 0
    pre_debit_violations = 0
    ml = ml or MlOptions()
    if ml.dropped is None:
        ml.dropped = []
    p2_candidates_by_class: dict[str, int] = {}
    p2_quartile_by_class: dict[str, int] = {}
    if (ml.use_model and ml.app == "second_ask"
            and ml.p2_percentile is not None and ml.p2_threshold is None):
        import numpy as np
        scored: list[tuple[str, float]] = []
        for row in world["pay_vis"]:
            vis = payment_visible_from_row(row)
            if vis.arm != "treatment":
                continue
            cust = world["customers"][vis.customer_id]
            p2 = second_ask_p2(vis, cust, calibrated=ml.calibrated)
            if p2 is None:
                continue
            cls = str(getattr(vis, "failure_class", "") or "")
            scored.append((cls, p2))
            p2_candidates_by_class[cls] = p2_candidates_by_class.get(cls, 0) + 1
        if scored:
            ml.p2_threshold = float(np.percentile(
                [p for _, p in scored], ml.p2_percentile,
            ))
            for cls, p2 in scored:
                if p2 <= ml.p2_threshold:
                    p2_quartile_by_class[cls] = p2_quartile_by_class.get(cls, 0) + 1

    for row in world["pay_vis"]:
        vis = payment_visible_from_row(row)
        if vis.arm != "treatment":
            continue
        cust = world["customers"][vis.customer_id]
        diagnosed, steps = build_schedule(vis, cust, ml=ml)
        for k, v in _trai_counts(steps).items():
            trai[k] += v

        sim_actions = []
        for step in steps:
            executed = step.executed is not None
            args = dict((step.executed or step.proposed).args)
            if (executed and step.executed is not None
                    and step.executed.action in MESSAGE_ACTIONS):
                # Copy only — does not choose the action.
                msg = generate(vis, cust)
                args["policy_id"] = msg.policy_id
                args["template_id"] = msg.template_id
                args["trai_category"] = step.category
            if (executed and step.executed is not None
                    and step.executed.action in DEBIT_ACTIONS
                    and vis.has_active_mandate):
                notice = _log_pre_debit(
                    conn, vis, step.at, step.diagnosed_class, step.attempt_number,
                )
                pre_debit_n += 1
                pre_debit_violations += int(notice["window_violation"])
            log_decision(
                conn,
                payment_id=vis.payment_id,
                attempt_number=step.attempt_number,
                timestamp=step.at.isoformat(timespec="seconds"),
                failure_class=step.diagnosed_class,
                chosen_action=step.proposed.action,
                action_args=args,
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
        headers[vis.payment_id] = {
            "amount": int(vis.amount),
            "method": getattr(vis, "method", ""),
            "failed_at": vis.failed_at,
            "error_reason": vis.error_reason,
            "failure_class": vis.failure_class,
            "has_active_mandate": bool(vis.has_active_mandate),
            "customer_id": vis.customer_id,
            "preferred_channel": cust.get("preferred_channel", ""),
            "recovered": bool(out.recovered),
            "recovered_at": out.recovered_at,
            "source": out.source,
        }
        if log_path is not None:
            executed_logged = []
            for i, step in enumerate(steps):
                if step.executed is None:
                    continue
                if step.executed.action not in MESSAGE_ACTIONS and step.executed.action not in {
                    "retry_debit", "schedule_for_payday", "wait_for_downtime_recovery",
                }:
                    continue
                executed_logged.append((i, step))
            rec_at = None
            if out.recovered_at:
                rec_at = datetime.fromisoformat(out.recovered_at)
            ys = converting_step_labels(
                [s.at for _, s in executed_logged],
                out.recovered, rec_at, out.source,
            )
            for (i, step), y in zip(executed_logged, ys):
                logged.append(_decision_row(vis, cust, step, bool(y), seed))
                logged[-1]["step_index"] = i

        if trace_id and vis.payment_id == trace_id:
            traced_id = vis.payment_id
        elif traced_id is None and trace_id is None and any(
                s.gate_result == "rejected" for s in steps):
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
    totals.trai = trai  # type: ignore[attr-defined]
    totals.pre_debit_notifications = pre_debit_n  # type: ignore[attr-defined]
    totals.pre_debit_window_violations = pre_debit_violations  # type: ignore[attr-defined]
    totals.conn = conn  # type: ignore[attr-defined]
    totals.payment_headers = headers  # type: ignore[attr-defined]
    dropped = list(ml.dropped) if (ml and ml.dropped) else []
    by_cls: dict[str, int] = {}
    for cls, _reason in dropped:
        by_cls[cls] = by_cls.get(cls, 0) + 1
    totals.suppress_by_class = by_cls  # type: ignore[attr-defined]
    totals.n_suppressed = len(dropped)  # type: ignore[attr-defined]
    totals.p2_threshold = ml.p2_threshold  # type: ignore[attr-defined]
    totals.p2_candidates_by_class = p2_candidates_by_class  # type: ignore[attr-defined]
    totals.p2_quartile_by_class = p2_quartile_by_class  # type: ignore[attr-defined]
    if log_path is not None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["payment_id", "customer_id", "seed", "recovered", *FEATURES]
        with log_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(logged)
    return totals, conn


def _print_trace(conn, payment_id: str) -> None:
    rows = fetch_payment(conn, payment_id)
    print(f"\n  end-to-end trace  {payment_id}  ({len(rows)} decisions)")
    print(f"  {'at':<22}{'class':<22}{'action':<28}{'gate':<10}{'reason'}")
    print("  " + "-" * 90)
    for r in rows:
        print(f"  {r['timestamp']:<22}{r['failure_class']:<22}"
              f"{r['chosen_action']:<28}{r['gate_result']:<10}{r['gate_reason']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data", default=str(DATA.relative_to(DATA.parent)),
        help="Batch directory. Default data/ is the published canonical batch; "
             "robustness runs pass --data so we never overwrite headline numbers. "
             "Policy still loads the canonical taxonomy; this batch's p_resolves "
             "is already in ground_truth.csv.")
    ap.add_argument(
        "--log-decisions", default=None,
        help="Write one CSV row per executed step (visible features + action + "
             "converting-step label). Credit the last action at recovered_at "
             "if source=action; natural recoveries get 0. No hidden/GT columns.")
    ap.add_argument(
        "--use-model", action="store_true", default=False,
        help="Score message channel / suppression / second-ask with the "
             "propensity model. Off by default so the published rule floor "
             "stays `python -m eval.run_agent`.")
    ap.add_argument(
        "--ml-app", choices=ML_APPS, default="channel",
        help="Which model application to stack. channel < suppress < second_ask.")
    ap.add_argument(
        "--explore-channel", type=float, default=0.0,
        help="Train-only: probability of replacing preferred_channel with a "
             "random channel so LightGBM sees sms/whatsapp/email. Must stay "
             "0.0 on eval seeds 42/1/2/7/99/123.")
    ap.add_argument(
        "--seed", type=int, default=None,
        help="Recorded on --log-decisions rows; also seeds channel exploration.")
    ap.add_argument(
        "--calibrated", action="store_true", default=False,
        help="Apply val-set isotonic calibration at score time. Off by default.")
    ap.add_argument(
        "--p2-percentile", type=float, default=None,
        help="second_ask only: drop extra asks with p(step=2) at or below this "
             "percentile of the batch. Diagnostic rank filter; off by default.")
    ap.add_argument(
        "--trace", default=None,
        help="Print this payment's audit chain after the batch "
             "(e.g. PAY_00071). Default: first gate rejection.",
    )
    args = ap.parse_args(argv)
    if args.ml_app == "unconditional_second_ask" and args.use_model:
        print("  NOTE  unconditional_second_ask is a no-model ablation; "
              "ignoring --use-model")
    ml = MlOptions(
        use_model=args.use_model and args.ml_app != "unconditional_second_ask",
        app=args.ml_app,
        explore_channel=args.explore_channel,
        rng=random.Random(args.seed) if args.explore_channel > 0 else None,
        calibrated=args.calibrated,
        p2_percentile=args.p2_percentile,
    )
    world = load_world(resolve_data(args.data))
    ident = identity_mismatches(world)
    ctl = control_totals(world)
    ctl._ident_ok = ident == 0  # type: ignore[attr-defined]
    a = run_schedule(world, schedule_a, "A retry@24h")
    b = run_schedule(world, schedule_b, "B SMS+3 retries")
    c = run_schedule(world, schedule_c, "C aggressive")
    agent, conn = run_agent(
        world, ml=ml,
        log_path=Path(args.log_decisions) if args.log_decisions else None,
        seed=args.seed,
        trace_id=args.trace,
    )

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
        print(f"  NOTE  messages {agent.messages} > B {b.messages}; "
              f"restraint is m/rec {agent.messages_per_recovery:.2f} vs "
              f"B {b.messages_per_recovery:.2f}, not fewest sends")
    if agent.messages_per_recovery - 1e-9 > b.messages_per_recovery:
        errors.append(
            f"messages-per-recovery {agent.messages_per_recovery:.2f} "
            f"exceeded B {b.messages_per_recovery:.2f}"
        )
    if agent.downtime_messages:  # type: ignore[attr-defined]
        errors.append(f"technical_downtime messages={agent.downtime_messages}")
    if agent.rejections <= 0:  # type: ignore[attr-defined]
        errors.append("no gate rejections in the audit log")
    nsf = agent.by_class.get("insufficient_funds", {})
    nsf_rate = rate(nsf.get("recovered", 0), nsf.get("n", 0))
    if nsf_rate > 0.45:
        errors.append(f"insufficient_funds {nsf_rate:.1%} looks leaked (>45%)")

    if args.trace and agent.traced_id != args.trace:
        errors.append(f"--trace {args.trace} is not in this batch")

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
