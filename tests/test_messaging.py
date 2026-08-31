"""Bounded offers: retrieval fail-closed, DLT templates, no decision leak."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from agent.loop import build_schedule
from agent.messaging import (
    DEMO_LINE,
    PROHIBITED,
    ROGUE_PHRASE,
    generate,
    load_batch_payment,
    load_demo_cases,
    main as messaging_main,
    message_carries_offer,
    no_offer_beyond,
    rogue_composer,
    trai_category,
    validate_phrase,
)
from agent.policy_index import (
    amount_band,
    broken_index,
    customer_tier,
    load_chunks,
    retrieve,
)
from audit.log import fetch_payment, reset


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"


def _pay(amount, failure_class="insufficient_funds", pid="PAY_T"):
    return SimpleNamespace(
        payment_id=pid, amount=amount, failure_class=failure_class,
        error_reason="insufficient_funds" if failure_class == "insufficient_funds"
        else "card_expired" if failure_class == "instrument_invalid"
        else "incorrect_otp",
        failed_at="2026-08-10T10:00:00",
        has_active_mandate=False,
    )


class CorpusTests(unittest.TestCase):
    def test_chunks_load_and_most_forbid_offers(self):
        chunks = load_chunks()
        ids = {c.id for c in chunks}
        self.assertIn("POL-001", ids)
        self.assertIn("POL-002", ids)
        self.assertIn("POL-007", ids)
        self.assertIn("POL-011", ids)
        authority = [c for c in chunks if c.kind in {"discount_authority", "instrument_update"}]
        no_offer = [c for c in authority if "no" in c.permitted.lower()]
        self.assertGreater(len(no_offer), len(authority) / 2)

    def test_amount_bands_and_tiers(self):
        self.assertEqual(amount_band(800), "under_5000")
        self.assertEqual(amount_band(40000), "over_25000")
        self.assertEqual(customer_tier(800), "standard")
        self.assertEqual(customer_tier(25000), "gold")


class RetrievalTests(unittest.TestCase):
    def test_small_nsf_is_pol001_no_discount(self):
        pol = retrieve("insufficient_funds", "standard", "under_5000")
        self.assertIsNotNone(pol)
        self.assertEqual(pol.chunk_id, "POL-001")
        self.assertIn("no discount", pol.permitted.lower())

    def test_high_nsf_is_pol002(self):
        pol = retrieve("insufficient_funds", "gold", "over_25000")
        self.assertIsNotNone(pol)
        self.assertEqual(pol.chunk_id, "POL-002")
        self.assertIn("5%", pol.permitted)

    def test_instrument_is_pol011(self):
        pol = retrieve("instrument_invalid", "standard", "under_5000")
        self.assertIsNotNone(pol)
        self.assertEqual(pol.chunk_id, "POL-011")
        self.assertIn("no incentive", pol.permitted.lower())

    def test_broken_index_returns_none(self):
        with broken_index():
            self.assertIsNone(retrieve("insufficient_funds", "gold", "over_25000"))


class GenerationTests(unittest.TestCase):
    def test_generation_never_offers_without_policy(self):
        with broken_index():
            msg = generate(_pay(40000), {"lifetime_value": 25000}, name="Priya")
            self.assertIsNone(msg.policy_id)
            self.assertEqual(msg.template_id, "no_offer")
            self.assertNotIn("%", msg.body)
            self.assertNotIn("discount", msg.body.lower())
            self.assertNotIn("waiver", msg.body.lower())

    def test_small_nsf_has_no_offer(self):
        msg = generate(_pay(800), {"lifetime_value": 800}, name="Amit")
        self.assertEqual(msg.policy_id, "POL-001")
        self.assertNotIn("%", msg.body)
        self.assertNotIn("discount", msg.body.lower())
        self.assertNotIn("waiver", msg.body.lower())

    def test_pol001_rejects_any_invented_offer(self):
        """Zero permitted: 5% is as illegal as 30%. Word-only offers too."""
        pol = retrieve("insufficient_funds", "standard", "under_5000")
        self.assertEqual(pol.chunk_id, "POL-001")
        self.assertIn("no discount", pol.permitted.lower())
        for phrase in (
            "Take 5% off this invoice.",
            "Take 30% off this invoice.",
            "Enjoy a small discount today.",
            "A one-time waiver is available.",
            "Cashback if you pay now.",
        ):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_phrase(phrase, pol)
                self.assertFalse(no_offer_beyond(phrase, pol.permitted))

    def test_pol001_invented_phrase_falls_back_to_static(self):
        with rogue_composer(ROGUE_PHRASE):
            msg = generate(_pay(800), {"lifetime_value": 800}, name="Amit")
        self.assertEqual(msg.policy_id, "POL-001")
        self.assertEqual(msg.proposed_phrase, ROGUE_PHRASE)
        self.assertEqual(msg.fallback, "static_after_reject")
        self.assertIn("offer_beyond_policy", msg.rejections)
        self.assertNotIn("%", msg.body)
        self.assertNotIn("discount", msg.body.lower())
        self.assertNotIn("waiver", msg.body.lower())

    def test_high_nsf_may_mention_authorised_waiver(self):
        msg = generate(_pay(40000), {"lifetime_value": 25000}, name="Priya")
        self.assertEqual(msg.policy_id, "POL-002")
        self.assertIn("5%", msg.reason_phrase)
        self.assertIn("review", msg.reason_phrase.lower())
        pol = retrieve("insufficient_funds", "gold", "over_25000")
        validate_phrase("A one-time 5% waiver needs review.", pol)
        with self.assertRaises(ValueError):
            validate_phrase("A one-time 15% waiver needs review.", pol)

    def test_instrument_update_has_no_incentive(self):
        msg = generate(
            _pay(1200, "instrument_invalid"),
            {"lifetime_value": 1000},
        )
        self.assertEqual(msg.policy_id, "POL-011")
        self.assertNotIn("%", msg.body)
        self.assertNotIn("discount", msg.body.lower())

    def test_validate_rejects_prohibited_and_over_offer(self):
        pol = retrieve("insufficient_funds", "standard", "under_5000")
        with self.assertRaises(ValueError):
            validate_phrase("Pay or face legal action today.", pol)
        with self.assertRaises(ValueError):
            validate_phrase("Take 30% off this invoice.", pol)
        with self.assertRaises(ValueError):
            validate_phrase("x" * 61, pol)
        ok = validate_phrase("Please retry when funds are available.", pol)
        self.assertLessEqual(len(ok), 60)
        for claim in PROHIBITED[:4]:
            self.assertNotIn(claim, ok.lower())

    def test_no_offer_beyond_zero_vs_capped(self):
        none = "no discount; payment link only"
        capped = "one-time 5% waiver, requires review flag"
        self.assertFalse(no_offer_beyond("Enjoy 5% off.", none))
        self.assertFalse(no_offer_beyond("Enjoy 30% off.", none))
        self.assertFalse(no_offer_beyond("Enjoy a discount.", none))
        self.assertTrue(no_offer_beyond("A one-time 5% waiver needs review.", capped))
        self.assertFalse(no_offer_beyond("A one-time 15% waiver needs review.", capped))
        self.assertFalse(no_offer_beyond("Take 30% off this invoice.", capped))


class DemoAndAuditTests(unittest.TestCase):
    def test_bookmarks_include_day6_and_bounded_ids(self):
        cases = load_demo_cases()
        stories = {c["story"]: c["id"] for c in cases["day6_stories"]}
        self.assertEqual(set(stories), {
            "clean_recovery", "gate_fallback", "opt_out_gate",
            "restraint", "give_up", "high_value",
        })
        self.assertEqual(stories["clean_recovery"], "PAY_00210")
        self.assertEqual(stories["gate_fallback"], "PAY_00062")
        self.assertEqual(stories["opt_out_gate"], "PAY_00071")
        self.assertEqual(stories["restraint"], "PAY_00026")
        self.assertEqual(stories["give_up"], "PAY_00011")
        self.assertEqual(stories["high_value"], "PAY_00002")
        offers = [c["id"] for c in cases["bounded_offers"]]
        self.assertEqual(offers, ["PAY_HV", "PAY_LV", "PAY_00002"])
        vis = (ROOT / "data" / "payments_visible.csv").read_text(encoding="utf-8")
        for pid in stories.values():
            self.assertIn(f"{pid},", vis)
        self.assertIn("PAY_00002,", vis)

    def test_batch_high_nsf_retrieves_pol002(self):
        pay, customer = load_batch_payment("PAY_00002")
        self.assertGreater(pay.amount, 25000)
        self.assertEqual(pay.failure_class, "insufficient_funds")
        msg = generate(pay, customer)
        self.assertEqual(msg.policy_id, "POL-002")
        self.assertIn("5%", msg.reason_phrase)
        pol = retrieve(
            pay.failure_class,
            customer_tier(customer["lifetime_value"]),
            amount_band(pay.amount),
        )
        self.assertEqual(pol.chunk_id, "POL-002")

    def test_rogue_demo_writes_rejection_to_audit_log(self):
        tmp = Path(tempfile.mkdtemp()) / "log.db"
        conn = reset(tmp)
        pay = _pay(800, pid="PAY_LV")
        with rogue_composer(ROGUE_PHRASE):
            msg = generate(pay, {"lifetime_value": 800}, name="Amit", conn=conn)
        conn.commit()
        rows = fetch_payment(conn, "PAY_LV")
        conn.close()
        self.assertEqual(msg.policy_id, "POL-001")
        self.assertEqual(msg.fallback, "static_after_reject")
        self.assertNotIn("%", msg.body)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["gate_result"], "rejected")
        self.assertEqual(rows[0]["gate_reason"], "offer_beyond_policy")
        self.assertEqual(rows[0]["executed"], 0)
        self.assertEqual(rows[0]["chosen_action"], "send_payment_link")
        self.assertEqual(rows[1]["gate_result"], "allowed")
        self.assertEqual(rows[1]["executed"], 1)

    def test_cli_bounded_and_rogue(self):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = messaging_main(["--demo", "bounded"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("POL-002", out)
        self.assertIn("POL-001", out)
        self.assertIn("40000", out)
        self.assertIn("800", out)
        self.assertIn("PAY_00002", out)
        self.assertIn("46535", out)

        tmp = Path(tempfile.mkdtemp()) / "log.db"
        conn = reset(tmp)
        buf = StringIO()
        with redirect_stdout(buf):
            from agent.messaging import demo_rogue
            rc = demo_rogue(conn=conn)
        conn.commit()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("offer_beyond_policy", out)
        self.assertIn("PAY_LV", out)
        self.assertIn(DEMO_LINE, out)
        sent = next(ln for ln in out.splitlines() if "sent" in ln.lower())
        self.assertNotIn("%", sent)
        rows = fetch_payment(conn, "PAY_LV")
        conn.close()
        self.assertEqual(rows[0]["gate_reason"], "offer_beyond_policy")

    def test_cli_no_index_fail_closed(self):
        tmp = Path(tempfile.mkdtemp()) / "log.db"
        conn = reset(tmp)
        buf = StringIO()
        with redirect_stdout(buf):
            from agent.messaging import demo_no_index
            rc = demo_no_index(conn=conn)
        conn.commit()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("no_offer", out)
        self.assertIn("None", out)
        self.assertNotIn("%", out)
        rows = fetch_payment(conn, "PAY_LV")
        conn.close()
        self.assertTrue(rows)
        args = json.loads(rows[-1]["action_args"])
        self.assertIsNone(args.get("policy_id"))
        self.assertEqual(args.get("template_id"), "no_offer")

    def test_offer_copy_is_promotional(self):
        self.assertEqual(trai_category("Please retry when funds are available."), "service")
        self.assertEqual(
            trai_category("A one-time 5% waiver needs review."), "promotional",
        )
        self.assertTrue(message_carries_offer("Enjoy 10% off today."))
        self.assertFalse(message_carries_offer("Please complete the payment."))


class DecisionIsolationTests(unittest.TestCase):
    def test_generation_does_not_change_the_schedule(self):
        vis = SimpleNamespace(
            failed_at="2026-08-10T10:00:00", has_active_mandate=False,
            error_reason="incorrect_otp", amount=800, payment_id="PAY_ISO",
            failure_class="customer_input_error",
        )
        cust = {"preferred_channel": "sms", "opted_out": False, "lifetime_value": 800}
        _, a = build_schedule(vis, cust)
        _ = generate(vis, cust)
        _, b = build_schedule(vis, cust)
        self.assertEqual(
            [(s.at, s.proposed, s.executed) for s in a],
            [(s.at, s.proposed, s.executed) for s in b],
        )

    def test_messaging_avoids_hidden_modules(self):
        forbidden = ("simulator", "generator.latents", "generator.natural_recovery")
        path = AGENT_DIR / "messaging.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for f in forbidden:
                        self.assertFalse(alias.name.startswith(f))
            elif isinstance(node, ast.ImportFrom) and node.module:
                for f in forbidden:
                    self.assertFalse(node.module.startswith(f))


if __name__ == "__main__":
    unittest.main()
