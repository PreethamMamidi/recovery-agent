"""DLT-style message generation. Copy only — never chooses the action.

Retrieval fail-closed: no policy chunk → no-offer template, never an
unbounded LLM call. reason_phrase is the only free slot; it is validated
against the retrieved policy before send.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agent.policy_index import (
    RetrievedPolicy,
    amount_band,
    customer_tier,
    retrieve,
)
from audit.log import connect, fetch_payment, log_decision, print_trace

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "config" / "demo_cases.json"
PAYMENTS_CSV = ROOT / "data" / "payments_visible.csv"
CUSTOMERS_CSV = ROOT / "data" / "customers_visible.csv"
ROGUE_PHRASE = "Enjoy 10% off today."
DEMO_LINE = (
    "the bound isn't a prompt instruction, it's a validator on the output — "
    "so it holds whether the generator is well-behaved or not."
)

PROHIBITED = (
    "account closure",
    "legal action",
    "credit score",
    "service suspension",
    "police",
    "blacklist",
    "salary",
)

OFFER_MARKERS = ("%", "discount", "waiver", "cashback", "off your", "incentive")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

TEMPLATES = {
    "payment_link_generic": (
        "Hi {name}, your payment of Rs {amount} to {merchant} didn't go through. "
        "{reason_phrase} Complete it here: {link}"
    ),
    "instrument_update": (
        "Hi {name}, we couldn't process Rs {amount} — {reason_phrase} "
        "Update your payment method: {link}"
    ),
    "downtime_followup": (
        "Hi {name}, your Rs {amount} payment to {merchant} failed due to a "
        "temporary bank issue. {reason_phrase} Try again: {link}"
    ),
    "mandate_reauth": (
        "Hi {name}, we need you to re-authorise payments to {merchant}. "
        "{reason_phrase} Continue here: {link}"
    ),
    "session_retry": (
        "Hi {name}, your Rs {amount} checkout to {merchant} expired. "
        "{reason_phrase} Complete it here: {link}"
    ),
    "lockout_wait": (
        "Hi {name}, your Rs {amount} payment to {merchant} could not be "
        "processed. {reason_phrase} Try again: {link}"
    ),
    "limit_exceeded": (
        "Hi {name}, your Rs {amount} payment to {merchant} hit a bank limit. "
        "{reason_phrase} Retry here: {link}"
    ),
    "no_offer": (
        "Hi {name}, your payment of Rs {amount} to {merchant} didn't go through. "
        "Please complete it here: {link}"
    ),
}

TEMPLATE_NO_OFFER = TEMPLATES["no_offer"]

TEMPLATE_FOR_CLASS = {
    "insufficient_funds": "payment_link_generic",
    "customer_input_error": "payment_link_generic",
    "issuer_decline": "payment_link_generic",
    "instrument_invalid": "instrument_update",
    "mandate_failure": "mandate_reauth",
    "technical_downtime": "downtime_followup",
    "temporary_lockout": "lockout_wait",
    "session_expiry": "session_retry",
    "limit_exceeded": "limit_exceeded",
}

STATIC_PHRASE = {
    "insufficient_funds": "Please retry when funds are available.",
    "customer_input_error": "Please retry with the correct details.",
    "issuer_decline": "Please try a different payment method.",
    "instrument_invalid": "Please add an updated payment method.",
    "mandate_failure": "Please re-authorise the mandate.",
    "technical_downtime": "The bank issue should now be clear.",
    "temporary_lockout": "Please retry after a short wait.",
    "session_expiry": "Please complete checkout again.",
    "limit_exceeded": "Please retry after the limit resets.",
}

MERCHANT = "your merchant"
MAX_PHRASE = 60
_COMPOSE_OVERRIDE: str | None = None


@dataclass
class GeneratedMessage:
    body: str
    template_id: str
    policy_id: str | None
    reason_phrase: str
    fallback: str
    proposed_phrase: str = ""
    rejections: list[str] = field(default_factory=list)


@contextmanager
def rogue_composer(phrase: str = ROGUE_PHRASE):
    """Demo/test helper: generator returns an unauthorised offer."""
    global _COMPOSE_OVERRIDE
    prev = _COMPOSE_OVERRIDE
    _COMPOSE_OVERRIDE = phrase
    try:
        yield
    finally:
        _COMPOSE_OVERRIDE = prev


def load_demo_cases(path: Path = CASES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_batch_payment(payment_id: str) -> tuple[_Pay, dict]:
    """Visible row from the canonical batch. No hidden files."""
    pay_row = None
    with PAYMENTS_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["payment_id"] == payment_id:
                pay_row = row
                break
    if pay_row is None:
        raise KeyError(f"{payment_id} is not in data/payments_visible.csv")
    cust_row = None
    with CUSTOMERS_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["customer_id"] == pay_row["customer_id"]:
                cust_row = row
                break
    if cust_row is None:
        raise KeyError(f"customer {pay_row['customer_id']} not in customers_visible.csv")
    pay = _Pay(
        pay_row["payment_id"],
        int(pay_row["amount"]),
        pay_row["failure_class"],
    )
    customer = {"lifetime_value": float(cust_row["lifetime_value"])}
    return pay, customer


def message_carries_offer(body: str) -> bool:
    """True if copy contains a discount/waiver — TRAI reclassifies as promotional."""
    if not body:
        return False
    text = body.lower()
    return (
        any(m in text for m in OFFER_MARKERS)
        or bool(_PERCENT_RE.search(body))
        or "%" in body
    )


def trai_category(body: str) -> str:
    """Service Implicit vs Promotional. Offer copy is the only reclassifier."""
    return "promotional" if message_carries_offer(body) else "service"


def no_offer_beyond(phrase: str, permitted: str) -> bool:
    """True iff phrase does not invent an offer past `permitted`.

    Two different failures, one function:
    - policy allows none → any %, discount, waiver, cashback is beyond
    - policy allows a 5% waiver → only 5.0 is within; 15% is not a substring of 5%
    """
    permitted_l = (permitted or "").lower()
    allows_waiver = "5%" in permitted_l and "waiver" in permitted_l
    text = phrase.lower()
    percents = [float(p) for p in _PERCENT_RE.findall(phrase)]
    has_offer = (
        any(m in text for m in OFFER_MARKERS)
        or bool(percents)
        or "%" in phrase
    )
    if not has_offer:
        return True
    if not allows_waiver:
        return False
    return bool(percents) and all(p == 5.0 for p in percents)


def validate_phrase(phrase: str, policy: RetrievedPolicy | None) -> str:
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("empty_phrase")
    phrase = " ".join(phrase.split()).strip()
    if len(phrase) > MAX_PHRASE:
        raise ValueError("phrase_too_long")
    low = phrase.lower()
    banned = list(PROHIBITED)
    if policy is not None:
        banned.extend(policy.never_say)
    for claim in banned:
        if claim and claim.lower() in low:
            raise ValueError(f"prohibited:{claim}")
    permitted = policy.permitted if policy is not None else "no offer permitted"
    if not no_offer_beyond(phrase, permitted):
        raise ValueError("offer_beyond_policy")
    return phrase


def _compose_phrase(policy: RetrievedPolicy, failure_class: str) -> str:
    """Grounded slot fill. Optional LLM would replace this call only."""
    if _COMPOSE_OVERRIDE is not None:
        return _COMPOSE_OVERRIDE
    permitted = policy.permitted.lower()
    if "5%" in permitted and "waiver" in permitted:
        return "A one-time 5% waiver needs review."
    return STATIC_PHRASE.get(failure_class, "Please complete the payment.")


def _audit_generation(conn: sqlite3.Connection | None, payment, failure_class: str,
                      msg: GeneratedMessage, *, timestamp: str) -> None:
    """Same decisions table as gate rejections. Copy only — does not choose the action."""
    if conn is None:
        return
    payment_id = str(
        getattr(payment, "payment_id", None)
        or (payment.get("payment_id") if isinstance(payment, dict) else "")
        or ""
    )
    args = {
        "template_id": msg.template_id,
        "policy_id": msg.policy_id,
        "reason_phrase": msg.proposed_phrase or msg.reason_phrase,
    }
    if msg.rejections:
        log_decision(
            conn,
            payment_id=payment_id,
            attempt_number=1,
            timestamp=timestamp,
            failure_class=failure_class,
            chosen_action="send_payment_link",
            action_args=args,
            gate_result="rejected",
            gate_reason=msg.rejections[0],
            executed=False,
        )
        fallback_args = {
            **args,
            "reason_phrase": msg.reason_phrase,
            "fallback": msg.fallback,
        }
        log_decision(
            conn,
            payment_id=payment_id,
            attempt_number=1,
            timestamp=timestamp,
            failure_class=failure_class,
            chosen_action="send_payment_link",
            action_args=fallback_args,
            gate_result="allowed",
            gate_reason="ok",
            executed=True,
        )
        return
    log_decision(
        conn,
        payment_id=payment_id,
        attempt_number=1,
        timestamp=timestamp,
        failure_class=failure_class,
        chosen_action="send_payment_link",
        action_args=args,
        gate_result="allowed",
        gate_reason="ok",
        executed=True,
    )


def _link(payment_id: str) -> str:
    pid = payment_id or "PAY"
    return f"https://pay.example/recover/{pid}"


def generate(payment, customer: dict, *,
             name: str = "there",
             merchant: str = MERCHANT,
             conn: sqlite3.Connection | None = None,
             timestamp: str = "2026-08-10T10:02:00") -> GeneratedMessage:
    """Render a DLT template. Does not choose whether to send."""
    failure_class = str(
        getattr(payment, "failure_class", None)
        or (payment.get("failure_class") if isinstance(payment, dict) else "")
        or ""
    )
    amount = getattr(payment, "amount", None)
    if amount is None and isinstance(payment, dict):
        amount = payment.get("amount", 0)
    payment_id = getattr(payment, "payment_id", None)
    if payment_id is None and isinstance(payment, dict):
        payment_id = payment.get("payment_id", "")
    ltv = 0.0
    if isinstance(customer, dict):
        ltv = float(customer.get("lifetime_value") or 0)
    band = amount_band(amount or 0)
    tier = customer_tier(ltv)
    policy = retrieve(failure_class, tier, band)
    slots = {
        "name": name,
        "amount": int(amount or 0),
        "merchant": merchant,
        "link": _link(str(payment_id or "")),
        "reason_phrase": "",
    }
    if not policy:
        body = TEMPLATE_NO_OFFER.format(**slots)
        msg = GeneratedMessage(
            body=body,
            template_id="no_offer",
            policy_id=None,
            reason_phrase="",
            fallback="no_policy",
        )
        _audit_generation(conn, payment, failure_class, msg, timestamp=timestamp)
        return msg

    tmpl_id = TEMPLATE_FOR_CLASS.get(failure_class, "payment_link_generic")
    template = TEMPLATES[tmpl_id]
    rejections: list[str] = []
    proposed = _compose_phrase(policy, failure_class)
    try:
        phrase = validate_phrase(proposed, policy)
        fallback = "generated"
    except ValueError as exc:
        rejections.append(str(exc))
        phrase = STATIC_PHRASE.get(failure_class, "Please complete the payment.")
        phrase = validate_phrase(phrase, policy)
        fallback = "static_after_reject"
    slots["reason_phrase"] = phrase
    body = template.format(**slots)
    msg = GeneratedMessage(
        body=body,
        template_id=tmpl_id,
        policy_id=policy.chunk_id,
        reason_phrase=phrase,
        fallback=fallback,
        proposed_phrase=proposed,
        rejections=rejections,
    )
    _audit_generation(conn, payment, failure_class, msg, timestamp=timestamp)
    return msg


class _Pay:
    def __init__(self, pid, amount, fclass):
        self.payment_id = pid
        self.amount = amount
        self.failure_class = fclass


def _case_pay(case: dict) -> tuple[_Pay, dict, str]:
    if case.get("from_batch"):
        pay, customer = load_batch_payment(case["id"])
        return pay, customer, case.get("name", "there")
    pay = _Pay(case["id"], case["amount"], case["failure_class"])
    customer = {"lifetime_value": case["lifetime_value"]}
    return pay, customer, case["name"]


def demo_bounded() -> int:
    for case in load_demo_cases()["bounded_offers"]:
        pay, customer, name = _case_pay(case)
        msg = generate(pay, customer, name=name)
        origin = "batch" if case.get("from_batch") else "staged"
        print(f"  {origin}  {pay.payment_id}  {msg.policy_id}  Rs {pay.amount} NSF")
        print(f"    {msg.body}")
    return 0


def demo_no_index(*, conn: sqlite3.Connection | None = None) -> int:
    """Retrieval fails. Fail closed: no-offer template, never an unbounded fill."""
    from agent.policy_index import broken_index

    cases = {c["id"]: c for c in load_demo_cases()["bounded_offers"]}
    low_c = cases["PAY_LV"]
    pay, customer, name = _case_pay(low_c)
    own = conn is None
    if conn is None:
        conn = connect()
    with broken_index():
        msg = generate(pay, customer, name=name, conn=conn)
    if own:
        conn.commit()
    print(f"  no-index        retrieve() → None")
    print(f"  fallback        {msg.fallback}  template={msg.template_id}")
    print(f"  policy_id       {msg.policy_id}")
    print(f"  sent            {msg.body}")
    print_trace(fetch_payment(conn, pay.payment_id), pay.payment_id)
    print("\n  retrieval failed closed — no-offer template, never an unbounded LLM call.")
    if own:
        conn.close()
    return 0


def demo_rogue(*, conn: sqlite3.Connection | None = None) -> int:
    cases = {c["id"]: c for c in load_demo_cases()["bounded_offers"]}
    low_c = cases["PAY_LV"]
    pay, customer, name = _case_pay(low_c)
    own = conn is None
    if conn is None:
        conn = connect()
    with rogue_composer(ROGUE_PHRASE):
        msg = generate(pay, customer, name=name, conn=conn)
    if own:
        conn.commit()
    print(f"  rogue composer  \"{msg.proposed_phrase}\"")
    print(f"  retrieved       {msg.policy_id}  ({low_c['failure_class']}, "
          f"under_5000, standard)")
    print(f"  validator       rejected  {msg.rejections[0] if msg.rejections else 'none'}")
    print(f"  sent            {msg.body}")
    print_trace(fetch_payment(conn, pay.payment_id), pay.payment_id)
    if msg.rejections:
        print(f"  rejected phrase {msg.proposed_phrase}")
        print(f"  fallback        {msg.fallback}  {msg.reason_phrase}")
    print(f"\n  {DEMO_LINE}")
    if own:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="DLT message demo. Does not score recovery.")
    ap.add_argument(
        "--demo", choices=("bounded", "rogue", "no-index"), default="bounded",
        help="bounded: POL-002 vs POL-001. rogue: unauthorised offer, validator holds. "
             "no-index: retrieval fails, no-offer fallback.",
    )
    args = ap.parse_args(argv)
    if args.demo == "rogue":
        return demo_rogue()
    if args.demo == "no-index":
        return demo_no_index()
    return demo_bounded()


if __name__ == "__main__":
    raise SystemExit(main())
