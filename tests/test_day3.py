import ast
import shutil
import tempfile
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
from agent.policy import nearest_paydays, next_payday_guess, plan
from eval.metrics import identity_mismatches, load_world, parse_bool, run_schedule
from generator.config import ERROR_REASONS, load_failure_classes
from generator.generate import generate
from generator.latents import CustomerLatents
from generator.presence import AFA_THRESHOLD, PRESENCE, _validate
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

    def test_value_threshold_does_not_freeze(self):
        vis = SimpleNamespace(has_active_mandate=True, amount=VALUE_ESCALATE_INR + 1,
                              error_reason="x")
        d = decision("retry_debit", delay_hours=6)
        g = check(vis, self.cust, "technical_downtime", d, self.ctx, self.at)
        self.assertTrue(g.allowed)
        self.assertEqual(g.executed.action, "retry_debit")

    def test_opted_out_high_value_escalates(self):
        ctx = RunContext(0, 0, opted_out=True)
        vis = SimpleNamespace(has_active_mandate=True, amount=VALUE_ESCALATE_INR + 1,
                              error_reason="x")
        d = decision("retry_debit", delay_hours=6)
        g = check(vis, self.cust, "technical_downtime", d, ctx, self.at)
        self.assertEqual(g.reason, "opted_out_high_value")
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


    def test_no_mandate_debit_is_impossible_not_wasted(self):
        """Debit with no stored authorisation is never sent: not recovered,
        impossible_debits > 0, wasted_debits and debit_attempts unchanged."""
        vis = SimpleNamespace(
            payment_id="PAY_NO_MANDATE",
            customer_id="CUST_NO_MANDATE",
            amount=500,
            method="upi",
            failed_at="2026-08-10T10:00:00",
            error_reason="insufficient_funds",
            failure_class="insufficient_funds",
            has_active_mandate=False,
            attempt_number=1,
            invoice_due_date="2026-08-12",
            arm="treatment",
        )
        hid = SimpleNamespace(
            payment_id="PAY_NO_MANDATE",
            downtime_ends_at=None,
            lockout_ends_at=None,
            limit_resets_at=None,
            is_structural_limit=False,
            is_deliberate_abandon=False,
        )
        lat = CustomerLatents(
            customer_id="CUST_NO_MANDATE",
            salary_day=1,
            true_intent_to_pay=0.9,
            reattempt_propensity=0.5,
            annoyance_threshold=4,
            resp_sms=0.4,
            resp_whatsapp=0.5,
            resp_email=0.1,
            tech_savviness=0.5,
        )
        fc = load_failure_classes()["insufficient_funds"]
        truth = {
            "would_have_recovered_naturally": False,
            "natural_recovery_date": "",
        }
        failed_at = datetime.fromisoformat(vis.failed_at)
        actions = [
            Action("retry_debit", failed_at + timedelta(hours=24),
                   {"delay_hours": 24}),
            Action("retry_debit", failed_at + timedelta(hours=72),
                   {"delay_hours": 72}),
        ]
        out = respond(vis, hid, lat, fc, truth, actions, opted_out=False)
        self.assertFalse(out.recovered)
        self.assertGreater(out.impossible_debits, 0)
        self.assertEqual(out.impossible_debits, 2)
        self.assertEqual(out.wasted_debits, 0)
        self.assertEqual(out.debit_attempts, 0)

    def test_no_mandate_never_viable_debit_does_not_count_as_waste(self):
        vis = SimpleNamespace(
            payment_id="PAY_DEAD_CARD",
            customer_id="CUST_DEAD_CARD",
            amount=800,
            method="card",
            failed_at="2026-08-10T10:00:00",
            error_reason="card_expired",
            failure_class="instrument_invalid",
            has_active_mandate=False,
            attempt_number=1,
            invoice_due_date="2026-08-12",
            arm="treatment",
        )
        hid = SimpleNamespace(
            payment_id="PAY_DEAD_CARD",
            downtime_ends_at=None,
            lockout_ends_at=None,
            limit_resets_at=None,
            is_structural_limit=False,
            is_deliberate_abandon=False,
        )
        lat = CustomerLatents(
            customer_id="CUST_DEAD_CARD",
            salary_day=7, true_intent_to_pay=0.8, reattempt_propensity=0.4,
            annoyance_threshold=3, resp_sms=0.3, resp_whatsapp=0.4,
            resp_email=0.1, tech_savviness=0.6,
        )
        fc = load_failure_classes()["instrument_invalid"]
        truth = {"would_have_recovered_naturally": False, "natural_recovery_date": ""}
        failed_at = datetime.fromisoformat(vis.failed_at)
        actions = [Action("retry_debit", failed_at + timedelta(hours=24), {})]
        out = respond(vis, hid, lat, fc, truth, actions, opted_out=False)
        self.assertFalse(out.recovered)
        self.assertEqual(out.impossible_debits, 1)
        self.assertEqual(out.wasted_debits, 0)
        self.assertEqual(out.debit_attempts, 0)


