"""Filters, drill slice, and cumulative trend — dashboard layer only."""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from dashboard.explore import (  # noqa: E402
    AMOUNT_BANDS,
    apply_filters,
    build_treatment_frame,
    class_options,
    cumulative_trend,
    per_class_breakdown,
    showing_label,
    slice_summary,
)
from dashboard.render import headline_metrics, lakhs  # noqa: E402


def _frame():
    vis_path = ROOT / "data" / "payments_visible.csv"
    with vis_path.open(newline="", encoding="utf-8") as fh:
        vis = list(csv.DictReader(fh))
    payments = json.loads((ROOT / "results" / "payments.json").read_text(encoding="utf-8"))
    return build_treatment_frame(vis, payments)


def _all(df):
    return apply_filters(
        df, class_options(df), (AMOUNT_BANDS[0], AMOUNT_BANDS[-1]), "Any", "Any",
    )


class TreatmentFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = _frame()

    def test_treatment_only_is_813(self):
        self.assertEqual(len(self.df), 813)
        self.assertEqual(int(self.df["recovered"].sum()), 338)

    def test_default_filters_keep_everyone(self):
        out = _all(self.df)
        self.assertEqual(len(out), 813)
        self.assertEqual(showing_label(len(out), len(self.df)), "showing 813 of 813 payments")

    def test_class_filter_instrument_invalid(self):
        out = apply_filters(
            self.df, ("instrument_invalid",), (AMOUNT_BANDS[0], AMOUNT_BANDS[-1]), "Any", "Any",
        )
        self.assertEqual(len(out), 91)
        self.assertTrue((out["failure_class"] == "instrument_invalid").all())
        self.assertEqual(showing_label(len(out), 813), "showing 91 of 813 payments")

    def test_amount_band_low(self):
        out = apply_filters(
            self.df, tuple(class_options(self.df)), ("<₹1k", "<₹1k"), "Any", "Any",
        )
        self.assertEqual(len(out), 344)
        self.assertTrue((out["amount"] < 1000).all())

    def test_amount_band_high(self):
        out = apply_filters(
            self.df, tuple(class_options(self.df)), (">₹25k", ">₹25k"), "Any", "Any",
        )
        self.assertEqual(len(out), 46)
        self.assertTrue((out["amount"] > 25000).all())

    def test_mandate_yes(self):
        out = apply_filters(
            self.df, tuple(class_options(self.df)), (AMOUNT_BANDS[0], AMOUNT_BANDS[-1]), "Yes", "Any",
        )
        self.assertEqual(len(out), 390)
        self.assertTrue(out["has_active_mandate"].all())

    def test_outcome_recovered(self):
        out = apply_filters(
            self.df, tuple(class_options(self.df)), (AMOUNT_BANDS[0], AMOUNT_BANDS[-1]), "Any", "Recovered",
        )
        self.assertEqual(len(out), 338)
        self.assertTrue(out["recovered"].all())

    def test_empty_classes_is_empty(self):
        out = apply_filters(
            self.df, (), (AMOUNT_BANDS[0], AMOUNT_BANDS[-1]), "Any", "Any",
        )
        self.assertEqual(len(out), 0)
        self.assertEqual(showing_label(0, 813), "showing 0 of 813 payments")

    def test_slice_summary_matches_frame(self):
        s = slice_summary(_all(self.df))
        self.assertEqual(s["n"], 813)
        self.assertEqual(s["recovered_n"], 338)
        self.assertAlmostEqual(s["at_risk"], 3_985_684)
        self.assertEqual(lakhs(s["at_risk"]), "₹39.9L")

    def test_per_class_sums_to_slice(self):
        rows = per_class_breakdown(_all(self.df))
        self.assertEqual(sum(r["n"] for r in rows), 813)
        by_class = {r["class"]: r["n"] for r in rows}
        self.assertEqual(by_class["insufficient_funds"], 198)
        self.assertEqual(by_class["mandate_failure"], 14)

    def test_headlines_stay_unfiltered(self):
        agent = json.loads((ROOT / "results" / "agent.json").read_text(encoding="utf-8"))
        b = json.loads((ROOT / "results" / "baseline_b.json").read_text(encoding="utf-8"))
        control = json.loads((ROOT / "results" / "control.json").read_text(encoding="utf-8"))
        cards = headline_metrics(agent, b, control, 3_985_684)
        self.assertEqual(cards[0][1], "₹39.9L")
        src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertIn("showing_label", src)
        self.assertNotIn("showing 96 of 813", src)


class CumulativeTrendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = _frame()
        cls.trend = cumulative_trend(cls.df)

    def test_axis_spans_failure_month_and_late_recoveries(self):
        self.assertEqual(self.trend.index.min().date().isoformat(), "2026-08-01")
        self.assertEqual(self.trend.index.max().date().isoformat(), "2026-09-07")

    def test_at_risk_finishes_on_last_failure_then_holds(self):
        last_fail = self.df["failed_at"].max().normalize()
        final = float(self.df["amount"].sum())
        self.assertAlmostEqual(float(self.trend.loc[last_fail, "at risk"]), final)
        self.assertAlmostEqual(float(self.trend["at risk"].iloc[-1]), final)

    def test_recovered_keeps_climbing_after_failures_stop(self):
        last_fail = self.df["failed_at"].max().normalize()
        rec_at_last_fail = float(self.trend.loc[last_fail, "recovered"])
        rec_final = float(self.trend["recovered"].iloc[-1])
        self.assertGreater(rec_final, rec_at_last_fail)
        self.assertAlmostEqual(rec_final, float(self.df["recovered_amount"].sum()))

    def test_app_labels_failure_vs_recovery_axis(self):
        src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertIn("At-risk is booked on the failure date. Recovered is booked on the recovery date", src)
        self.assertNotIn("2026-08-01", src)
        self.assertNotIn("2026-09-07", src)


if __name__ == "__main__":
    unittest.main()
