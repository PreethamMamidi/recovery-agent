"""Merchant-data join for webhook entities.

A Razorpay payment.failed payload gives amount, method, error fields, and
notes. It does not give tenure_months, past_payment_count, or
has_active_mandate. Those live on the merchant's own customer/payment
record. This module is that boundary: look up by notes.internal_payment_id,
fall back to conservative defaults if the id is absent or unknown.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PAYMENTS = ROOT / "data" / "payments_visible.csv"
CUSTOMERS = ROOT / "data" / "customers_visible.csv"

# Conservative when the merchant record is missing: cannot debit without
# proof of a mandate; short tenure; one prior payment; do not assume opt-out.
DEFAULTS = {
    "has_active_mandate": False,
    "tenure_months": 1,
    "past_payment_count": 1,
    "opted_out": False,
    "preferred_channel": "sms",
    "lifetime_value": 0,
    "attempt_number": 1,
}


def _bool(v) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def _index_csv(path: Path, key: str) -> dict[str, dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return {row[key]: row for row in csv.DictReader(fh)}


def notes_of(entity: dict) -> dict:
    raw = entity.get("notes")
    if isinstance(raw, dict):
        return raw
    return {}


def _amount_rupees(entity: dict) -> int:
    """Razorpay amounts are smallest currency unit. Agent vis.amount is rupees."""
    amount = int(entity.get("amount") or 0)
    currency = str(entity.get("currency") or "INR").upper()
    if currency == "INR":
        return amount // 100
    return amount


def _failed_at(entity: dict, event: dict) -> str:
    ts = entity.get("created_at") or event.get("created_at")
    if ts is None:
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")


def lookup_merchant(internal_payment_id: str) -> tuple[dict | None, dict | None]:
    """Return (payment_row, customer_row) or (None, None)."""
    payments = _index_csv(PAYMENTS, "payment_id")
    pay = payments.get(internal_payment_id)
    if pay is None:
        return None, None
    customers = _index_csv(CUSTOMERS, "customer_id")
    return pay, customers.get(pay["customer_id"])


def build_visible_from_webhook(entity: dict, event: dict) -> tuple[SimpleNamespace, dict]:
    notes = notes_of(entity)
    internal_id = notes.get("internal_payment_id")
    pay, cust = lookup_merchant(internal_id) if internal_id else (None, None)

    failed_at = _failed_at(entity, event)
    amount = _amount_rupees(entity)
    method = entity.get("method") or (pay["method"] if pay else "upi")
    razorpay_id = entity.get("id") or "pay_unknown"

    if pay is not None:
        vis = SimpleNamespace(
            payment_id=pay["payment_id"],
            customer_id=pay["customer_id"],
            amount=amount,
            method=method,
            failed_at=failed_at,
            error_reason=entity.get("error_reason"),
            failure_class=pay["failure_class"],
            has_active_mandate=_bool(pay["has_active_mandate"]),
            attempt_number=int(pay.get("attempt_number") or 1),
            invoice_due_date=pay.get("invoice_due_date") or failed_at[:10],
            arm=pay.get("arm") or "treatment",
            tenure_months=int((cust or {}).get("tenure_months") or DEFAULTS["tenure_months"]),
            past_payment_count=int(
                (cust or {}).get("past_payment_count") or DEFAULTS["past_payment_count"]
            ),
        )
        customer = {
            "opted_out": _bool((cust or {}).get("opted_out", False)),
            "preferred_channel": (cust or {}).get(
                "preferred_channel", DEFAULTS["preferred_channel"]
            ),
            "lifetime_value": float(
                (cust or {}).get("lifetime_value") or DEFAULTS["lifetime_value"]
            ),
            "tenure_months": vis.tenure_months,
            "past_payment_count": vis.past_payment_count,
        }
        return vis, customer

    vis = SimpleNamespace(
        payment_id=internal_id or razorpay_id,
        customer_id="",
        amount=amount,
        method=method,
        failed_at=failed_at,
        error_reason=entity.get("error_reason"),
        failure_class="",
        has_active_mandate=DEFAULTS["has_active_mandate"],
        attempt_number=DEFAULTS["attempt_number"],
        invoice_due_date=failed_at[:10],
        arm="treatment",
        tenure_months=DEFAULTS["tenure_months"],
        past_payment_count=DEFAULTS["past_payment_count"],
    )
    customer = {
        "opted_out": DEFAULTS["opted_out"],
        "preferred_channel": DEFAULTS["preferred_channel"],
        "lifetime_value": DEFAULTS["lifetime_value"],
        "tenure_months": DEFAULTS["tenure_months"],
        "past_payment_count": DEFAULTS["past_payment_count"],
    }
    return vis, customer
