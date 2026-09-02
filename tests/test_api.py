"""API layer: metrics, audit chain, webhook signature, idempotency."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "razorpay_payment_failed.json"
RESULTS = ROOT / "results"
AUDIT = RESULTS / "audit.db"
SECRET = "whsec_test"

os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api.security import sign, verify_signature  # noqa: E402
from api.store import unknown_reasons  # noqa: E402
from dashboard.render import timeline_lines  # noqa: E402


def _body() -> bytes:
    return FIXTURE.read_bytes()


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _signed(body: bytes, secret: str = SECRET) -> dict:
    return {"X-Razorpay-Signature": sign(body, secret)}


class SignatureTests(unittest.TestCase):
    def test_valid_signature_accepted(self):
        raw = b'{"event":"payment.failed"}'
        self.assertTrue(verify_signature(raw, sign(raw, SECRET), SECRET))

    def test_tampered_body_rejected_at_compare(self):
        raw = b'{"event":"payment.failed"}'
        sig = sign(raw, SECRET)
        tampered = raw[:-1] + bytes([raw[-1] ^ 0x01])
        self.assertFalse(verify_signature(tampered, sig, SECRET))


class MetricsAndChainTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_metrics_returns_agent_json(self):
        expected = json.loads((RESULTS / "agent.json").read_text(encoding="utf-8"))
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["recovery_rate"], expected["recovery_rate"])
        self.assertEqual(body["n"], expected["n"])
        self.assertEqual(body["wasted_debits"], 0)

    def test_payments_pay_00071_two_row_chain(self):
        resp = self.client.get("/payments/PAY_00071")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["payment_id"], "PAY_00071")
        rows = body["decisions"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["chosen_action"], "retry_debit")
        self.assertEqual(rows[0]["gate_result"], "rejected")
        self.assertEqual(rows[0]["gate_reason"], "opted_out")
        self.assertEqual(rows[1]["chosen_action"], "mark_uncollectible")

    def test_payments_unknown_404(self):
        resp = self.client.get("/payments/PAY_DOES_NOT_EXIST")
        self.assertEqual(resp.status_code, 404)

    def test_audit_chain_matches_dashboard_render(self):
        pid = "PAY_00071"
        api_rows = self.client.get(f"/payments/{pid}").json()["decisions"]
        conn = sqlite3.connect(f"file:{AUDIT}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """SELECT timestamp, failure_class, chosen_action, action_args,
                      gate_result, gate_reason, executed, outcome, flagged_for_review
               FROM decisions WHERE payment_id = ? ORDER BY id""",
            (pid,),
        )
        db_rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        header = json.loads(
            (RESULTS / "payments.json").read_text(encoding="utf-8")
        )[pid]
        self.assertEqual(
            timeline_lines(pid, header, api_rows),
            timeline_lines(pid, header, db_rows),
        )


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["API_EVENTS_DB"] = str(Path(self._tmp.name) / "events.db")
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET
        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("API_EVENTS_DB", None)

    def _post(self, body: bytes, headers: dict | None = None):
        return self.client.post("/webhook", content=body, headers=headers or _signed(body))

    def test_stage1_payload_produces_decision(self):
        body = _body()
        resp = self._post(body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "accepted")
        self.assertEqual(data["failure_class"], "insufficient_funds")
        self.assertEqual(data["internal_payment_id"], "PAY_00001")
        self.assertTrue(data["actions"])
        first = data["actions"][0]
        self.assertIn(first["action"], {"retry_debit", "schedule_for_payday"})
        self.assertIn(first["gate"], {"allowed", "rejected"})
        self.assertIn("reason", first)
        self.assertIn("args", first)

    def test_duplicate_event_returns_duplicate(self):
        body = _body()
        first = self._post(body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "accepted")
        second = self._post(body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(second.json()["event_id"], first.json()["event_id"])

    def test_tampered_signature_rejected(self):
        body = _body()
        tampered = body[:-1] + bytes([body[-1] ^ 0x01])
        resp = self._post(tampered, headers=_signed(body))
        self.assertEqual(resp.status_code, 401)

    def test_missing_signature_header_rejected(self):
        resp = self.client.post("/webhook", content=_body())
        self.assertEqual(resp.status_code, 401)

    def test_null_error_reason_returns_unknown_not_500(self):
        event = _payload()
        event["payload"]["payment"]["entity"]["error_reason"] = None
        event["payload"]["payment"]["entity"]["id"] = "pay_null_reason"
        body = json.dumps(event).encode()
        resp = self._post(body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["failure_class"], "unknown")
        self.assertEqual(data["actions"], [])
        self.assertEqual(data["status"], "accepted")

    def test_unknown_reason_is_logged(self):
        event = _payload()
        event["payload"]["payment"]["entity"]["error_reason"] = "not_in_taxonomy"
        event["payload"]["payment"]["entity"]["id"] = "pay_unknown_reason"
        body = json.dumps(event).encode()
        resp = self._post(body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["failure_class"], "unknown")
        logged = unknown_reasons(resp.json()["event_id"])
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["reason"], "not_in_taxonomy")


if __name__ == "__main__":
    unittest.main()
