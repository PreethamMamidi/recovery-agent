"""Fixed economics. Set before seeing results; do not tune after.

Units are INR. These are stated costs for net-value comparison, not
production invoices.
"""

DEBIT_COST = 2.0
MESSAGE_COST = {
    "sms": 0.20,
    "whatsapp": 1.0,
    "email": 0.05,
}
OPT_OUT_LTV_FRACTION = 0.30


def message_cost(sms: int = 0, whatsapp: int = 0, email: int = 0) -> float:
    return (sms * MESSAGE_COST["sms"]
            + whatsapp * MESSAGE_COST["whatsapp"]
            + email * MESSAGE_COST["email"])


def opt_out_cost(lifetime_value: float, triggered: bool) -> float:
    if not triggered:
        return 0.0
    return float(lifetime_value) * OPT_OUT_LTV_FRACTION


def intervention_cost(debit_attempts: int, sms: int, whatsapp: int, email: int,
                      lifetime_value: float, opted_out_triggered: bool) -> float:
    return (DEBIT_COST * debit_attempts
            + message_cost(sms, whatsapp, email)
            + opt_out_cost(lifetime_value, opted_out_triggered))
