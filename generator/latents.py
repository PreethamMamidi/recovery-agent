"""
HIDDEN GROUND TRUTH. The agent must NEVER import this module.

These are facts about customers that no merchant can observe in production:
salary dates, willingness to pay, irritation thresholds. They exist so the
synthetic data behaves like reality - outcomes driven by mechanism rather than
by a flat probability per class.

The agent sees only PROXIES of these (tenure, past payment history, timing
patterns) and must infer the rest. That is what makes it a real test: the
simulator rewards being RIGHT ABOUT A HIDDEN FACT, not using a strategy the
author happened to like.

If you ever find yourself importing this file from agent/ or from a model,
stop. That is the leakage failure mode: it scores beautifully in simulation
and is worthless on real data.
"""

from dataclasses import dataclass, asdict
import random


@dataclass
class CustomerLatents:
    customer_id: str

    # When money actually arrives. Drives insufficient_funds resolution.
    salary_day: int              # day of month, 1-28

    # Does this person want to pay at all? Separate from whether they CAN.
    true_intent_to_pay: float    # 0-1

    # Will they bother re-trying unprompted? This is the second factor in
    # base_recovery_prob: P(resolves) x P(re-attempts). P(resolves) is a
    # property of the failure class; this is a property of the person.
    reattempt_propensity: float  # 0-1

    # How many contacts before they disengage. Without this, the optimal
    # policy is "message everyone forever" and stopping rules are decorative.
    annoyance_threshold: int     # 2-5

    # Per-channel pickup rates.
    resp_sms: float
    resp_whatsapp: float
    resp_email: float

    # Affects completion of instrument-update / re-auth flows.
    tech_savviness: float        # 0-1

    def responsiveness(self, channel: str) -> float:
        return {"sms": self.resp_sms,
                "whatsapp": self.resp_whatsapp,
                "email": self.resp_email}.get(channel, 0.1)

    def as_row(self) -> dict:
        return asdict(self)


def make_latents(customer_id: str, rng: random.Random) -> CustomerLatents:
    # Salary days cluster at month start but not exclusively - a fixed "1st"
    # for everyone would make the agent's payday heuristic trivially correct.
    salary_day = rng.choices(
        population=[1, 2, 3, 5, 7, 10, 15, 25],
        weights=[0.30, 0.12, 0.08, 0.10, 0.15, 0.08, 0.10, 0.07],
    )[0]

    intent = min(1.0, max(0.0, rng.betavariate(5, 2)))       # skewed high
    reattempt = min(1.0, max(0.0, rng.betavariate(2, 3)))    # skewed low

    return CustomerLatents(
        customer_id=customer_id,
        salary_day=salary_day,
        true_intent_to_pay=round(intent, 3),
        reattempt_propensity=round(reattempt, 3),
        annoyance_threshold=rng.choices([2, 3, 4, 5], [0.20, 0.40, 0.30, 0.10])[0],
        resp_sms=round(rng.uniform(0.10, 0.45), 3),
        resp_whatsapp=round(rng.uniform(0.25, 0.70), 3),
        resp_email=round(rng.uniform(0.03, 0.20), 3),
        tech_savviness=round(rng.betavariate(3, 2), 3),
    )
