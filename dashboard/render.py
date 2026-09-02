"""Display formatting. App and tests share this so figures cannot drift."""

from __future__ import annotations

import json
from datetime import datetime

MESSAGE_ACTIONS = {
    "send_payment_link",
    "request_instrument_update",
    "request_mandate_reauth",
    "send_reminder",
}


def pct(x: float) -> str:
    return f"{x:.1%}"


def pp(x: float) -> str:
    return f"{x * 100:+.1f}"


def inr(x: float) -> str:
    return f"{x:,.0f}"


def lakhs(x: float) -> str:
    return f"₹{x / 100_000:.1f}L"


def sum_treatment_amounts(rows: list[dict]) -> float:
    """Treatment-arm total. That is revenue at risk."""
    return sum(
        float(r["amount"])
        for r in rows
        if str(r.get("arm", "")).strip().lower() == "treatment"
    )


def _wasted_delta_label(delta: int) -> str:
    if delta < 0:
        return f"−{abs(delta)} vs baseline"
    if delta > 0:
        return f"+{delta} vs baseline"
    return "0 vs baseline"


def headline_metrics(
    agent: dict, baseline_b: dict, control: dict, at_risk: float,
) -> list[tuple[str, str, str]]:
    """Four hero cards. Values come from JSON + the batch CSV so app.py never hardcodes them."""
    recovered = float(agent["recovered_rupees"])
    of_risk = recovered / at_risk if at_risk else 0.0
    vs_ctl = agent["recovery_rate"] - control["recovery_rate"]
    wasted_delta = int(agent["wasted_debits"]) - int(baseline_b["wasted_debits"])
    return [
        ("Revenue at risk", lakhs(at_risk), ""),
        ("Recovered", lakhs(recovered), f"{pct(of_risk)} of at-risk"),
        ("Incremental lift", f"{pp(vs_ctl)} pp", "vs no-intervention control"),
        ("Wasted debits", str(int(agent["wasted_debits"])), _wasted_delta_label(wasted_delta)),
    ]


def caveat(control_n: int) -> str:
    return (
        f"control n={control_n}; rupee figures on this arm are noisy — "
        "headline is recovery-rate lift"
    )


def comparison_row(label: str, policy: dict, control: dict) -> dict:
    lift = "—" if label == "Control" else pp(policy["recovery_rate"] - control["recovery_rate"])
    return {
        "Policy": label,
        "Recovery": pct(policy["recovery_rate"]),
        "Lift": lift,
        "Wasted": policy["wasted_debits"],
        "Impossible": policy["impossible_debits"],
        "Msgs": policy["messages"],
        "Msgs/rec": f"{policy['messages_per_recovery']:.2f}",
        "Net ₹": inr(policy["net_value"]),
    }


def per_class_lift_rows(agent: dict, baseline_b: dict) -> list[dict]:
    rows = []
    for cid, ab in agent["by_class"].items():
        bb = baseline_b["by_class"].get(cid, {})
        ar = ab.get("recovery_rate", 0.0)
        br = bb.get("recovery_rate", 0.0)
        rows.append({
            "class": cid,
            "n": ab.get("n", 0),
            "B": pct(br),
            "agent": pct(ar),
            "lift": pp(ar - br),
            "lift_raw": ar - br,
        })
    rows.sort(key=lambda r: r["lift_raw"], reverse=True)
    for r in rows:
        r.pop("lift_raw")
    return rows


def args_of(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def rel(failed_at: str, ts: str) -> str:
    try:
        a = datetime.fromisoformat(failed_at)
        b = datetime.fromisoformat(ts)
    except ValueError:
        return ts[11:16] if len(ts) >= 16 else ts
    delta = b - a
    if delta.days >= 1:
        return f"+{delta.days}d {b:%H:%M}"
    hours = int(delta.total_seconds() // 3600)
    if hours >= 1:
        return f"+{hours}h {b:%H:%M}"
    return f"{b:%H:%M}"


def gate_label(result: str) -> str:
    return (result or "").upper()


def timeline_lines(pid: str, header: dict | None, rows: list[dict]) -> list[str]:
    """Plain-text chain for one payment. Empty means the lookup failed."""
    if header is None and not rows:
        return []
    amount = header["amount"] if header else "?"
    fclass = (
        header["failure_class"] if header
        else (rows[0]["failure_class"] if rows else "")
    )
    mandate = header["has_active_mandate"] if header else False
    err = header.get("error_reason", "") if header else ""
    failed_at = header.get("failed_at", "") if header else ""
    staged = bool(header and header.get("staged"))

    lines = [
        f"{pid}  ₹{int(amount):,}  {fclass}  mandate: "
        f"{'YES' if mandate else 'NO'}"
        + ("  (staged demo)" if staged else "")
    ]
    if failed_at and err:
        lines.append(
            f"{rel(failed_at, failed_at)}  diagnosed {fclass} (from {err!r})"
        )
    for r in rows:
        args = args_of(r.get("action_args"))
        when = rel(failed_at, r["timestamp"]) if failed_at else r["timestamp"]
        action = r["chosen_action"]
        policy = args.get("policy_id") or ""
        channel = args.get("channel") or ""
        gate = gate_label(r["gate_result"])
        reason = r.get("gate_reason") or ""
        if r["executed"] and action == "pre_debit_notification":
            viol = "  WINDOW VIOLATION" if args.get("window_violation") else ""
            lines.append(
                f"{when}  pre-debit notice  {args.get('merchant', '')}  "
                f"₹{int(args.get('amount') or 0):,}  "
                f"debit {args.get('debit_at', '')}  "
                f"{args.get('e_mandate_ref', '')}  {args.get('reason', '')}"
                f"{viol}  gate: {gate}  {reason}"
            )
        elif r["executed"] and action in MESSAGE_ACTIONS:
            bit = f"sent  {channel or 'sms'}"
            if policy:
                bit += f"  {policy}"
            lines.append(f"{when}  {bit}  gate: {gate}  {reason}")
        else:
            verb = "executed" if r["executed"] else "proposed"
            extra = f" ({args['reason']})" if args.get("reason") else ""
            lines.append(
                f"{when}  {verb} {action}{extra}  gate: {gate}  {reason}"
            )
    if header and header.get("recovered") and header.get("recovered_at"):
        when = rel(failed_at, header["recovered_at"]) if failed_at else header["recovered_at"]
        src = header.get("source") or ""
        tail = f" ({src})" if src else ""
        lines.append(f"{when}  RECOVERED ₹{int(amount):,}{tail}")
    elif header and staged and header.get("body"):
        lines.append(header["body"])
    return lines
