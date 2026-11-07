"""Dry-run-first service with atomic claims and no ambiguous resend."""

from __future__ import annotations

from datetime import date
import hashlib
import logging
import time

from .adapters import WhatsAppAdapter
from .errors import (
    ChannelSendError, LiveSendingDisabled, PlanNotDeliverable,
    RetryableWhatsAppRejection, WhatsAppSendError,
)
from .formatter import render_plan_message
from .models import DeliveryIdentity, DeliveryOutcome, DeliveryStatus
from messmate_nutrition.optimizer import validate_optimization_result

logger = logging.getLogger(__name__)


class DietPlanDeliveryService:
    def __init__(self, store, adapter: WhatsAppAdapter | None = None, *, dry_run: bool = True,
                 live_send_enabled: bool = False, template_name: str = "messmate_diet_plan",
                 max_safe_retries: int = 2, sleep_fn=time.sleep, channel: str = "whatsapp") -> None:
        if max_safe_retries < 0:
            raise ValueError("max_safe_retries cannot be negative")
        channel = channel.strip().casefold()
        if channel not in {"telegram", "whatsapp"}:
            raise ValueError("delivery channel must be 'whatsapp' or 'telegram'")
        self.store = store
        self.adapter = adapter
        self.dry_run = dry_run
        self.live_send_enabled = live_send_enabled
        self.template_name = template_name
        self.max_safe_retries = max_safe_retries
        self.sleep_fn = sleep_fn
        self.channel = channel

    def deliver(self, recipient: str, plan_date: date, optimization_result) -> DeliveryOutcome:
        if validate_optimization_result(optimization_result):
            raise PlanNotDeliverable(
                "Optimization result failed complete-plan validation; no delivery was prepared."
            )
        message = render_plan_message(plan_date, optimization_result)
        identity = DeliveryIdentity.for_recipient(recipient, plan_date, channel=self.channel)
        payload_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        record = self.store.prepare(identity, payload_hash, self.template_name)
        if self.dry_run:
            return DeliveryOutcome(identity, record.status, message, True, note="Dry run only; no messaging request made.")
        if not self.live_send_enabled:
            raise LiveSendingDisabled("Live messaging is disabled; explicit enablement is required.")
        if self.adapter is None:
            raise ValueError("a messaging adapter is required for live delivery")
        self.store.recover_interrupted()
        lease = self.store.claim(identity)
        if lease is None:
            record = self.store.get(identity)
            return DeliveryOutcome(identity, record.status if record else DeliveryStatus.UNCERTAIN, message, False,
                                   note="Existing delivery is not claimable; no resend attempted.")
        try:
            for attempt in range(self.max_safe_retries + 1):
                try:
                    acceptance = self.adapter.send(recipient, message, self.template_name)
                    break
                except Exception as exc:
                    if isinstance(exc, ChannelSendError):
                        retryable = exc.retryable
                    else:
                        retryable = isinstance(exc, RetryableWhatsAppRejection)
                    if retryable and attempt < self.max_safe_retries:
                        requested_delay = exc.retry_after_seconds if isinstance(exc, ChannelSendError) or isinstance(exc, WhatsAppSendError) else None
                        delay = requested_delay if requested_delay is not None else 2 ** attempt
                        delay = max(delay, 0)
                        # Preserve the existing WhatsApp retry ceiling. Honor
                        # Telegram's explicit retry_after rather than retrying early.
                        if self.channel == "whatsapp":
                            delay = min(delay, 30)
                        self.sleep_fn(delay)
                        continue
                    raise
        except Exception as exc:
            if isinstance(exc, (ChannelSendError, WhatsAppSendError)):
                status = DeliveryStatus.UNCERTAIN if getattr(exc, "uncertain", False) else DeliveryStatus.FAILED
                finalized = self.store.finish(lease, status, error_code=exc.code)
                logger.warning("Message send ended channel=%s status=%s code=%s", self.channel, status.value, exc.code)
                if not finalized:
                    status = DeliveryStatus.UNCERTAIN
                return DeliveryOutcome(identity, status, message, False, sent=False, note=f"Send status: {status.value}.")
            self.store.finish(lease, DeliveryStatus.UNCERTAIN, error_code="unexpected_adapter_error")
            logger.error("Messaging adapter raised an unexpected error; state marked uncertain")
            return DeliveryOutcome(identity, DeliveryStatus.UNCERTAIN, message, False, note="Outcome uncertain; manual review required.")
        finalized = self.store.finish(lease, DeliveryStatus.ACCEPTED, provider_message_id=acceptance.provider_message_id)
        if not finalized:
            logger.warning("Provider acceptance could not be persisted; delivery remains uncertain")
            return DeliveryOutcome(identity, DeliveryStatus.UNCERTAIN, message, False, sent=True,
                                   provider_message_id=acceptance.provider_message_id,
                                   note="Provider accepted the request, but local state could not be finalized; manual review required.")
        logger.info("Messaging provider accepted delivery channel=%s", self.channel)
        return DeliveryOutcome(identity, DeliveryStatus.ACCEPTED, message, False, sent=True,
                               provider_message_id=acceptance.provider_message_id,
                               note="Accepted by messaging provider; not confirmed delivered.")


def service_from_environment(
    store, *, dry_run: bool = True, adapter: WhatsAppAdapter | None = None, channel: str | None = None
) -> DietPlanDeliveryService:
    """Build the selected channel service; absent explicit opt-in remains dry-run."""
    import os
    selected = (channel or os.getenv("MESSMATE_CHANNEL", "telegram")).strip().casefold()
    if selected not in {"telegram", "whatsapp"}:
        raise ValueError("delivery channel must be 'whatsapp' or 'telegram'")
    if selected == "telegram":
        enabled = os.getenv("MESSMATE_LIVE_SEND", "false").casefold() == "true"
        if adapter is None and not dry_run and enabled:
            from .telegram_adapter import TelegramBotAPIAdapter
            adapter = TelegramBotAPIAdapter.from_env()
        template_name = "telegram.sendMessage"
    else:
        # Preserve the legacy WhatsApp gate; it cannot enable Telegram sends.
        enabled = os.getenv("MESSMATE_WHATSAPP_LIVE_SEND_ENABLED", "false").casefold() == "true"
        if adapter is None and not dry_run and enabled:
            from .adapters import WhatsAppCloudAPIAdapter
            adapter = WhatsAppCloudAPIAdapter.from_env()
        template_name = os.getenv("WHATSAPP_TEMPLATE_NAME", "messmate_diet_plan")
    return DietPlanDeliveryService(store, adapter, dry_run=dry_run, live_send_enabled=enabled,
                                   template_name=template_name, channel=selected)
