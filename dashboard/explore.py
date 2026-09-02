"""Payment-slice filters for the batch view. Pure functions — no Streamlit."""

from __future__ import annotations

import pandas as pd

AMOUNT_BANDS = ("<₹1k", "₹1–5k", "₹5–25k", ">₹25k")
MANDATE_OPTIONS = ("Any", "Yes", "No")
OUTCOME_OPTIONS = ("Any", "Recovered", "Not recovered")

# Taxonomy order, so the multiselect does not jump around alphabetically.
CLASS_ORDER = (
    "technical_downtime",
    "temporary_lockout",
    "limit_exceeded",
    "insufficient_funds",
    "session_expiry",
    "customer_input_error",
    "mandate_failure",
    "issuer_decline",
    "instrument_invalid",
)


def _parse_bool(v) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def amount_band(amount: float) -> str:
    if amount < 1000:
        return "<₹1k"
    if amount < 5000:
        return "₹1–5k"
    if amount <= 25000:
        return "₹5–25k"
    return ">₹25k"


def class_options(df: pd.DataFrame) -> list[str]:
    present = set(df["failure_class"].tolist()) if len(df) else set()
    ordered = [c for c in CLASS_ORDER if c in present]
    extra = sorted(present - set(CLASS_ORDER))
    return ordered + extra


def build_treatment_frame(vis_rows: list[dict], payments: dict) -> pd.DataFrame:
    records = []
    for r in vis_rows:
        if str(r.get("arm", "")).strip().lower() != "treatment":
            continue
        pid = r["payment_id"]
        header = payments.get(pid, {})
        amount = float(r["amount"])
        recovered = bool(header.get("recovered"))
        recovered_at = header.get("recovered_at") if recovered else None
        records.append({
            "payment_id": pid,
            "amount": amount,
            "amount_band": amount_band(amount),
            "failed_at": pd.to_datetime(r["failed_at"]),
            "failure_class": r["failure_class"],
            "has_active_mandate": _parse_bool(r.get("has_active_mandate")),
            "error_reason": r.get("error_reason", ""),
            "recovered": recovered,
            "recovered_at": pd.to_datetime(recovered_at) if recovered_at else pd.NaT,
            "recovered_amount": amount if recovered else 0.0,
        })
    return pd.DataFrame.from_records(records)


def apply_filters(
    df: pd.DataFrame,
    classes: tuple[str, ...] | list[str],
    band: tuple[str, str],
    mandate: str,
    outcome: str,
) -> pd.DataFrame:
    """Filter in memory. Caller must pass a cached frame so the CSV is not re-read."""
    if df.empty:
        return df.copy()
    lo, hi = band
    i = AMOUNT_BANDS.index(lo)
    j = AMOUNT_BANDS.index(hi)
    if i > j:
        i, j = j, i
    allowed_bands = set(AMOUNT_BANDS[i : j + 1])
    out = df[df["failure_class"].isin(list(classes))]
    out = out[out["amount_band"].isin(allowed_bands)]
    if mandate == "Yes":
        out = out[out["has_active_mandate"]]
    elif mandate == "No":
        out = out[~out["has_active_mandate"]]
    if outcome == "Recovered":
        out = out[out["recovered"]]
    elif outcome == "Not recovered":
        out = out[~out["recovered"]]
    return out.copy()


def showing_label(n: int, total: int) -> str:
    return f"showing {n} of {total} payments"


def slice_summary(df: pd.DataFrame) -> dict:
    n = int(len(df))
    rec_n = int(df["recovered"].sum()) if n else 0
    at_risk = float(df["amount"].sum()) if n else 0.0
    recovered_rupees = float(df["recovered_amount"].sum()) if n else 0.0
    return {
        "n": n,
        "recovered_n": rec_n,
        "at_risk": at_risk,
        "recovered_rupees": recovered_rupees,
        "rate": rec_n / n if n else 0.0,
    }


def per_class_breakdown(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    rows = []
    for cls, g in df.groupby("failure_class", sort=False):
        n = int(len(g))
        rec_n = int(g["recovered"].sum())
        rows.append({
            "class": cls,
            "n": n,
            "recovered": rec_n,
            "rate": rec_n / n if n else 0.0,
            "at_risk": float(g["amount"].sum()),
        })
    rows.sort(key=lambda r: (-r["n"], r["class"]))
    return rows


def cumulative_trend(df: pd.DataFrame) -> pd.DataFrame:
    """Two series on a shared calendar axis.

    At-risk is booked on the failure date. Recovered is booked on the
    recovery date — the 14-day window means recoveries can land after
    the last failure.
    """
    empty = pd.DataFrame(columns=["at risk", "recovered"])
    if df.empty:
        return empty
    fail = df.groupby(df["failed_at"].dt.normalize())["amount"].sum()
    rec = df.loc[df["recovered"] & df["recovered_at"].notna()]
    rec_daily = (
        rec.groupby(rec["recovered_at"].dt.normalize())["recovered_amount"].sum()
        if len(rec) else pd.Series(dtype="float64")
    )
    start = fail.index.min()
    end = fail.index.max()
    if len(rec_daily):
        end = max(end, rec_daily.index.max())
    idx = pd.date_range(start, end, freq="D")
    out = pd.DataFrame(index=idx)
    out["at risk"] = fail.reindex(idx, fill_value=0.0).cumsum()
    out["recovered"] = rec_daily.reindex(idx, fill_value=0.0).cumsum()
    return out
