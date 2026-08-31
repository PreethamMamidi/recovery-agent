"""
Reason-level mandate presence.

Could a human have been at a screen when this failure occurred?

    NEVER  - checkout / setup only; a silent mandate debit cannot produce it
    AFA    - only if the debit is above the RBI AFA threshold (customer is prompted)
    ANY    - fires on mandate debits and checkout alike

Sourced from mandate-presence-audit.xlsx columns A (reason) and C (should_be).
_validate() at import stops a new ERROR_REASONS code from silently defaulting.
"""

from __future__ import annotations

import random

from .config import MANDATE_FRACTION

# RBI Digital Payments – E-mandate Framework, 2026 (issued 21 Apr 2026,
# effective immediately). Subsequent recurring transactions skip AFA up to
# ₹15,000. Insurance premiums, mutual-fund subscriptions, and credit-card
# bill payments sit at ₹1,00,000. This batch is a general subscription /
# recurring merchant, so the ₹15,000 threshold applies.
# Checked 31 August 2026.
AFA_THRESHOLD = 15_000

# reason -> NEVER | AFA | ANY. 74 rows, pasted from the audit sheet.
PRESENCE = {
    "insufficient_funds": "ANY",
    "funds_blocked_by_mandate": "ANY",
    "bank_technical_error": "ANY",
    "bank_not_available": "ANY",
    "bank_cutoff_in_progress": "ANY",
    "gateway_technical_error": "ANY",
    "issuer_technical_error": "ANY",
    "invalid_response_from_gateway": "ANY",
    "server_error": "ANY",
    "request_timed_out": "ANY",
    "verification_failed": "ANY",
    "upi_app_technical_error": "ANY",
    "psp_app_not_available": "ANY",
    "psp_not_available": "ANY",
    "authorisation_declined_by_psp": "ANY",
    "payment_declined_due_to_high_traffic": "ANY",
    "vpa_resolution_failed": "ANY",
    "otp_attempts_exceeded": "AFA",
    "pin_attempts_exceeded": "AFA",
    "transaction_daily_limit_exceeded": "ANY",
    "transaction_daily_count_exceeded": "ANY",
    "transaction_frequency_limit_exceeded": "ANY",
    "transaction_limit_exceeded": "ANY",
    "credit_limit_exceeded": "ANY",
    "emi_greater_than_max_amount": "NEVER",
    "payment_timed_out": "NEVER",
    "payment_session_expired": "NEVER",
    "payment_collect_request_expired": "NEVER",
    "collect_request_pending": "NEVER",
    "otp_expired": "AFA",
    "payment_cancelled": "NEVER",
    "authentication_failed": "AFA",
    "incorrect_otp": "AFA",
    "incorrect_pin": "AFA",
    "incorrect_atm_pin": "AFA",
    "incorrect_cvv": "NEVER",
    "incorrect_card_details": "NEVER",
    "incorrect_card_expiry_date": "NEVER",
    "incorrect_cardholder_name": "NEVER",
    "card_number_invalid": "NEVER",
    "invalid_vpa": "NEVER",
    "invalid_mobile_number": "NEVER",
    "mobile_number_invalid": "NEVER",
    "invalid_user_details": "NEVER",
    "bank_account_validation_failed": "NEVER",
    "payment_failed": "ANY",
    "payment_declined": "ANY",
    "card_declined": "ANY",
    "debit_declined": "ANY",
    "debit_instrument_blocked": "ANY",
    "payment_risk_check_failed": "ANY",
    "credit_not_permitted": "ANY",
    "credit_failed": "ANY",
    "international_transaction_not_allowed": "ANY",
    "transaction_on_vpa_restricted": "ANY",
    "user_not_eligible": "ANY",
    "card_expired": "ANY",
    "card_not_enrolled": "ANY",
    "card_type_invalid": "ANY",
    "bank_account_invalid": "ANY",
    "debit_instrument_inactive": "ANY",
    "credit_limit_expired": "ANY",
    "credit_limit_inactive": "ANY",
    "credit_limit_not_approved": "ANY",
    "pin_not_set": "NEVER",
    "user_not_registered_for_netbanking": "ANY",
    "psp_not_registered": "ANY",
    "psp_app_not_supported": "NEVER",
    "upi_autopay_not_supported_on_psp": "ANY",
    "mandate_creation_failed": "NEVER",
    "mandate_creation_declined": "NEVER",
    "mandate_creation_expired": "NEVER",
    "mandate_creation_timeout": "NEVER",
    "reqauth_mandate_not_acknowledged": "NEVER",
}


def _validate() -> None:
    from .config import ERROR_REASONS
    all_reasons = {r for v in ERROR_REASONS.values() for r in v}
    missing = all_reasons - PRESENCE.keys()
    extra = PRESENCE.keys() - all_reasons
    if missing or extra:
        raise ValueError(f"presence map drift: missing={missing} extra={extra}")


_validate()          # runs at import


def assign_mandate(reason: str, amount: int, rng: random.Random) -> bool:
    p = PRESENCE[reason]              # KeyError is intentional
    if p == "NEVER":
        return False
    if p == "AFA":
        return amount >= AFA_THRESHOLD and rng.random() < 0.7
    return rng.random() < MANDATE_FRACTION
