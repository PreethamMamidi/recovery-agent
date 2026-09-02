"""Dashboard figures come from JSON; every bookmark renders a full chain."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "results"
AUDIT = RESULTS / "audit.db"
AUDIT_SRC = ROOT / "audit" / "log.py"

from dashboard.render import (  # noqa: E402
    MESSAGE_ACTIONS,
    caveat,
    comparison_row,
    headline_metrics,
    lakhs,
    per_class_lift_rows,
    sum_treatment_amounts,
    timeline_lines,
)

POLICY_FILES = {
    "Control": "control.json",
    "Baseline A": "baseline_a.json",
    "Baseline B": "baseline_b.json",
    "Baseline C": "baseline_c.json",
    "Agent": "agent.json",
    "Agent (channel)": "agent_channel.json",
    "Agent (quartile)": "agent_quartile.json",
}

BOOKMARKS = [
    "PAY_00210",
    "PAY_00062",
    "PAY_00071",
    "PAY_00026",
    "PAY_00011",
    "PAY_00002",
    "PAY_HV",
    "PAY_LV",
]


def _json(name: str) -> dict:
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def _treatment_at_risk() -> float:
    import csv
    path = ROOT / "data" / "payments_visible.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        return sum_treatment_amounts(list(csv.DictReader(fh)))


def _audit_rows(pid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{AUDIT}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT timestamp, failure_class, chosen_action, action_args,
                  gate_result, gate_reason, executed, outcome, flagged_for_review
           FROM decisions WHERE payment_id = ? ORDER BY id""",
        (pid,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _chain(pid: str) -> list[str]:
    payments = _json("payments.json")
    return timeline_lines(pid, payments.get(pid), _audit_rows(pid))


# Published floor: formatter output must equal these strings.
# If JSON moves, this fails; if the formatter drifts, this fails.
PUBLISHED_ROWS = {
    "Control": {
        "Recovery": "20.9%", "Lift": "—", "Wasted": 0, "Impossible": 0,
        "Msgs": 0, "Msgs/rec": "0.00", "Net ₹": "77,878",
    },
    "Baseline A": {
        "Recovery": "26.8%", "Lift": "+6.0", "Wasted": 142, "Impossible": 417,
        "Msgs": 0, "Msgs/rec": "0.00", "Net ₹": "1,072,537",
    },
    "Baseline B": {
        "Recovery": "32.5%", "Lift": "+11.6", "Wasted": 426, "Impossible": 1094,
        "Msgs": 813, "Msgs/rec": "3.08", "Net ₹": "1,377,187",
    },
    "Baseline C": {
        "Recovery": "32.2%", "Lift": "+11.4", "Wasted": 569, "Impossible": 1427,
        "Msgs": 3297, "Msgs/rec": "12.58", "Net ₹": "85,761",
    },
    "Agent": {
        "Recovery": "41.6%", "Lift": "+20.7", "Wasted": 0, "Impossible": 0,
        "Msgs": 949, "Msgs/rec": "2.81", "Net ₹": "1,657,412",
    },
    "Agent (channel)": {
        "Recovery": "43.9%", "Lift": "+23.1", "Wasted": 0, "Impossible": 0,
        "Msgs": 928, "Msgs/rec": "2.60", "Net ₹": "1,721,991",
    },
    "Agent (quartile)": {
        "Recovery": "43.9%", "Lift": "+23.1", "Wasted": 0, "Impossible": 0,
        "Msgs": 837, "Msgs/rec": "2.34", "Net ₹": "1,722,077",
    },
}


class PrecomputeShapeTests(unittest.TestCase):
    def test_policy_files_exist_and_agent_floor_holds(self):
        needed = (
            "control.json", "baseline_a.json", "baseline_b.json", "baseline_c.json",
            "agent.json", "agent_channel.json", "agent_quartile.json",
            "payments.json",
        )
        for name in needed:
            path = RESULTS / name
            self.assertTrue(path.exists(), f"missing {path}; run python -m eval.precompute_dashboard")
        agent = _json("agent.json")
        self.assertAlmostEqual(agent["recovery_rate"], 0.415744, places=4)
        self.assertEqual(agent["messages"], 949)
        self.assertEqual(agent["wasted_debits"], 0)
        self.assertEqual(agent["impossible_debits"], 0)
        closes = agent["close_reasons"]
        self.assertEqual(closes.get("no_viable_action", 0), 0)
        self.assertEqual(
            sum(closes.get(k, 0) for k in
                ("schedule_exhausted", "attempt_budget", "opted_out", "no_viable_action")),
            813,
        )
        self.assertEqual(agent["gate_rejections"].get("offer_beyond_policy", 0), 0)
        trai = agent.get("trai") or {}
        self.assertEqual(trai.get("promotional"), 3)
        self.assertEqual(trai.get("shifted_or_suppressed"), 0)
        self.assertGreater(trai.get("service", 0), 900)
        self.assertEqual(agent.get("pre_debit_notifications"), 448)
        self.assertEqual(agent.get("pre_debit_window_violations"), 189)
        b = _json("baseline_b.json")
        self.assertEqual(b["channel_mix"]["sms"], 813)

    def test_bookmarks_are_eight(self):
        cases = json.loads((ROOT / "config" / "demo_cases.json").read_text(encoding="utf-8"))
        ids = [c["id"] for c in cases["day6_stories"]]
        ids += [c["id"] for c in cases["bounded_offers"] if not c.get("from_batch")]
        self.assertEqual(ids, BOOKMARKS)
        self.assertEqual(len(ids), 8)
        notes = {c["id"]: c["note"] for c in cases["day6_stories"]}
        self.assertIn("never proposes retry_debit", notes["PAY_00062"])
        self.assertIn("gate rejected", notes["PAY_00071"])

    def test_audit_fetch_is_indexed_lookup(self):
        src = AUDIT_SRC.read_text(encoding="utf-8")
        self.assertIn("idx_decisions_payment", src)
        dash = (ROOT / "dashboard" / "data.py").read_text(encoding="utf-8")
        self.assertIn("WHERE payment_id = ?", dash)
        self.assertNotIn("SELECT * FROM decisions", dash)
        self.assertIn('RESULTS / "audit.db"', dash)
        self.assertNotIn("audit/log.db", dash)
        self.assertTrue((RESULTS / "audit.db").exists())

    def test_app_has_no_hardcoded_headlines(self):
        src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        for stale in ("20.9%", "41.5%", "41.6%", "1,657,339", "1,657,412", "+20.6", "+20.7", "32.5%", "₹39.9L", "₹16.6L"):
            self.assertNotIn(stale, src)
        self.assertIn(
            "Every failed payment gets the same retry. Different failures need opposite actions.",
            src,
        )
        self.assertIn("Measured against a control group that got nothing.", src)

    def test_requirements_are_dashboard_only(self):
        lines = [
            ln.strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self.assertEqual(lines, ["streamlit", "pandas"])

    def test_sandbox_import_does_not_load_lightgbm(self):
        import sys
        from dashboard.sandbox import PRESETS, run_invented
        self.assertNotIn("lightgbm", sys.modules)
        out = run_invented(PRESETS["Expired card"])
        self.assertEqual(out["diagnosed"], "instrument_invalid")
        self.assertNotIn("lightgbm", sys.modules)


class ScreenFiguresMatchJsonTests(unittest.TestCase):
    """Every cell View 1/3/4 would paint is recomputed from the JSON that produced it."""

    def test_comparison_table_matches_json_and_published_floor(self):
        control = _json("control.json")
        self.assertEqual(control["n"], 187)
        self.assertEqual(
            caveat(control["n"]),
            "control n=187; rupee figures on this arm are noisy — headline is recovery-rate lift",
        )
        for label, filename in POLICY_FILES.items():
            policy = _json(filename)
            row = comparison_row(label, policy, control)
            published = PUBLISHED_ROWS[label]
            for key, expected in published.items():
                self.assertEqual(
                    row[key], expected,
                    f"{label}.{key}: on-screen {row[key]!r} != published {expected!r}",
                )
            self.assertEqual(row["Wasted"], policy["wasted_debits"])
            self.assertEqual(row["Impossible"], policy["impossible_debits"])
            self.assertEqual(row["Msgs"], policy["messages"])

    def test_headline_metrics_match_json(self):
        agent = _json("agent.json")
        b = _json("baseline_b.json")
        control = _json("control.json")
        at_risk = _treatment_at_risk()
        self.assertEqual(at_risk, 3_985_684)
        cards = headline_metrics(agent, b, control, at_risk)
        self.assertEqual(cards[0][0], "Revenue at risk")
        self.assertEqual(cards[0][1], "₹39.9L")
        self.assertEqual(cards[0][2], "")
        self.assertEqual(cards[1][0], "Recovered")
        self.assertEqual(cards[1][1], lakhs(agent["recovered_rupees"]))
        self.assertEqual(cards[1][1], "₹16.6L")
        self.assertEqual(cards[1][2], "41.6% of at-risk")
        self.assertEqual(cards[2][0], "Incremental lift")
        self.assertTrue(cards[2][1].endswith(" pp"))
        self.assertEqual(cards[2][1], comparison_row("Agent", agent, control)["Lift"] + " pp")
        self.assertEqual(cards[2][2], "vs no-intervention control")
        self.assertEqual(cards[3][0], "Wasted debits")
        self.assertEqual(cards[3][1], "0")
        self.assertEqual(cards[3][2], "−426 vs baseline")
        src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn(cards[0][1], src)
        self.assertNotIn(cards[1][1], src)
        self.assertNotIn(cards[1][2], src)
        self.assertNotIn(cards[2][1], src)

    def test_per_class_lift_matches_json(self):
        agent = _json("agent.json")
        b = _json("baseline_b.json")
        rows = per_class_lift_rows(agent, b)
        self.assertEqual(len(rows), len(agent["by_class"]))
        lifts = [r["lift"] for r in rows]
        self.assertEqual(lifts, sorted(lifts, key=lambda s: float(s), reverse=True))
        by_class = {r["class"]: r for r in rows}
        self.assertEqual(by_class["instrument_invalid"]["B"], "1.1%")
        self.assertEqual(by_class["instrument_invalid"]["agent"], "23.1%")
        self.assertEqual(by_class["mandate_failure"]["B"], "14.3%")
        self.assertEqual(by_class["mandate_failure"]["agent"], "42.9%")
        self.assertEqual(by_class["issuer_decline"]["B"], "11.6%")
        self.assertEqual(by_class["issuer_decline"]["agent"], "11.6%")
        self.assertEqual(by_class["issuer_decline"]["lift"], "+0.0")
        for cid, row in by_class.items():
            self.assertEqual(row["n"], agent["by_class"][cid]["n"])

    def test_view3_restraint_matches_json(self):
        agent = _json("agent.json")
        b = _json("baseline_b.json")
        self.assertEqual(agent["downtime_mandate_messages"], 0)
        self.assertEqual(b["downtime_mandate_messages"], 79)
        closes = agent["close_reasons"]
        self.assertEqual(closes["schedule_exhausted"], 439)
        self.assertEqual(closes["attempt_budget"], 347)
        self.assertEqual(closes["opted_out"], 27)
        self.assertEqual(closes["no_viable_action"], 0)
        gates = agent["gate_rejections"]
        self.assertEqual(gates["opted_out"], 27)
        self.assertEqual(gates.get("offer_beyond_policy", 0), 0)
        self.assertEqual(_json("baseline_c.json")["opted_out_triggered"], 329)
        trai = agent["trai"]
        self.assertEqual(trai["promotional"], 3)
        self.assertEqual(trai["shifted_or_suppressed"], 0)
        self.assertEqual(agent["pre_debit_notifications"], 448)
        self.assertEqual(agent["pre_debit_window_violations"], 189)
        trai = agent["trai"]
        self.assertEqual(trai["promotional"], 3)
        self.assertEqual(trai["shifted_or_suppressed"], 0)
        self.assertEqual(agent["pre_debit_notifications"], 448)
        self.assertEqual(agent["pre_debit_window_violations"], 189)

    def test_view4_efficiency_matches_json(self):
        agent = _json("agent.json")
        b = _json("baseline_b.json")
        ch = _json("agent_channel.json")
        self.assertEqual((b["wasted_debits"], b["impossible_debits"]), (426, 1094))
        self.assertEqual((agent["wasted_debits"], agent["impossible_debits"]), (0, 0))
        self.assertEqual(b["channel_mix"], {"sms": 813, "whatsapp": 0, "email": 0})
        self.assertEqual(agent["channel_mix"], {"sms": 372, "whatsapp": 463, "email": 114})
        self.assertEqual(ch["channel_mix"], {"sms": 58, "whatsapp": 839, "email": 31})
        self.assertEqual(b["cost_breakdown"]["total"], 2106.6)
        self.assertEqual(agent["cost_breakdown"]["total"], 1187.1)


class BookmarkRenderTests(unittest.TestCase):
    def test_unknown_id_is_empty(self):
        self.assertEqual(timeline_lines("PAY_NOPE", None, []), [])

    def test_every_bookmark_renders(self):
        payments = _json("payments.json")
        for pid in BOOKMARKS:
            header = payments.get(pid)
            rows = _audit_rows(pid)
            self.assertIsNotNone(header, f"{pid} missing from payments.json")
            self.assertGreater(len(rows), 0, f"{pid} has no audit rows")
            lines = timeline_lines(pid, header, rows)
            self.assertTrue(lines, f"{pid} rendered empty")
            self.assertTrue(lines[0].startswith(pid), f"{pid} missing header line")
            # Loop-bug guard: every audit row paints, not just the last.
            n_sent = sum(
                1 for r in rows
                if r["executed"] and r["chosen_action"] in MESSAGE_ACTIONS
            )
            self.assertEqual(
                sum(1 for line in lines if "  sent  " in line), n_sent,
                f"{pid} sent-line count drifted",
            )
            for r in rows:
                if r["executed"] and r["chosen_action"] in MESSAGE_ACTIONS:
                    continue
                if r["chosen_action"] == "pre_debit_notification":
                    self.assertTrue(
                        any("pre-debit" in line for line in lines),
                        f"{pid} dropped pre-debit notice",
                    )
                    continue
                self.assertTrue(
                    any(r["chosen_action"] in line for line in lines),
                    f"{pid} dropped action {r['chosen_action']}",
                )
            self.assertGreaterEqual(
                len(lines), 1 + len(rows),
                f"{pid} too short: {len(lines)} lines for {len(rows)} audit rows",
            )

    def test_pay_00210_instrument_update_pol011(self):
        blob = "\n".join(_chain("PAY_00210"))
        self.assertIn("instrument_invalid", blob)
        self.assertIn("POL-011", blob)
        self.assertIn("card_expired", blob)
        self.assertIn("RECOVERED", blob)
        self.assertEqual(sum(1 for line in _chain("PAY_00210") if "POL-011" in line), 2)

    def test_pay_00062_policy_never_proposes_debit(self):
        blob = "\n".join(_chain("PAY_00062"))
        self.assertIn("mandate_failure", blob)
        self.assertIn("mandate_creation_failed", blob)
        self.assertIn("POL-006", blob)
        self.assertIn("attempt_budget", blob)
        self.assertIn("RECOVERED", blob)
        self.assertNotIn("REJECTED", blob)
        self.assertNotIn("mandate_gate", blob)
        self.assertNotIn("retry_debit", blob)

    def test_pay_00071_opt_out_gate_visible(self):
        lines = _chain("PAY_00071")
        blob = "\n".join(lines)
        self.assertIn("insufficient_funds", blob)
        self.assertIn("proposed retry_debit", blob)
        self.assertIn("gate: REJECTED  opted_out", blob)
        self.assertIn("mark_uncollectible (opted_out)", blob)
        rejected = [r for r in _audit_rows("PAY_00071") if r["gate_result"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["gate_reason"], "opted_out")
        self.assertEqual(rejected[0]["executed"], 0)

    def test_pay_00026_downtime_wait_zero_messages(self):
        lines = _chain("PAY_00026")
        blob = "\n".join(lines)
        self.assertIn("technical_downtime", blob)
        self.assertIn("mandate: YES", blob)
        self.assertIn("wait_for_downtime_recovery", blob)
        self.assertIn("retry_debit", blob)
        self.assertIn("pre-debit notice", blob)
        self.assertIn("WINDOW VIOLATION", blob)
        self.assertNotIn(" sent  ", blob)
        self.assertIn("RECOVERED", blob)
        rows = _audit_rows("PAY_00026")
        notices = [r for r in rows if r["chosen_action"] == "pre_debit_notification"]
        self.assertGreaterEqual(len(notices), 1)
        self.assertGreaterEqual(sum(1 for line in lines if "executed" in line), 5)

    def test_pay_00011_stops_on_attempt_budget(self):
        lines = _chain("PAY_00011")
        blob = "\n".join(lines)
        self.assertIn("issuer_decline", blob)
        self.assertEqual(sum(1 for line in lines if "sent" in line), 2)
        self.assertIn("attempt_budget", blob)
        self.assertTrue(lines[-1].endswith("ok") or "attempt_budget" in lines[-1])
        self.assertIn("mark_uncollectible (attempt_budget)", blob)

    def test_pay_00002_pol002(self):
        blob = "\n".join(_chain("PAY_00002"))
        self.assertIn("₹46,535", blob)
        self.assertIn("insufficient_funds", blob)
        self.assertIn("POL-002", blob)

    def test_pay_hv_pol002_staged(self):
        blob = "\n".join(_chain("PAY_HV"))
        self.assertIn("₹40,000", blob)
        self.assertIn("POL-002", blob)
        self.assertIn("staged demo", blob)
        self.assertIn("5% waiver", blob)

    def test_pay_lv_pol001_staged(self):
        blob = "\n".join(_chain("PAY_LV"))
        self.assertIn("₹800", blob)
        self.assertIn("POL-001", blob)
        self.assertIn("staged demo", blob)
        self.assertIn("when funds are available", blob)


@unittest.skipUnless(
    importlib.util.find_spec("streamlit") is not None,
    "streamlit not installed",
)
class BookmarkClickTests(unittest.TestCase):
    """Headless click of each bookmark button — the demo path."""

    def test_eight_buttons_each_render(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=20)
        at.run()
        self.assertFalse(at.exception, msg=str(at.exception))
        markdown = "\n".join(m.value for m in at.markdown if isinstance(m.value, str))
        self.assertIn("showing 813 of 813 payments", markdown)
        labels = [b.label for b in at.button]
        for pid in BOOKMARKS:
            self.assertIn(pid, labels, f"button {pid} missing; have {labels}")

        for pid in BOOKMARKS:
            btn = next(b for b in at.button if b.label == pid)
            btn.click().run()
            self.assertFalse(at.exception, msg=f"{pid}: {at.exception}")
            markdown = "\n".join(m.value for m in at.markdown if isinstance(m.value, str))
            self.assertIn(
                pid, markdown,
                f"{pid} click did not render the chain. markdown={markdown[:400]!r}",
            )


    def test_class_drill_opens_chain_inline(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception, msg=str(at.exception))
        view = next(b for b in at.button if b.key == "drill_mandate_failure")
        view.click().run()
        self.assertFalse(at.exception, msg=str(at.exception))
        captions = "\n".join(c.value for c in at.caption if isinstance(c.value, str))
        self.assertIn("Batch → mandate_failure", captions)
        self.assertIn("← Back to all", [b.label for b in at.button])
        self.assertIn("open_PAY_00062", [b.key for b in at.button])
        opener = next(b for b in at.button if b.key == "open_PAY_00062")
        opener.click().run()
        self.assertFalse(at.exception, msg=str(at.exception))
        markdown = "\n".join(m.value for m in at.markdown if isinstance(m.value, str))
        self.assertIn("PAY_00062", markdown)
        self.assertIn("mandate_failure", markdown)


if __name__ == "__main__":
    unittest.main()
