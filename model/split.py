"""Customer-level split. A row split would leak the same person across the cut."""

from __future__ import annotations

import random


def split_customers(
    customer_ids: list[str],
    train_frac: float = 0.8,
    seed: int = 0,
) -> tuple[set[str], set[str]]:
    ids = sorted(set(customer_ids))
    rng = random.Random(seed)
    rng.shuffle(ids)
    n = int(len(ids) * train_frac)
    return set(ids[:n]), set(ids[n:])
