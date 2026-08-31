"""Streamlit dashboard. Reads results/*.json and results/audit.db only.

    streamlit run dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from dashboard.data import (
    POLICY_FILES,
    fetch_timeline,
    load_bookmarks,
    load_payments,
    load_policy,
)
from dashboard.render import (
    caveat,
    comparison_row,
    gate_label,
    per_class_lift_rows,
    timeline_lines,
)

st.set_page_config(page_title="Recovery agent", layout="wide")
st.markdown(
    "<style>.gate-rej{color:#b91c1c;font-weight:600}</style>",
    unsafe_allow_html=True,
)


def _gate_html(result: str) -> str:
    lab = gate_label(result)
    if result == "rejected":
        return f'<span class="gate-rej">{lab}</span>'
    return lab


def _html_chain(lines: list[str]) -> str:
    out = []
    for line in lines:
        html = (
            line.replace("  ", " &nbsp; ")
            .replace("gate: REJECTED", "gate: " + _gate_html("rejected"))
        )
        if line.startswith("PAY_") or line.startswith("PAY"):
            parts = line.split("  ", 1)
            html = f"**{parts[0]}**" + (
                " &nbsp; " + parts[1].replace("  ", " &nbsp; ") if len(parts) > 1 else ""
            )
        if " RECOVERED " in line:
            html = html.replace("RECOVERED", "**RECOVERED**")
        out.append(html)
    return "<br>".join(out)


def view_summary() -> None:
    ctl = load_policy("Control")
    labels = st.multiselect(
        "Policies",
        list(POLICY_FILES),
        default=["Control", "Baseline B", "Agent"],
    )
    if not labels:
        st.info("Select at least one policy.")
        return
    rows = [comparison_row(lab, load_policy(lab), ctl) for lab in labels]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(caveat(ctl["n"]))

    agent = load_policy("Agent")
    b = load_policy("Baseline B")
    st.subheader("Per-class lift vs B")
    st.dataframe(
        pd.DataFrame(per_class_lift_rows(agent, b)),
        hide_index=True,
        width="stretch",
    )


def view_timeline() -> None:
    payments = load_payments()
    bookmarks = load_bookmarks()
    if "pay_id" not in st.session_state:
        st.session_state.pay_id = bookmarks[0]["id"]

    cols = st.columns(len(bookmarks))
    for col, bm in zip(cols, bookmarks):
        if col.button(bm["id"], help=bm["note"], width="stretch"):
            st.session_state.pay_id = bm["id"]

    typed = st.text_input("Payment ID", key="pay_id")
    pid = typed.strip()

    header = payments.get(pid)
    rows = fetch_timeline(pid)
    lines = timeline_lines(pid, header, rows)
    if not lines:
        st.warning(f"{pid} is not in this precomputed batch.")
        return
    note = next((b["note"] for b in bookmarks if b["id"] == pid), "")
    if note:
        st.caption(note)
    st.markdown(_html_chain(lines), unsafe_allow_html=True)


def view_restraint() -> None:
    agent = load_policy("Agent")
    b = load_policy("Baseline B")
    msg_rows = []
    for cid, ab in agent["by_class"].items():
        bb = b["by_class"].get(cid, {})
        msg_rows.append({
            "class": cid,
            "agent msgs": ab.get("messages", 0),
            "B msgs": bb.get("messages", 0),
        })
    st.subheader("Messages by class")
    st.caption(
        f"technical_downtime with a mandate: agent "
        f"{agent.get('downtime_mandate_messages', 0)}, "
        f"B {b.get('downtime_mandate_messages', 0)}."
    )
    st.dataframe(pd.DataFrame(msg_rows), hide_index=True, width="stretch")

    order = ["schedule_exhausted", "attempt_budget", "opted_out", "no_viable_action"]
    closes = agent.get("close_reasons", {})
    st.subheader("Close reasons")
    st.dataframe(
        pd.DataFrame([{"reason": k, "n": int(closes.get(k, 0))} for k in order]),
        hide_index=True,
        width="stretch",
    )

    gates = dict(agent.get("gate_rejections", {}))
    gates.setdefault("offer_beyond_policy", 0)
    st.subheader("Gate rejections")
    st.caption("Same table, two layers: action gate and offer validator.")
    st.dataframe(
        pd.DataFrame(
            [{"reason": k, "n": n} for k, n in sorted(gates.items(), key=lambda kv: -kv[1])]
        ),
        hide_index=True,
        width="stretch",
    )

    trai = agent.get("trai") or {}
    st.subheader("TRAI category")
    st.caption(
        "A no-offer recovery message is service-class (24/7, DND-exempt). "
        "Offer copy reclassifies it as promotional (09:00–21:00, DND-scrubbed). "
        "The bounded-offer validator is doing that compliance work."
    )
    st.dataframe(
        pd.DataFrame([
            {"bucket": "sent as service", "n": int(trai.get("service", 0))},
            {"bucket": "reclassified promotional (offer in body)",
             "n": int(trai.get("promotional", 0))},
            {"bucket": "shifted or suppressed as a result",
             "n": int(trai.get("shifted_or_suppressed", 0))},
        ]),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"RBI pre-debit notices: {int(agent.get('pre_debit_notifications', 0))} "
        f"({int(agent.get('pre_debit_window_violations', 0))} with less than 24h notice). "
        "Not a conversion lever — audit only. TRAI service/promotional counts are "
        "audit send-attempts (the schedule continues after recovery); "
        f"conversion-counted messages are {agent['messages']}."
    )


def view_efficiency() -> None:
    agent = load_policy("Agent")
    b = load_policy("Baseline B")
    ch = load_policy("Agent (channel)")

    st.subheader("Wasted vs impossible debits")
    chart = pd.DataFrame(
        {
            "wasted": [b["wasted_debits"], agent["wasted_debits"]],
            "impossible": [b["impossible_debits"], agent["impossible_debits"]],
        },
        index=["Baseline B", "Agent"],
    )
    st.bar_chart(chart)

    st.subheader("Channel mix")
    st.dataframe(
        pd.DataFrame(
            [
                {"policy": "Baseline B", **b.get("channel_mix", {})},
                {"policy": "Agent", **agent.get("channel_mix", {})},
                {"policy": "Agent (channel)", **ch.get("channel_mix", {})},
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    st.subheader("Cost breakdown")
    st.dataframe(
        pd.DataFrame(
            [
                {"policy": "Baseline B", **b.get("cost_breakdown", {})},
                {"policy": "Agent", **agent.get("cost_breakdown", {})},
                {"policy": "Baseline C", **load_policy("Baseline C").get("cost_breakdown", {})},
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption("Higher cost per message, lower cost overall.")


def main() -> None:
    st.title("Recovery agent")
    v1, v2, v3, v4 = st.tabs([
        "Batch summary",
        "Per-payment timeline",
        "Restraint and stopping",
        "Efficiency",
    ])
    with v1:
        view_summary()
    with v2:
        view_timeline()
    with v3:
        view_restraint()
    with v4:
        view_efficiency()


if __name__ == "__main__":
    main()
