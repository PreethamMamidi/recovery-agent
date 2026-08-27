"""
Map a Razorpay error_reason string to a failure class.

This is the same lookup a production webhook handler would do. Unknown
strings raise — a silent default is how a diagnosis layer rots.
"""

from generator.config import ERROR_REASONS


def _build_lookup() -> dict[str, str]:
    out: dict[str, str] = {}
    for class_id, reasons in ERROR_REASONS.items():
        for reason in reasons:
            if reason in out:
                raise ValueError(
                    f"duplicate error_reason {reason!r} in {out[reason]} and {class_id}")
            out[reason] = class_id
    return out


REASON_TO_CLASS = _build_lookup()


def diagnose(error_reason: str) -> str:
    if error_reason not in REASON_TO_CLASS:
        raise KeyError(f"unmapped error_reason: {error_reason!r}")
    return REASON_TO_CLASS[error_reason]


def assert_coverage() -> int:
    n = len(REASON_TO_CLASS)
    expected = sum(len(v) for v in ERROR_REASONS.values())
    if n != expected:
        raise AssertionError(f"lookup size {n} != listed reasons {expected}")
    return n


assert_coverage()