class PolicyTests(unittest.TestCase):
    def test_payday_guess_is_1_or_7(self):
        failed = datetime(2026, 8, 3, 15, 0, 0)
        nxt = next_payday_guess(failed)
        self.assertEqual(nxt.day, 7)
        failed2 = datetime(2026, 8, 16, 10, 0, 0)
        nxt2 = next_payday_guess(failed2)
        self.assertEqual((nxt2.day, nxt2.month), (1, 9))

    def test_payday_ladder_proximity_not_calendar_order(self):
        failed = datetime(2026, 8, 3, 15, 0, 0)
        window = failed + timedelta(days=14)
        days = [d.day for d in nearest_paydays(failed, window)]
        self.assertEqual(days[0], 7)
        self.assertIn(15, days)
        self.assertNotEqual(days[0], 1)

    def test_payday_ladder_truncates_to_window(self):
        failed = datetime(2026, 8, 20, 12, 0, 0)
        window = failed + timedelta(days=14)
        found = nearest_paydays(failed, window)
        self.assertTrue(all(failed < d <= window for d in found))
        self.assertLessEqual(len(found), 3)
        self.assertTrue(found, "20th should still catch the next 1st")
        self.assertEqual(found[0].day, 1)
        self.assertEqual(found[0].month, 9)

    def test_payday_ladder_fits_attempt_budget(self):
        vis = SimpleNamespace(
            failed_at="2026-08-01T08:00:00", has_active_mandate=True,
            error_reason="insufficient_funds", amount=500,
        )
        fc = load_failure_classes()["insufficient_funds"]
        planned = plan(vis, {"preferred_channel": "sms"}, "insufficient_funds")
        debits = [s for s in planned if s.decision.action in {"retry_debit", "schedule_for_payday"}]
        self.assertLessEqual(len(debits), fc.max_attempts)
        self.assertGreaterEqual(len(debits), 1)
        diagnosed, steps = build_schedule(
            vis, {"preferred_channel": "sms", "opted_out": False})
        self.assertEqual(diagnosed, "insufficient_funds")
        executed = [s.executed.action for s in steps if s.executed]
        self.assertEqual(executed.count("retry_debit"), len(debits))
        self.assertNotIn("escalate", executed)

    def test_diagnose_not_visible_class_column(self):
        world = load_world()
        for row in world["pay_vis"]:
            vis = payment_visible_from_row(row)
            self.assertEqual(diagnose(vis.error_reason), vis.failure_class)

    def test_downtime_has_retry_and_no_message(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=True,
            error_reason="bank_technical_error", amount=500,
        )
        steps = plan(vis, {"preferred_channel": "sms"}, "technical_downtime")
        names = [s.decision.action for s in steps]
        self.assertIn("wait_for_downtime_recovery", names)
        self.assertIn("retry_debit", names)
        self.assertNotIn("send_reminder", names)
        hours = [s.decision.args["delay_hours"]
                 for s in steps if s.decision.action == "retry_debit"]
        self.assertGreaterEqual(max(hours), 24)
        self.assertGreaterEqual(len(hours), 3)
        fc = load_failure_classes()["technical_downtime"]
        self.assertLessEqual(len(hours), fc.max_attempts)
        diagnosed, gated = build_schedule(
            vis, {"preferred_channel": "sms", "opted_out": False})
        self.assertEqual(diagnosed, "technical_downtime")
        executed = [s.executed.action for s in gated if s.executed]
        self.assertEqual(executed.count("retry_debit"), len(hours))
        self.assertNotIn("escalate", executed)

    def test_lockout_exponential_backoff(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=True,
            error_reason="otp_attempts_exceeded", amount=500,
        )
        fc = load_failure_classes()["temporary_lockout"]
        steps = plan(vis, {"preferred_channel": "sms"}, "temporary_lockout")
        hours = [s.decision.args["delay_hours"]
                 for s in steps if s.decision.action == "retry_debit"]
        self.assertEqual(len(hours), fc.max_attempts)
        self.assertGreater(hours[-1], 24)
        for earlier, later in zip(hours, hours[1:]):
            self.assertGreaterEqual(later, earlier * 3)
        diagnosed, gated = build_schedule(
            vis, {"preferred_channel": "sms", "opted_out": False})
        self.assertEqual(diagnosed, "temporary_lockout")
        executed = [s.executed.action for s in gated if s.executed]
        self.assertEqual(executed.count("retry_debit"), len(hours))
        self.assertNotIn("escalate", executed)

    def test_daily_limit_two_resets(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=True,
            error_reason="transaction_daily_limit_exceeded", amount=500,
        )
        fc = load_failure_classes()["limit_exceeded"]
        steps = plan(vis, {"preferred_channel": "sms"}, "limit_exceeded")
        debits = [s for s in steps if s.decision.action == "retry_debit"]
        self.assertEqual(len(debits), fc.max_attempts)
        self.assertEqual([s.at.hour for s in debits], [0, 0])
        self.assertEqual([s.at.minute for s in debits], [30, 30])
        self.assertEqual((debits[1].at.date() - debits[0].at.date()).days, 1)
        diagnosed, gated = build_schedule(
            vis, {"preferred_channel": "sms", "opted_out": False})
        executed = [s.executed.action for s in gated if s.executed]
        self.assertEqual(executed.count("retry_debit"), len(debits))
        self.assertNotIn("escalate", executed)

    def test_session_immediate_then_followup(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=True,
            error_reason="payment_timed_out", amount=500,
        )
        fc = load_failure_classes()["session_expiry"]
        steps = plan(vis, {"preferred_channel": "sms"}, "session_expiry")
        hours = [s.decision.args["delay_hours"]
                 for s in steps if s.decision.action == "retry_debit"]
        self.assertEqual(len(hours), fc.max_attempts)
        self.assertEqual(hours[0], 0)
        self.assertEqual(hours[-1], 6)
        diagnosed, gated = build_schedule(
            vis, {"preferred_channel": "sms", "opted_out": False})
        executed = [s.executed.action for s in gated if s.executed]
        self.assertEqual(executed.count("retry_debit"), len(hours))
        self.assertNotIn("escalate", executed)

    def test_customer_action_immediate_then_followup(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=False,
            error_reason="incorrect_otp", amount=500,
        )
        fc = load_failure_classes()["customer_input_error"]
        steps = plan(vis, {"preferred_channel": "sms"}, "customer_input_error")
        self.assertEqual(len(steps), 2)
        self.assertLessEqual(len(steps), fc.max_attempts)
        self.assertEqual(steps[0].decision.action, "send_payment_link")
        self.assertEqual(steps[1].decision.action, "send_payment_link")
        self.assertEqual(steps[1].at, steps[0].at + timedelta(hours=6))
        diagnosed, gated = build_schedule(
            vis, {"preferred_channel": "sms", "opted_out": False})
        self.assertEqual(diagnosed, "customer_input_error")
        executed = [s.executed.action for s in gated if s.executed]
        self.assertEqual(executed.count("send_payment_link"), 2)

    def test_high_value_is_flagged_not_abandoned(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=True,
            error_reason="bank_technical_error", amount=VALUE_ESCALATE_INR + 1,
        )
        diagnosed, steps = build_schedule(vis, {"preferred_channel": "sms", "opted_out": False})
        self.assertEqual(diagnosed, "technical_downtime")
        executed = [s.executed.action for s in steps if s.executed is not None]
        self.assertIn("retry_debit", executed)
        self.assertNotIn("escalate", executed)
        self.assertTrue(all(s.flagged_for_review for s in steps))
        self.assertGreater(len(steps), 0)

    def test_downtime_no_mandate_waits_then_links(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=False,
            error_reason="bank_technical_error", amount=500,
        )
        steps = plan(vis, {"preferred_channel": "sms"}, "technical_downtime")
        names = [s.decision.action for s in steps]
        self.assertEqual(names[0], "wait_for_downtime_recovery")
        self.assertIn("send_payment_link", names)
        self.assertNotIn("retry_debit", names)
        wait_at = steps[0].at
        link_at = next(s.at for s in steps if s.decision.action == "send_payment_link")
        self.assertGreaterEqual(link_at, wait_at)

    def test_no_class_is_silent_without_mandate(self):
        failed = "2026-08-03T10:00:00"
        reasons = {
            "insufficient_funds": "insufficient_funds",
            "technical_downtime": "bank_technical_error",
            "temporary_lockout": "otp_attempts_exceeded",
            "limit_exceeded": "transaction_daily_limit_exceeded",
            "session_expiry": "payment_timed_out",
            "customer_input_error": "incorrect_otp",
            "instrument_invalid": "card_expired",
            "issuer_decline": "payment_declined",
            "mandate_failure": "mandate_creation_failed",
        }
        for class_id, reason in reasons.items():
            vis = SimpleNamespace(
                failed_at=failed, has_active_mandate=False,
                error_reason=reason, amount=500,
            )
            diagnosed, steps = build_schedule(
                vis, {"preferred_channel": "sms", "opted_out": False})
            self.assertEqual(diagnosed, class_id, reason)
            levers = [
                s.executed.action for s in steps
                if s.executed is not None
                and s.executed.action not in {"wait_for_downtime_recovery"}
            ]
            self.assertTrue(
                levers,
                f"{class_id} produced no recovery lever without a mandate: "
                f"{[s.proposed.action for s in steps]}",
            )


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
        found = None
        for r in world["pay_vis"]:
            if r["arm"] != "treatment" or r["has_active_mandate"] != "False":
                continue
            if r["failure_class"] != "insufficient_funds":
                continue
            vis = payment_visible_from_row(r)
            cust = world["customers"][vis.customer_id]
            diagnosed, steps = build_schedule(vis, cust)
            if any(s.gate_result == "rejected" for s in steps) and any(
                    s.executed is not None for s in steps):
                found = (diagnosed, steps)
                break
        self.assertIsNotNone(found, "need a no-mandate NSF payment that hits the gate")
        diagnosed, steps = found
        self.assertEqual(diagnosed, "insufficient_funds")
        self.assertTrue(any(s.gate_result == "rejected" for s in steps))
        self.assertTrue(any(s.executed is not None for s in steps))


