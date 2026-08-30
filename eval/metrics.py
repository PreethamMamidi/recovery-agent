"""Shared scoring for baselines and the agent. Hidden files stay in eval."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from config.costs import intervention_cost
from generator.config import load_failure_classes
from simulator.response import (
    Outcome,
    latents_from_row,
    payment_hidden_from_row,
    payment_visible_from_row,
    respond,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def resolve_data(data: Path | str = DATA) -> Path:
    """Batch directory. Relative paths are from the repo root, not cwd."""
    path = Path(data)
    return path if path.is_absolute() else ROOT / path


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def index_rows(rows: list[dict], key: str) -> dict[str, dict]:
    return {r[key]: r for r in rows}


def parse_bool(v) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def load_world(data: Path | str = DATA) -> dict:
    # Always the canonical taxonomy: policy and max_attempts must not silently
    # pick up a robustness CSV. Calibrated/sensitivity p_resolves is already
    # baked into this batch's ground_truth.csv at generate time.
    data = resolve_data(data)
    classes = load_failure_classes()
    pay_vis = read_csv(data / "payments_visible.csv")
    return {
        "classes": classes,
        "pay_vis": pay_vis,
        "pay_hid": index_rows(read_csv(data / "payments_hidden.csv"), "payment_id"),
        "customers": index_rows(read_csv(data / "customers_visible.csv"), "customer_id"),
        "latents": index_rows(read_csv(data / "customers_latent.csv"), "customer_id"),
        "truth": index_rows(read_csv(data / "ground_truth.csv"), "payment_id"),
    }


@dataclass
class RowScore:
    payment_id: str
    failure_class: str
    arm: str
    amount: int
    recovered: bool
    natural: bool
    debit_attempts: int
    wasted_debits: int
    impossible_debits: int
    messages: int
    messages_sms: int
    messages_whatsapp: int
    messages_email: int
    opted_out_triggered: bool
    recovered_rupees: float
    cost: float
    net_value: float


@dataclass
class PolicyTotals:
    name: str
    n: int = 0
    recovered: int = 0
    natural: int = 0
    debit_attempts: int = 0
    wasted_debits: int = 0
    impossible_debits: int = 0
    messages: int = 0
    messages_sms: int = 0
    messages_whatsapp: int = 0
    messages_email: int = 0
    opted_out_triggered: int = 0
    recovered_rupees: float = 0.0
    cost: float = 0.0
    net_value: float = 0.0
    by_class: dict = field(default_factory=dict)

    @property
    def recovery_rate(self) -> float:
        return self.recovered / self.n if self.n else 0.0

    @property
    def natural_rate(self) -> float:
        return self.natural / self.n if self.n else 0.0

    @property
    def messages_per_recovery(self) -> float:
        return self.messages / self.recovered if self.recovered else 0.0


def _empty_class() -> dict:
    return {
        "n": 0, "recovered": 0, "natural": 0,
        "wasted": 0, "impossible": 0, "messages": 0, "opt_outs": 0,
        "net_value": 0.0,
    }


def score_outcome(vis, customer: dict, truth: dict, out: Outcome) -> RowScore:
    natural = parse_bool(truth.get("would_have_recovered_naturally", False))
    ltv = float(customer.get("lifetime_value", 0) or 0)
    cost = intervention_cost(
        out.debit_attempts,
        out.messages_sms, out.messages_whatsapp, out.messages_email,
        ltv, out.opted_out_triggered,
    )
    recovered_rupees = float(vis.amount) if out.recovered else 0.0
    return RowScore(
        payment_id=vis.payment_id,
        failure_class=vis.failure_class,
        arm=vis.arm,
        amount=vis.amount,
        recovered=out.recovered,
        natural=natural,
        debit_attempts=out.debit_attempts,
        wasted_debits=out.wasted_debits,
        impossible_debits=out.impossible_debits,
        messages=out.messages_total,
        messages_sms=out.messages_sms,
        messages_whatsapp=out.messages_whatsapp,
        messages_email=out.messages_email,
        opted_out_triggered=out.opted_out_triggered,
        recovered_rupees=recovered_rupees,
        cost=cost,
        net_value=recovered_rupees - cost,
    )


def add_row(totals: PolicyTotals, row: RowScore) -> None:
    totals.n += 1
    totals.recovered += int(row.recovered)
    totals.natural += int(row.natural)
    totals.debit_attempts += row.debit_attempts
    totals.wasted_debits += row.wasted_debits
    totals.impossible_debits += row.impossible_debits
    totals.messages += row.messages
    totals.messages_sms += row.messages_sms
    totals.messages_whatsapp += row.messages_whatsapp
    totals.messages_email += row.messages_email
    totals.opted_out_triggered += int(row.opted_out_triggered)
    totals.recovered_rupees += row.recovered_rupees
    totals.cost += row.cost
    totals.net_value += row.net_value
    bucket = totals.by_class.setdefault(row.failure_class, _empty_class())
    bucket["n"] += 1
    bucket["recovered"] += int(row.recovered)
    bucket["natural"] += int(row.natural)
    bucket["wasted"] += row.wasted_debits
    bucket["impossible"] += row.impossible_debits
    bucket["messages"] += row.messages
    bucket["opt_outs"] += int(row.opted_out_triggered)
    bucket["net_value"] += row.net_value


def run_schedule(world: dict, schedule_fn, name: str) -> PolicyTotals:
    """Run a baseline schedule on the treatment arm."""
    totals = PolicyTotals(name=name)
    classes = world["classes"]
    for row in world["pay_vis"]:
        vis = payment_visible_from_row(row)
        if vis.arm != "treatment":
            continue
        hid = payment_hidden_from_row(world["pay_hid"][vis.payment_id])
        cust = world["customers"][vis.customer_id]
        lat = latents_from_row(world["latents"][vis.customer_id])
        fc = classes[vis.failure_class]
        gt = world["truth"][vis.payment_id]
        opted = parse_bool(cust["opted_out"])
        out = respond(vis, hid, lat, fc, gt, schedule_fn(vis), opted_out=opted)
        add_row(totals, score_outcome(vis, cust, gt, out))
    return totals


def identity_mismatches(world: dict) -> int:
    n = 0
    classes = world["classes"]
    for row in world["pay_vis"]:
        vis = payment_visible_from_row(row)
        hid = payment_hidden_from_row(world["pay_hid"][vis.payment_id])
        cust = world["customers"][vis.customer_id]
        lat = latents_from_row(world["latents"][vis.customer_id])
        fc = classes[vis.failure_class]
        gt = world["truth"][vis.payment_id]
        out = respond(vis, hid, lat, fc, gt, actions=[],
                      opted_out=parse_bool(cust["opted_out"]))
        raw = parse_bool(gt.get("would_have_recovered_naturally", False))
        raw_date = (gt.get("natural_recovery_date") or "").strip()
        if out.recovered != raw or (out.recovered_at or "") != raw_date:
            n += 1
    return n


def control_totals(world: dict) -> PolicyTotals:
    totals = PolicyTotals(name="control")
    classes = world["classes"]
    for row in world["pay_vis"]:
        vis = payment_visible_from_row(row)
        if vis.arm != "control":
            continue
        hid = payment_hidden_from_row(world["pay_hid"][vis.payment_id])
        cust = world["customers"][vis.customer_id]
        lat = latents_from_row(world["latents"][vis.customer_id])
        fc = classes[vis.failure_class]
        gt = world["truth"][vis.payment_id]
        out = respond(vis, hid, lat, fc, gt, actions=[],
                      opted_out=parse_bool(cust["opted_out"]))
        add_row(totals, score_outcome(vis, cust, gt, out))
    return totals


def rate(n: int, d: int) -> float:
    return n / d if d else 0.0
