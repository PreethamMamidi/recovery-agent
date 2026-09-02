"""Streamlit dashboard. Reads results/*.json and results/audit.db for the
batch views. The Try it tab runs diagnose → policy → gate → simulator on
one payment. It never scores the batch.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd
import streamlit as st

try:
    from dashboard.data import (
        POLICY_FILES,
        fetch_timeline,
        filtered_payments,
        load_bookmarks,
        load_payments,
        load_policy,
        load_treatment_payments,
        treatment_at_risk,
    )
    from dashboard.explore import (
        AMOUNT_BANDS,
        MANDATE_OPTIONS,
        OUTCOME_OPTIONS,
        class_options,
        cumulative_trend,
        per_class_breakdown,
        showing_label,
        slice_summary,
    )
    from dashboard.render import (
        caveat,
        comparison_row,
        gate_label,
        headline_metrics,
        lakhs,
        pct,
        per_class_lift_rows,
        timeline_lines,
    )
    from dashboard.sandbox import (
        CLASS_IDS,
        ERROR_REASONS,
        PRESET_NOTES,
        PRESETS,
        run_batch,
        run_invented,
    )
except ImportError:
    from data import (
        POLICY_FILES,
        fetch_timeline,
        filtered_payments,
        load_bookmarks,
        load_payments,
        load_policy,
        load_treatment_payments,
        treatment_at_risk,
    )
    from explore import (
        AMOUNT_BANDS,
        MANDATE_OPTIONS,
        OUTCOME_OPTIONS,
        class_options,
        cumulative_trend,
        per_class_breakdown,
        showing_label,
        slice_summary,
    )
    from render import (
        caveat,
        comparison_row,
        gate_label,
        headline_metrics,
        lakhs,
        pct,
        per_class_lift_rows,
        timeline_lines,
    )
    from sandbox import (
        CLASS_IDS,
        ERROR_REASONS,
        PRESET_NOTES,
        PRESETS,
        run_batch,
        run_invented,
    )

st.set_page_config(
    page_title="Recovery Agent",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(
    """