class PresenceTests(unittest.TestCase):
    """Reason-level mandate presence. Guard against class-level miss."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="presence_"))
        generate(1000, seed=7, out_dir=cls._tmp)
        cls.world = load_world(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_presence_covers_all_reasons(self):
        _validate()
        all_reasons = {r for v in ERROR_REASONS.values() for r in v}
        self.assertEqual(set(PRESENCE), all_reasons)
        missing_key = next(iter(PRESENCE))
        saved = PRESENCE.pop(missing_key)
        try:
            with self.assertRaises(ValueError) as ctx:
                _validate()
            self.assertIn("presence map drift", str(ctx.exception))
        finally:
            PRESENCE[missing_key] = saved
        _validate()

    def test_never_reasons_have_no_mandate(self):
        never = {r for r, p in PRESENCE.items() if p == "NEVER"}
        n_never = 0
        for row in self.world["pay_vis"]:
            if row["error_reason"] not in never:
                continue
            n_never += 1
            self.assertFalse(
                parse_bool(row["has_active_mandate"]),
                row["error_reason"],
            )
        self.assertGreater(n_never, 0)

    def test_afa_below_threshold_no_mandate(self):
        afa = {r for r, p in PRESENCE.items() if p == "AFA"}
        n_below = 0
        for row in self.world["pay_vis"]:
            if row["error_reason"] not in afa:
                continue
            if int(row["amount"]) >= AFA_THRESHOLD:
                continue
            n_below += 1
            self.assertFalse(parse_bool(row["has_active_mandate"]), row["payment_id"])
        self.assertGreater(n_below, 0)

    def test_afa_above_threshold_some(self):
        afa = {r for r, p in PRESENCE.items() if p == "AFA"}
        above = [
            row for row in self.world["pay_vis"]
            if row["error_reason"] in afa and int(row["amount"]) >= AFA_THRESHOLD
        ]
        with_mandate = sum(1 for r in above if parse_bool(r["has_active_mandate"]))
        self.assertGreater(len(above), 0, "need some AFA-code payments >= threshold")
        self.assertGreater(with_mandate, 0, "some above-threshold AFA codes should have a mandate")


if __name__ == "__main__":
    unittest.main()
