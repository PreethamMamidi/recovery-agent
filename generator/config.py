"""
Loads the failure taxonomy.

This file is the SINGLE SOURCE OF TRUTH for failure classes. It is read by:
  - the generator   (to pick failure classes and set resolution probabilities)
  - the simulator   (to decide whether a problem resolves)
  - the agent       (Day 3, as its diagnosis lookup)

Same CSV, three consumers. Do not fork it.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "failure_classes.csv"

# ---- run-wide constants -----------------------------------------------------
MEASUREMENT_WINDOW_DAYS = 14   # "recovers without intervention" is meaningless
                               # without a deadline. Everything is measured
                               # inside this window.
CONTROL_ARM_FRACTION = 0.20
MANDATE_FRACTION = 0.70        # share of payments with an active mandate
RANDOM_SEED = 42


@dataclass(frozen=True)
class FailureClass:
    class_id: str
    bucket: str
    retry_viable: str
    optimal_delay: str
    ask_delay: str
    customer_action_required: str
    method_switch_helps: bool
    message_needed: str
    p_resolves: float          # P(the underlying problem goes away in-window)
    max_attempts: int
    gen_weight: float          # share of the generated batch
    reasoning: str

    @property
    def is_autonomous(self) -> bool:
        return self.bucket.startswith("autonomous")

    @property
    def needs_mandate(self) -> bool:
        return "if mandate" in self.bucket


def load_failure_classes(path: Path = CONFIG_PATH) -> dict[str, FailureClass]:
    out: dict[str, FailureClass] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            fc = FailureClass(
                class_id=row["class_id"],
                bucket=row["bucket"],
                retry_viable=row["retry_viable"],
                optimal_delay=row["optimal_delay"],
                ask_delay=row["ask_delay"],
                customer_action_required=row["customer_action_required"],
                method_switch_helps=row["method_switch_helps"].strip().lower().startswith("yes"),
                message_needed=row["message_needed"],
                p_resolves=float(row["p_resolves"]),
                max_attempts=int(row["max_attempts"]),
                gen_weight=float(row["gen_weight"]),
                reasoning=row["reasoning"],
            )
            out[fc.class_id] = fc

    total = sum(f.gen_weight for f in out.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"gen_weight must sum to 1.0, got {total:.4f}")
    return out


# ---------------------------------------------------------------------------
# All in-scope error reasons from the tagged Razorpay error list.
#
# The 121 tagged codes break down as:
#   36  out_of_scope  - API, config, payout, refund errors. A customer never
#                       experiences these while trying to pay, so the recovery
#                       pipeline never sees them. Deliberately not generated.
#   85  in scope      - but this includes codes listed in BOTH the payment and
#                       gateway sections of the docs (payment_failed,
#                       authentication_failed, invalid_vpa, bank_technical_error,
#                       server_error, payment_cancelled, payment_timed_out,
#                       debit_instrument_blocked, payment_risk_check_failed,
#                       transaction_daily_limit_exceeded,
#                       transaction_frequency_limit_exceeded, user_not_eligible).
#   74  distinct      - after deduping. That is what appears below.
#
# The agent diagnoses failure_class FROM these strings on Day 3, exactly as a
# production system would. Generating all 74 means every branch of the
# diagnosis layer gets exercised.
# ---------------------------------------------------------------------------
ERROR_REASONS = {
    "insufficient_funds": [
        "insufficient_funds",
        "funds_blocked_by_mandate",
    ],
    "technical_downtime": [
        "bank_technical_error",
        "bank_not_available",
        "bank_cutoff_in_progress",
        "gateway_technical_error",
        "issuer_technical_error",
        "invalid_response_from_gateway",
        "server_error",
        "request_timed_out",
        "verification_failed",
        "upi_app_technical_error",
        "psp_app_not_available",
        "psp_not_available",
        "authorisation_declined_by_psp",
        "payment_declined_due_to_high_traffic",
        "vpa_resolution_failed",
    ],
    "temporary_lockout": [
        "otp_attempts_exceeded",
        "pin_attempts_exceeded",
    ],
    "limit_exceeded": [
        "transaction_daily_limit_exceeded",
        "transaction_daily_count_exceeded",
        "transaction_frequency_limit_exceeded",
        "transaction_limit_exceeded",
        "credit_limit_exceeded",
        "emi_greater_than_max_amount",
    ],
    "session_expiry": [
        "payment_timed_out",
        "payment_session_expired",
        "payment_collect_request_expired",
        "collect_request_pending",
        "otp_expired",
        "payment_cancelled",
    ],
    "customer_input_error": [
        "authentication_failed",
        "incorrect_otp",
        "incorrect_cvv",
        "incorrect_pin",
        "incorrect_atm_pin",
        "incorrect_card_details",
        "incorrect_card_expiry_date",
        "incorrect_cardholder_name",
        "card_number_invalid",
        "invalid_vpa",
        "invalid_mobile_number",
        "mobile_number_invalid",
        "invalid_user_details",
        "bank_account_validation_failed",
    ],
    "issuer_decline": [
        "payment_failed",
        "payment_declined",
        "card_declined",
        "debit_declined",
        "debit_instrument_blocked",
        "payment_risk_check_failed",
        "credit_not_permitted",
        "credit_failed",
        "international_transaction_not_allowed",
        "transaction_on_vpa_restricted",
        "user_not_eligible",
    ],
    "instrument_invalid": [
        "card_expired",
        "card_not_enrolled",
        "card_type_invalid",
        "bank_account_invalid",
        "debit_instrument_inactive",
        "credit_limit_expired",
        "credit_limit_inactive",
        "credit_limit_not_approved",
        "pin_not_set",
        "user_not_registered_for_netbanking",
        "psp_not_registered",
        "psp_app_not_supported",
        "upi_autopay_not_supported_on_psp",
    ],
    "mandate_failure": [
        "mandate_creation_failed",
        "mandate_creation_declined",
        "mandate_creation_expired",
        "mandate_creation_timeout",
        "reqauth_mandate_not_acknowledged",
    ],
}

# Within-class frequency. Codes not listed default to weight 1.
# payment_failed is the generic no-code bucket and dominates in reality -
# it is also your vaguest diagnosis, which is worth demonstrating.
REASON_WEIGHTS = {
    "payment_failed": 6.0,
    "insufficient_funds": 8.0,
    "funds_blocked_by_mandate": 1.0,
    "incorrect_otp": 4.0,
    "authentication_failed": 3.0,
    "card_expired": 4.0,
    "bank_technical_error": 3.0,
    "bank_not_available": 3.0,
    "payment_timed_out": 3.0,
    "payment_cancelled": 2.5,
    "transaction_daily_limit_exceeded": 2.5,
    "mandate_creation_failed": 2.0,
}


def reason_weights_for(class_id: str) -> list[float]:
    return [REASON_WEIGHTS.get(r, 1.0) for r in ERROR_REASONS[class_id]]


# Structural limit codes: waiting never helps. Tomorrow's cap is the same cap.
STRUCTURAL_LIMIT_REASONS = {
    "transaction_limit_exceeded",
    "credit_limit_exceeded",
    "emi_greater_than_max_amount",
}

# Deliberate abandonment: re-pinging instantly reads as pestering.
DELIBERATE_ABANDON_REASONS = {"payment_cancelled"}

# Mandate refusal, as opposed to a timeout - it will not reverse in minutes.
MANDATE_REFUSAL_REASONS = {"mandate_creation_declined"}

# RBI Digital Payments – E-mandate Framework, 2026 (effective 21 Apr 2026):
# subsequent recurring transactions may skip AFA up to ₹15,000. Above that
# the customer is present (PIN/OTP), so a checkout-style input error is
# possible on a mandate debit. Insurance/MF/CC-bill sit at ₹1,00,000; we
# use the general threshold because this batch is not those categories.
AFA_THRESHOLD_INR = 15000

# Checkout-page session errors: nobody cancels a debit that runs while
# they are asleep. These cannot coexist with an active silent mandate.
CHECKOUT_SESSION_REASONS = {
    "payment_cancelled",
    "payment_timed_out",
    "payment_session_expired",
    "collect_request_pending",
}

METHODS = ["card", "upi", "netbanking"]
METHOD_WEIGHTS = [0.45, 0.40, 0.15]
