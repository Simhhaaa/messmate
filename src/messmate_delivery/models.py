"""Delivery state and identity models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
import hashlib


class DeliveryStatus(StrEnum):
    PREPARED = "prepared"
    SENDING = "sending"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class DeliveryIdentity:
    delivery_key: str
    recipient_key: str
    plan_date: date
    message_kind: str = "diet_plan"

    @classmethod
    def for_recipient(cls, recipient: str, plan_date: date, message_kind: str = "diet_plan") -> "DeliveryIdentity":
        normalized = "".join(char for char in recipient if char.isdigit())
        if not normalized:
            raise ValueError("recipient must contain a phone number")
        recipient_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        raw_key = f"{recipient_key}:{plan_date.isoformat()}:{message_kind}"
        delivery_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return cls(delivery_key, recipient_key, plan_date, message_kind)


@dataclass(frozen=True)
class DeliveryLease:
    delivery_key: str
    lease_token: str
    attempt_count: int


@dataclass(frozen=True)
class DeliveryRecord:
    identity: DeliveryIdentity
    status: DeliveryStatus
    payload_hash: str
    template_name: str
    attempt_count: int
    provider_message_id: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True)
class DeliveryOutcome:
    identity: DeliveryIdentity
    status: DeliveryStatus
    message: str
    dry_run: bool
    sent: bool = False
    provider_message_id: str | None = None
    note: str = ""
