"""Sandbox: diagnose → policy → gate → simulator on one invented payment."""

from __future__ import annotations

import unittest

from agent.actions import MESSAGE_ACTIONS
from agent.diagnose import diagnose
from dashboard.sandbox import PRESETS, run_invented


class PresetStoryTests(unittest.TestCase):
    def test_expired_card_asks_instead_of_debit(self):
        out = run_invented(PRESETS["Expired card"])
        self.assertEqual(out["diagnosed"], "instrument_invalid")
        executed = [r["executed"] for r in out["plan"] if r["executed"] != "—"]
        self.assertIn("request_instrument_update", executed)
        self.assertNotIn("retry_debit", executed)
        self.assertEqual(out["diagnosis"][0]["error_reason"], "card_expired")

    def test_downtime_with_mandate_sends_nothing(self):
        out = run_invented(PRESETS["Bank downtime"])
        self.assertEqual(out["diagnosed"], "technical_downtime")
        executed = [r["executed"] for r in out["plan"] if r["executed"] != "—"]
        self.assertIn("wait_for_downtime_recovery", executed)
        self.assertTrue(any(a == "retry_debit" for a in executed))
        self.assertFalse(any(a in MESSAGE_ACTIONS for a in executed))

    def test_no_mandate_does_not_debit(self):
        out = run_invented(PRESETS["No mandate"])
        self.assertEqual(out["diagnosed"], "insufficient_funds")
        executed = [r["executed"] for r in out["plan"] if r["executed"] != "—"]
        self.assertNotIn("retry_debit", executed)
        self.assertIn("send_payment_link", executed)

    def test_hidden_state_includes_salary_day(self):
        out = run_invented(PRESETS["No mandate"])
        self.assertIn("salary_day", out["hidden"])
        self.assertIsInstance(out["hidden"]["salary_day"], int)
        again = run_invented(PRESETS["No mandate"])
        self.assertEqual(out["hidden"]["salary_day"], again["hidden"]["salary_day"])

    def test_outcome_has_natural_counterfactual(self):
        out = run_invented(PRESETS["Expired card"])
        self.assertIn("natural", out["outcome"])
        self.assertIn("recovered", out["outcome"])
        self.assertIn("p_natural", out["outcome"])


class DecisionCardTests(unittest.TestCase):
    def _rails(self, card):
        return {g["text"]: g["ok"] for g in card["guardrails"]}

    def test_expired_card_asks_and_shows_passed_rails(self):
        card = run_invented(PRESETS["Expired card"])["card"]
        self.assertIn("PAY_SANDBOX", card["headline"])
        self.assertIn("instrument_invalid", card["headline"])
        self.assertIn("mandate: yes", card["headline"])
        self.assertEqual(card["diagnosis"]["error_reason"], "card_expired")
        self.assertEqual(card["diagnosis"]["failure class"], "instrument_invalid")
        self.assertEqual(card["diagnosis"]["retry viable"], "never")
        self.assertEqual(card["decision"]["action"], "request_instrument_update")
        self.assertIn("retry cannot fix", card["decision"]["why"])
        rails = self._rails(card)
        self.assertTrue(rails["mandate present"])
        self.assertTrue(rails["attempt 1 of 3"])
        self.assertTrue(rails["not opted out"])
        self.assertTrue(rails["under ₹25,000 escalate threshold"])
        self.assertIn("agent", card["outcome"])
        self.assertIn("no_intervention", card["outcome"])

    def test_downtime_decides_to_wait(self):
        card = run_invented(PRESETS["Bank downtime"])["card"]
        self.assertEqual(card["decision"]["action"], "wait_for_downtime_recovery")
        self.assertIn("wait", card["decision"]["why"])
        self.assertTrue(self._rails(card)["mandate present"])

    def test_no_mandate_asks_instead_of_debiting(self):
        card = run_invented(PRESETS["No mandate"])["card"]
        self.assertEqual(card["decision"]["action"], "send_payment_link")
        self.assertIn("no mandate", card["decision"]["why"])
        rails = self._rails(card)
        self.assertTrue(rails["no mandate — asked instead of debiting"])
        self.assertNotIn("mandate present", rails)

    def test_nsf_with_mandate_is_payday_anchored(self):
        card = run_invented({
            "failure_class": "insufficient_funds",
            "error_reason": "insufficient_funds",
            "amount": 2400,
            "has_active_mandate": True,
            "tenure_months": 12,
            "past_payment_count": 8,
            "opted_out": False,
        })["card"]
        self.assertIn("mandate: yes", card["headline"])
        self.assertEqual(card["diagnosis"]["bucket"], "autonomous (if mandate)")
        self.assertEqual(card["diagnosis"]["retry viable"], "after delay")
        self.assertEqual(card["decision"]["action"], "retry_debit")
        self.assertEqual(card["decision"]["target"], "1 Sep")
        self.assertEqual(
            card["decision"]["why"],
            "payday-anchored retry; agent cannot see salary_day",
        )
        self.assertTrue(self._rails(card)["mandate present"])


class DiagnoseFromReasonTests(unittest.TestCase):
    def test_reason_not_class_label_drives_diagnosis(self):
        self.assertEqual(diagnose("card_expired"), "instrument_invalid")
        self.assertEqual(diagnose("insufficient_funds"), "insufficient_funds")
        self.assertEqual(diagnose("bank_not_available"), "technical_downtime")


if __name__ == "__main__":
    unittest.main()
