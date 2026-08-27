import ast
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent.actions import decision, validate
from agent.diagnose import REASON_TO_CLASS, assert_coverage, diagnose
from agent.guardrails import (
    MAX_MESSAGES_PER_WEEK,
    VALUE_ESCALATE_INR,
    RunContext,
    check,
)
from agent.loop import build_schedule
from agent.policy import next_payday_guess, plan
from eval.metrics import identity_mismatches, load_world, parse_bool, run_schedule
from generator.config import ERROR_REASONS, load_failure_classes
from simulator.response import (
    Action,
    latents_from_row,
    payment_hidden_from_row,
    payment_visible_from_row,
    respond,
)
from baselines.aggressive_dunning import schedule as schedule_c


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"


class DiagnoseTests(unittest.TestCase):
    def test_all_reasons_unique_and_covered(self):
        n = assert_coverage()
        listed = sum(len(v) for v in ERROR_REASONS.values())
        self.assertEqual(n, listed)
        self.assertEqual(len(REASON_TO_CLASS), 74)

    def test_round_trip(self):
        for class_id, reasons in ERROR_REASONS.items():
            for reason in reasons:
                self.assertEqual(diagnose(reason), class_id)

    def test_unknown_raises(self):
        with self.assertRaises(KeyError):
            diagnose("not_a_real_razorpay_reason")


class ActionSchemaTests(unittest.TestCase):
    def test_valid_actions(self):
        validate({"action": "retry_debit", "args": {"delay_hours": 24}})
        validate({"action": "schedule_for_payday", "args": {"target_date": "2026-08-07"}})
        validate({"action": "send_payment_link", "args": {"channel": "sms"}})
        validate({"action": "escalate", "args": {"reason": "value"}})

    def test_rejects_freeform_and_bad_args(self):
        with self.assertRaises(ValueError):
            validate({"action": "invent_new_tool", "args": {}})
        with self.assertRaises(ValueError):
            validate({"action": "retry_debit", "args": {"delay_hours": 1, "extra": 1}})
        with self.assertRaises(TypeError):
            validate({"action": "retry_debit", "args": {"delay_hours": "soon"}})
        with self.assertRaises(ValueError):
            validate({"action": "send_payment_link", "args": {"channel": "carrier-pigeon"}})


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.vis = SimpleNamespace(
            has_active_mandate=False, amount=500, error_reason="insufficient_funds",
        )
        self.cust = {"preferred_channel": "sms", "opted_out": False}
        self.ctx = RunContext(attempt_number=0, messages_this_week=0, opted_out=False)
        self.at = datetime(2026, 8, 10, 12, 0, 0)

    def test_mandate_gate(self):
        d = decision("retry_debit", delay_hours=24)
        g = check(self.vis, self.cust, "insufficient_funds", d, self.ctx, self.at)
        self.assertFalse(g.allowed)
        self.assertEqual(g.reason, "mandate_gate")
        self.assertEqual(g.executed.action, "send_payment_link")

    def test_opt_out_terminal_only(self):
        ctx = RunContext(0, 0, opted_out=True)
        d = decision("send_reminder", template_id="x", channel="sms")
        g = check(self.vis, self.cust, "insufficient_funds", d, ctx, self.at)
        self.assertEqual(g.reason, "opted_out")
        self.assertEqual(g.executed.action, "mark_uncollectible")

    def test_attempt_budget(self):
        ctx = RunContext(attempt_number=3, messages_this_week=0, opted_out=False)
        d = decision("retry_debit", delay_hours=24)
        vis = SimpleNamespace(has_active_mandate=True, amount=500, error_reason="x")
        g = check(vis, self.cust, "insufficient_funds", d, ctx, self.at)
        self.assertEqual(g.reason, "attempt_budget")

    def test_quiet_hours(self):
        vis = SimpleNamespace(has_active_mandate=True, amount=500, error_reason="x")
        d = decision("send_payment_link", channel="sms")
        night = datetime(2026, 8, 10, 23, 0, 0)
        g = check(vis, self.cust, "session_expiry", d, self.ctx, night)
        self.assertEqual(g.reason, "quiet_hours")

    def test_contact_cap(self):
        vis = SimpleNamespace(has_active_mandate=True, amount=500, error_reason="x")
        ctx = RunContext(0, messages_this_week=MAX_MESSAGES_PER_WEEK, opted_out=False)
        d = decision("send_payment_link", channel="sms")
        g = check(vis, self.cust, "session_expiry", d, ctx, self.at)
        self.assertEqual(g.reason, "contact_frequency")

    def test_cooling_off(self):
        vis = SimpleNamespace(has_active_mandate=True, amount=500, error_reason="x")
        ctx = RunContext(0, 0, False, promise_to_pay_until=self.at + timedelta(days=2))
        d = decision("send_reminder", template_id="x", channel="sms")
        g = check(vis, self.cust, "insufficient_funds", d, ctx, self.at)
        self.assertEqual(g.reason, "cooling_off")

    def test_value_threshold(self):
        vis = SimpleNamespace(has_active_mandate=True, amount=VALUE_ESCALATE_INR + 1,
                              error_reason="x")
        d = decision("retry_debit", delay_hours=6)
        g = check(vis, self.cust, "technical_downtime", d, self.ctx, self.at)
        self.assertEqual(g.reason, "value_threshold")
        self.assertEqual(g.executed.action, "escalate")


