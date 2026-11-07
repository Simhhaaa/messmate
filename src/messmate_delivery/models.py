"""Delivery state and identity models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
import hashlib
import re


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
    def for_recipient(
        cls, recipient: str, plan_date: date, message_kind: str = "diet_plan", *, channel: str = "whatsapp"
    ) -> "DeliveryIdentity":
        channel = channel.strip().casefold()
        if channel == "whatsapp":
            # Keep the original normalization and key derivation so existing
            # WhatsApp deliveries remain idempotent across this change.
            normalized = "".join(char for char in recipient if char.isdigit())
            if not normalized:
                raise ValueError("recipient must contain a phone number")
            identity_kind = message_kind
            recipient_hash_input = normalized
        elif channel == "telegram":
            normalized = recipient.strip()
            if normalized.startswith("@"):
                if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", normalized):
                    raise ValueError("Telegram chat ID must be a numeric ID or @username")
                normalized = normalized.casefold()
            else:
                try:
                    normalized = str(int(normalized))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Telegram chat ID must be a numeric ID or @username") from exc
            identity_kind = f"telegram:{message_kind}"
            recipient_hash_input = f"telegram:{normalized}"
        else:
            raise ValueError("delivery channel must be 'whatsapp' or 'telegram'")
        recipient_key = hashlib.sha256(recipient_hash_input.encode("utf-8")).hexdigest()
        raw_key = f"{recipient_key}:{plan_date.isoformat()}:{identity_kind}"
        delivery_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return cls(delivery_key, recipient_key, plan_date, identity_kind)


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
