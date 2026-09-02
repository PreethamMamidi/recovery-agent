"""Load precomputed dashboard files. JSON is cached; SQLite is not."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import streamlit as st

try:
    from dashboard.explore import apply_filters, build_treatment_frame
    from dashboard.render import sum_treatment_amounts
except ImportError:
    from explore import apply_filters, build_treatment_frame
    from render import sum_treatment_amounts

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"
# Timeline uses the committed results copy, not the gitignored working log.
AUDIT = RESULTS / "audit.db"
CASES = ROOT / "config" / "demo_cases.json"
VISIBLE = DATA / "payments_visible.csv"

POLICY_FILES = {
    "Control": "control.json",
    "Baseline A": "baseline_a.json",
    "Baseline B": "baseline_b.json",
    "Baseline C": "baseline_c.json",
    "Agent": "agent.json",
    "Agent (channel)": "agent_channel.json",
    "Agent (quartile)": "agent_quartile.json",
}


@st.cache_data
def load_json(name: str) -> dict:
    path = RESULTS / name
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data
def load_policy(label: str) -> dict:
    return load_json(POLICY_FILES[label])


@st.cache_data
def load_payments() -> dict:
    return load_json("payments.json")


@st.cache_data
def treatment_at_risk() -> float:
    with open(VISIBLE, newline="", encoding="utf-8") as fh:
        return sum_treatment_amounts(list(csv.DictReader(fh)))


@st.cache_data
def load_treatment_payments():
    """Treatment arm + agent outcomes. Read once; filters reuse this frame."""
    with open(VISIBLE, newline="", encoding="utf-8") as fh:
        vis = list(csv.DictReader(fh))
    return build_treatment_frame(vis, load_payments())


@st.cache_data
def filtered_payments(
    classes: tuple,
    band_lo: str,
    band_hi: str,
    mandate: str,
    outcome: str,
):
    return apply_filters(
        load_treatment_payments(),
        classes,
        (band_lo, band_hi),
        mandate,
        outcome,
    )


@st.cache_data
def load_bookmarks() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    out = []
    for story in data["day6_stories"]:
        out.append({"id": story["id"], "label": story["title"], "note": story["note"]})
    for case in data["bounded_offers"]:
        if case["id"] in {c["id"] for c in out}:
            continue
        if case.get("from_batch"):
            continue
        out.append({
            "id": case["id"],
            "label": case["id"],
            "note": case.get("note", ""),
        })
    return out


def fetch_timeline(payment_id: str) -> list[dict]:
    """One indexed lookup. Does not load the table."""
    if not AUDIT.exists():
        return []
    conn = sqlite3.connect(f"file:{AUDIT}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT timestamp, failure_class, chosen_action, action_args,
                  gate_result, gate_reason, executed, outcome, flagged_for_review
           FROM decisions WHERE payment_id = ? ORDER BY id""",
        (payment_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
