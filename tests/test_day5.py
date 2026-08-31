"""Day 5: visible-only features, customer split, rule path unchanged."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from datetime import datetime

from agent.loop import build_schedule
from agent.ml_options import MlOptions
from eval.metrics import load_world
from generator.latents import CustomerLatents
from generator.entities import PaymentHidden
from model.features import FEATURES, hidden_columns
from model.labels import converting_step_labels
from model.split import split_customers
from simulator.response import payment_visible_from_row


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"
MODEL_DIR = ROOT / "model"


class FeatureLeakTests(unittest.TestCase):
    def test_model_features_are_visible_only(self):
        hidden = hidden_columns()
        overlap = set(FEATURES) & hidden
        self.assertFalse(overlap, overlap)

    def test_hidden_sets_cover_source_columns(self):
        latent = set(CustomerLatents.__dataclass_fields__) - {"customer_id"}
        pay_hid = set(PaymentHidden.__dataclass_fields__) - {"payment_id"}
        self.assertTrue(latent <= hidden_columns())
        self.assertTrue(pay_hid <= hidden_columns())


class CustomerSplitTests(unittest.TestCase):
    def test_no_customer_on_both_sides(self):
        ids = [f"C{i}" for i in range(50)] + [f"C{i}" for i in range(20)]
        train, val = split_customers(ids, train_frac=0.8, seed=0)
        self.assertFalse(train & val)
        self.assertEqual(train | val, set(ids))
        self.assertGreater(len(train), len(val))


class RulePathUnchangedTests(unittest.TestCase):
    def test_use_model_off_matches_default_schedule(self):
        world = load_world()
        n = 0
        for row in world["pay_vis"]:
            if row["arm"] != "treatment":
                continue
            vis = payment_visible_from_row(row)
            cust = world["customers"][vis.customer_id]
            a, sa = build_schedule(vis, cust)
            b, sb = build_schedule(vis, cust, ml=MlOptions())
            self.assertEqual(a, b)
            self.assertEqual(
                [(s.at, s.proposed, s.executed) for s in sa],
                [(s.at, s.proposed, s.executed) for s in sb],
            )
            n += 1
            if n >= 25:
                break
        self.assertGreater(n, 0)

    def test_unconditional_second_ask_is_now_the_rule_schedule(self):
        world = load_world()
        found = False
        for row in world["pay_vis"]:
            vis = payment_visible_from_row(row)
            if vis.arm != "treatment" or vis.failure_class != "customer_input_error":
                continue
            cust = world["customers"][vis.customer_id]
            _, rules = build_schedule(vis, cust)
            _, extra = build_schedule(
                vis, cust,
                ml=MlOptions(use_model=False, app="unconditional_second_ask"),
            )
            rule_msgs = [s for s in rules if s.executed and s.executed.action in {
                "send_payment_link", "request_instrument_update",
                "request_mandate_reauth", "send_reminder",
            }]
            extra_msgs = [s for s in extra if s.executed and s.executed.action in {
                "send_payment_link", "request_instrument_update",
                "request_mandate_reauth", "send_reminder",
            }]
            if len(rule_msgs) < 1:
                continue
            self.assertEqual(len(rule_msgs), 2, vis.payment_id)
            self.assertEqual(
                [(s.at, s.proposed, s.executed) for s in rules],
                [(s.at, s.proposed, s.executed) for s in extra],
            )
            self.assertGreaterEqual(rule_msgs[1].at, rule_msgs[0].at)
            found = True
            break
        self.assertTrue(found, "need a CIE treatment payment")

    def test_session_expiry_is_not_given_a_third_ask(self):
        world = load_world()
        found = False
        for row in world["pay_vis"]:
            vis = payment_visible_from_row(row)
            if vis.arm != "treatment" or vis.failure_class != "session_expiry":
                continue
            if vis.has_active_mandate:
                continue
            cust = world["customers"][vis.customer_id]
            _, steps = build_schedule(vis, cust)
            msgs = [s for s in steps if s.executed and s.executed.action in {
                "send_payment_link", "send_reminder",
            }]
            self.assertEqual(len(msgs), 2, vis.payment_id)
            found = True
            break
        self.assertTrue(found, "need a no-mandate session treatment payment")


class ConvertingLabelTests(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 8, 10, 10, 0, 0)
        self.t1 = datetime(2026, 8, 10, 16, 0, 0)

    def test_not_recovered_all_zero(self):
        self.assertEqual(
            converting_step_labels([self.t0, self.t1], False, None, "none"),
            [0, 0],
        )

    def test_natural_all_zero(self):
        self.assertEqual(
            converting_step_labels([self.t0, self.t1], True, self.t1, "natural"),
            [0, 0],
        )

    def test_first_action_credited(self):
        self.assertEqual(
            converting_step_labels([self.t0, self.t1], True, self.t0, "action"),
            [1, 0],
        )

    def test_second_action_credited(self):
        self.assertEqual(
            converting_step_labels([self.t0, self.t1], True, self.t1, "action"),
            [0, 1],
        )

    def test_same_timestamp_last_wins(self):
        self.assertEqual(
            converting_step_labels([self.t0, self.t0], True, self.t0, "action"),
            [0, 1],
        )


class ImportBoundaryTests(unittest.TestCase):
    def test_agent_and_model_inference_avoid_hidden(self):
        forbidden = ("simulator", "generator.latents", "generator.natural_recovery")
        paths = list(AGENT_DIR.glob("*.py")) + [
            MODEL_DIR / "features.py",
            MODEL_DIR / "score.py",
            MODEL_DIR / "split.py",
        ]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for f in forbidden:
                            self.assertFalse(
                                alias.name == f or alias.name.startswith(f + "."),
                                f"{path.name} imports {alias.name}",
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for f in forbidden:
                        self.assertFalse(
                            node.module == f or node.module.startswith(f + "."),
                            f"{path.name} imports from {node.module}",
                        )


if __name__ == "__main__":
    unittest.main()