<style>
#MainMenu, footer, header {visibility: hidden;}
.block-container {padding-top: 2.5rem; max-width: 1100px;}
[data-testid="stMetricValue"] {font-size: 2.2rem; font-weight: 500; color: #1A1A19;}
[data-testid="stMetricLabel"] {font-size: 0.8rem; text-transform: none;}
div[data-testid="column"]:nth-of-type(2) [data-testid="stMetricValue"],
div[data-testid="column"]:nth-of-type(3) [data-testid="stMetricValue"] {
    color: #0F6E56;
}
.thesis {font-size: 1.15rem; margin: -0.35rem 0 0.15rem; color: #1A1A19;}
.thesis-sub {color: #6b6b68; font-size: 0.95rem; margin: 0 0 1.35rem;}
.gate-rej{color:#b91c1c;font-weight:600}
.decision-card {
    background: #F7F6F3;
    padding: 1.35rem 1.5rem 1.45rem;
    border-radius: 8px;
    margin: 0.75rem 0 0.5rem;
}
.decision-card .card-head {font-weight: 600; font-size: 1.05rem; margin-bottom: 0.2rem;}
.decision-card .sec {
    font-size: 0.7rem;
    letter-spacing: 0.08em;
    font-weight: 600;
    color: #6b6b68;
    margin: 1.15rem 0 0.4rem;
}
.decision-card .kv {
    display: grid;
    grid-template-columns: 9.5rem 1fr;
    gap: 0.12rem 1rem;
    font-size: 0.92rem;
    line-height: 1.5;
}
.decision-card .kv .k {color: #6b6b68;}
.decision-card .g {font-size: 0.92rem; line-height: 1.6;}
.decision-card .g.fail {color: #b91c1c; font-weight: 600;}
.slice-count {font-size: 1.2rem; font-weight: 600; margin: 0.85rem 0 0.35rem; color: #1A1A19;}
</style>
""",
    unsafe_allow_html=True,
)


def _kv_html(pairs: dict) -> str:
    rows = "".join(
        f'<div class="k">{_esc(k)}</div><div>{_esc(v)}</div>'
        for k, v in pairs.items()
    )
    return f'<div class="kv">{rows}</div>'


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _card_html(card: dict) -> str:
    rails = []
    for item in card.get("guardrails") or []:
        mark = "✓" if item["ok"] else "✗"
        cls = "g" if item["ok"] else "g fail"
        rails.append(f'<div class="{cls}">{mark} {_esc(item["text"])}</div>')
    out = card.get("outcome") or {}
    return (
        '<div class="decision-card">'
        f'<div class="card-head">{_esc(card.get("headline", ""))}</div>'
        '<div class="sec">DIAGNOSIS</div>'
        f'{_kv_html(card.get("diagnosis") or {})}'
        '<div class="sec">DECISION</div>'
        f'{_kv_html(card.get("decision") or {})}'
        '<div class="sec">GUARDRAILS</div>'
        f'{"".join(rails)}'
        '<div class="sec">OUTCOME</div>'
        f'{_kv_html({"agent": out.get("agent", "—"), "no intervention": out.get("no_intervention", "—")})}'
        "</div>"
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


def _render_payment_chain(pid: str) -> None:
    payments = load_payments()
    header = payments.get(pid)
    rows = fetch_timeline(pid)
    lines = timeline_lines(pid, header, rows)
    if not lines:
        st.warning(f"{pid} is not in this precomputed batch.")
        return
    note = next((b["note"] for b in load_bookmarks() if b["id"] == pid), "")
    if note:
        st.caption(note)
    st.markdown(_html_chain(lines), unsafe_allow_html=True)


def _class_rows(df) -> None:
    rows = per_class_breakdown(df)
    if not rows:
        st.info("No payments match these filters.")
        return
    h1, h2, h3, h4 = st.columns([3, 1, 1, 1])
    h1.write("**class**")
    h2.write("**n**")
    h3.write("**recovered**")
    h4.write("")
    for row in rows:
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        c1.write(row["class"])
        c2.write(str(row["n"]))
        c3.write(pct(row["rate"]))
        if c4.button("View", key=f"drill_{row['class']}"):
            st.session_state.drill_class = row["class"]
            st.session_state.selected_payment = None
            st.rerun()


def _payment_rows(df) -> None:
    if df.empty:
        st.info("No payments in this class for the current filters.")
        return
    h1, h2, h3, h4, h5 = st.columns([2, 1, 2, 1, 1])
    h1.write("**payment**")
    h2.write("**amount**")
    h3.write("**class**")
    h4.write("**mandate**")
    h5.write("")
    for rec in df.sort_values("payment_id").itertuples(index=False):
        c1, c2, c3, c4, c5 = st.columns([2, 1, 2, 1, 1])
        c1.write(rec.payment_id)
        c2.write(f"₹{int(rec.amount):,}")
        c3.write(rec.failure_class)
        c4.write("yes" if rec.has_active_mandate else "no")
        if c5.button("Open", key=f"open_{rec.payment_id}"):
            st.session_state.selected_payment = rec.payment_id
            st.rerun()


def view_summary() -> None:
    ctl = load_policy("Control")
    agent = load_policy("Agent")
    b = load_policy("Baseline B")
    cards = headline_metrics(agent, b, ctl, treatment_at_risk())
    cols = st.columns(4)
    for col, (label, value, delta) in zip(cols, cards):
        if delta:
            col.metric(label, value, delta)
        else:
            col.metric(label, value)

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

    st.subheader("Per-class lift vs B")
    st.dataframe(
        pd.DataFrame(per_class_lift_rows(agent, b)),
        hide_index=True,
        width="stretch",
    )

    frame = load_treatment_payments()
    all_classes = class_options(frame)
    f1, f2, f3, f4 = st.columns(4)
    classes = f1.multiselect("Failure class", all_classes, default=all_classes)
    band = f2.select_slider(
        "Amount", list(AMOUNT_BANDS),
        value=(AMOUNT_BANDS[0], AMOUNT_BANDS[-1]),
    )
    mandate = f3.selectbox("Mandate", list(MANDATE_OPTIONS))
    outcome = f4.selectbox("Outcome", list(OUTCOME_OPTIONS))
    lo, hi = band if isinstance(band, (tuple, list)) else (AMOUNT_BANDS[0], AMOUNT_BANDS[-1])
    filtered = filtered_payments(tuple(classes), lo, hi, mandate, outcome)

    st.session_state.setdefault("drill_class", None)
    st.session_state.setdefault("selected_payment", None)
    drill = st.session_state.drill_class
    visible = filtered[filtered["failure_class"] == drill] if drill else filtered

    summary = slice_summary(visible)
    st.markdown(
        f'<p class="slice-count">{showing_label(summary["n"], len(frame))}</p>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"{lakhs(summary['at_risk'])} at risk · "
        f"{lakhs(summary['recovered_rupees'])} recovered · "
        f"{pct(summary['rate'])}"
    )

    trend = cumulative_trend(visible)
    if not trend.empty:
        st.subheader("Cumulative at-risk vs recovered")
        st.caption(
            "At-risk is booked on the failure date. Recovered is booked on the recovery date — "
            "the 14-day window means recoveries can land after the last failure."
        )
        st.line_chart(trend)

    if drill:
        st.caption(f"Batch → {drill}")
        if st.button("← Back to all"):
            st.session_state.drill_class = None
            st.session_state.selected_payment = None
            st.rerun()
        _payment_rows(visible)
        pid = st.session_state.get("selected_payment")
        if pid:
            _render_payment_chain(pid)
    else:
        st.subheader("Per-class in this slice")
        _class_rows(filtered)


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


def view_try_it() -> None:
    st.caption(
        "This runs the same agent code on a single payment you specify. "
        "The measured results are in Batch results — this tab is for seeing "
        "how one decision is made."
    )

    st.session_state.setdefault("sandbox_class", PRESETS["Expired card"]["failure_class"])
    st.session_state.setdefault("sandbox_reason", PRESETS["Expired card"]["error_reason"])
    st.session_state.setdefault("sandbox_amount", PRESETS["Expired card"]["amount"])
    st.session_state.setdefault("sandbox_mandate", PRESETS["Expired card"]["has_active_mandate"])
    st.session_state.setdefault("sandbox_tenure", PRESETS["Expired card"]["tenure_months"])
    st.session_state.setdefault("sandbox_past_pay", PRESETS["Expired card"]["past_payment_count"])
    st.session_state.setdefault("sandbox_opted", PRESETS["Expired card"]["opted_out"])

    cols = st.columns(3)
    for col, name in zip(cols, PRESETS):
        if col.button(name, help=PRESET_NOTES[name], width="stretch"):
            spec = PRESETS[name]
            st.session_state.sandbox_mode = "Build one"
            st.session_state.sandbox_class = spec["failure_class"]
            st.session_state.sandbox_reason = spec["error_reason"]
            st.session_state.sandbox_amount = spec["amount"]
            st.session_state.sandbox_mandate = spec["has_active_mandate"]
            st.session_state.sandbox_tenure = spec["tenure_months"]
            st.session_state.sandbox_past_pay = spec["past_payment_count"]
            st.session_state.sandbox_opted = spec["opted_out"]
            st.session_state.sandbox_result = run_invented(spec)

    mode = st.radio(
        "Input",
        ("Pick from the batch", "Build one"),
        horizontal=True,
        key="sandbox_mode",
    )

    result = st.session_state.get("sandbox_result")

    if mode == "Pick from the batch":
        payments = load_payments()
        ids = sorted(pid for pid, h in payments.items() if not h.get("staged"))
        default = "PAY_00210" if "PAY_00210" in ids else (ids[0] if ids else "")
        idx = ids.index(default) if default in ids else 0
        pid = st.selectbox("Payment ID", ids, index=idx, key="sandbox_pid")
        if pid:
            header = payments.get(pid)
            rows = fetch_timeline(pid)
            result = run_batch(pid, header or {}, rows)
            note = next((b["note"] for b in load_bookmarks() if b["id"] == pid), "")
            if note:
                st.caption(note)
    else:
        cls = st.selectbox("Failure class", CLASS_IDS, key="sandbox_class")
        reasons = ERROR_REASONS[cls]
        if st.session_state.sandbox_reason not in reasons:
            st.session_state.sandbox_reason = reasons[0]
        st.selectbox("Error reason", reasons, key="sandbox_reason")
        st.number_input("Amount (₹)", min_value=99, max_value=60000, step=1,
                        key="sandbox_amount")
        st.checkbox("Has active mandate", key="sandbox_mandate")
        st.slider("Tenure (months)", 1, 36, key="sandbox_tenure")
        st.slider("Past payment count", 1, 40, key="sandbox_past_pay")
        st.checkbox("Opted out", key="sandbox_opted")
        if st.button("Run agent"):
            result = run_invented({
                "failure_class": st.session_state.sandbox_class,
                "error_reason": st.session_state.sandbox_reason,
                "amount": int(st.session_state.sandbox_amount),
                "has_active_mandate": bool(st.session_state.sandbox_mandate),
                "tenure_months": int(st.session_state.sandbox_tenure),
                "past_payment_count": int(st.session_state.sandbox_past_pay),
                "opted_out": bool(st.session_state.sandbox_opted),
            })
            st.session_state.sandbox_result = result
        result = st.session_state.get("sandbox_result")

    if not result:
        return

    card = result.get("card") or {}
    if card:
        st.markdown(_card_html(card), unsafe_allow_html=True)
    else:
        st.warning("No decision for this payment.")

    hidden = result.get("hidden") or {}
    if hidden:
        with st.expander("Hidden state the agent cannot see"):
            st.dataframe(
                pd.DataFrame([{"field": k, "value": str(v)} for k, v in hidden.items()]),
                hide_index=True,
                width="stretch",
            )


def main() -> None:
    st.title("Recovery Agent")
    st.markdown(
        '<p class="thesis">Every failed payment gets the same retry. Different failures need opposite actions.</p>'
        '<p class="thesis-sub">Measured against a control group that got nothing.</p>',
        unsafe_allow_html=True,
    )
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Batch results", "Payment timeline", "Restraint",
        "Efficiency", "Try it",
    ])
    with tab1:
        view_summary()
    with tab2:
        view_timeline()
    with tab3:
        view_restraint()
    with tab4:
        view_efficiency()
    with tab5:
        view_try_it()


if __name__ == "__main__":
    main()
