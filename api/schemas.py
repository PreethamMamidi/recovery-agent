"""Response shapes. Webhook request body is not parsed through these — signature
verification needs the raw bytes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DecisionOut(BaseModel):
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    gate: str
    reason: str


class WebhookResult(BaseModel):
    status: str
    event_id: str | None = None
    internal_payment_id: str | None = None
    failure_class: str | None = None
    actions: list[DecisionOut] = Field(default_factory=list)


class PaymentChain(BaseModel):
    payment_id: str
    decisions: list[dict[str, Any]]
