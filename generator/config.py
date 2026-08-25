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


# Error reasons per class, used to give payments a realistic-looking code.
# Trimmed from the 121 tagged codes; the agent diagnoses class from these.
ERROR_REASONS = {
    "insufficient_funds":   ["insufficient_funds", "funds_blocked_by_mandate"],
    "technical_downtime":   ["bank_technical_error", "bank_not_available", "gateway_technical_error",
                             "psp_app_not_available", "server_error", "bank_cutoff_in_progress",
                             "issuer_technical_error", "vpa_resolution_failed"],
    "temporary_lockout":    ["otp_attempts_exceeded", "pin_attempts_exceeded"],
    "limit_exceeded":       ["transaction_daily_limit_exceeded", "transaction_frequency_limit_exceeded",
                             "transaction_limit_exceeded", "credit_limit_exceeded"],
    "session_expiry":       ["payment_timed_out", "payment_session_expired",
                             "payment_collect_request_expired", "payment_cancelled", "otp_expired"],
    "customer_input_error": ["incorrect_otp", "incorrect_cvv", "card_number_invalid",
                             "invalid_vpa", "incorrect_card_expiry_date", "incorrect_pin"],
    "issuer_decline":       ["payment_failed", "card_declined", "debit_instrument_blocked",
                             "payment_risk_check_failed", "debit_declined", "user_not_eligible"],
    "instrument_invalid":   ["card_expired", "bank_account_invalid", "debit_instrument_inactive",
                             "user_not_registered_for_netbanking", "psp_not_registered"],
    "mandate_failure":      ["mandate_creation_failed", "mandate_creation_declined",
                             "mandate_creation_expired", "reqauth_mandate_not_acknowledged"],
}

# Structural limit codes: waiting never helps (the sub-rule from the taxonomy).
STRUCTURAL_LIMIT_REASONS = {"transaction_limit_exceeded", "credit_limit_exceeded"}

# Deliberate abandonment: re-pinging instantly reads as pestering.
DELIBERATE_ABANDON_REASONS = {"payment_cancelled"}

METHODS = ["card", "upi", "netbanking"]
METHOD_WEIGHTS = [0.45, 0.40, 0.15]