class SimulatorTests(unittest.TestCase):
    def test_identity_all_rows(self):
        world = load_world()
        self.assertEqual(identity_mismatches(world), 0)

    def test_failed_retry_does_not_suppress_natural(self):
        world = load_world()
        found = None
        for row in world["pay_vis"]:
            vis = payment_visible_from_row(row)
            gt = world["truth"][vis.payment_id]
            if vis.failure_class != "insufficient_funds":
                continue
            if not parse_bool(gt.get("would_have_recovered_naturally")):
                continue
            when = datetime.fromisoformat(gt["natural_recovery_date"])
            failed = datetime.fromisoformat(vis.failed_at)
            if when - failed > timedelta(hours=30):
                found = (vis, world, gt)
                break
        self.assertIsNotNone(found, "need an NSF row that recovers after 30h")
        vis, world, gt = found
        hid = payment_hidden_from_row(world["pay_hid"][vis.payment_id])
        lat = latents_from_row(world["latents"][vis.customer_id])
        fc = world["classes"][vis.failure_class]
        cust = world["customers"][vis.customer_id]
        actions = [Action("retry_debit", datetime.fromisoformat(vis.failed_at) + timedelta(hours=24),
                          {"delay_hours": 24})]
        out = respond(vis, hid, lat, fc, gt, actions, opted_out=parse_bool(cust["opted_out"]))
        self.assertTrue(out.recovered)
        self.assertEqual(out.source, "natural")

    def test_aggressive_dunning_triggers_opt_outs(self):
        world = load_world()
        totals = run_schedule(world, schedule_c, "C")
        self.assertGreater(totals.opted_out_triggered, 0)
        self.assertTrue(any(b["opt_outs"] > 0 for b in totals.by_class.values()))


class PolicyTests(unittest.TestCase):
    def test_payday_guess_is_1_or_7(self):
        failed = datetime(2026, 8, 3, 15, 0, 0)
        nxt = next_payday_guess(failed)
        self.assertEqual(nxt.day, 7)
        failed2 = datetime(2026, 8, 8, 10, 0, 0)
        nxt2 = next_payday_guess(failed2)
        self.assertEqual((nxt2.day, nxt2.month), (1, 9))

    def test_diagnose_not_visible_class_column(self):
        world = load_world()
        for row in world["pay_vis"]:
            vis = payment_visible_from_row(row)
            self.assertEqual(diagnose(vis.error_reason), vis.failure_class)

    def test_downtime_has_retry_and_no_message(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=True,
            error_reason="bank_technical_error",
        )
        steps = plan(vis, {"preferred_channel": "sms"}, "technical_downtime")
        names = [s.decision.action for s in steps]
        self.assertIn("wait_for_downtime_recovery", names)
        self.assertIn("retry_debit", names)
        self.assertNotIn("send_reminder", names)


class ImportBoundaryTests(unittest.TestCase):
    def test_agent_does_not_import_hidden_modules(self):
        forbidden = ("simulator", "generator.latents", "generator.natural_recovery")
        for path in AGENT_DIR.glob("*.py"):
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


class TraceTests(unittest.TestCase):
    def test_one_payment_has_diagnosed_gated_steps(self):
        world = load_world()
        row = next(r for r in world["pay_vis"]
                   if r["arm"] == "treatment" and r["has_active_mandate"] == "False"
                   and r["failure_class"] == "insufficient_funds")
        vis = payment_visible_from_row(row)
        cust = world["customers"][vis.customer_id]
        diagnosed, steps = build_schedule(vis, cust)
        self.assertEqual(diagnosed, "insufficient_funds")
        self.assertTrue(any(s.gate_result == "rejected" for s in steps))
        self.assertTrue(any(s.executed is not None for s in steps))


if __name__ == "__main__":
    unittest.main()
