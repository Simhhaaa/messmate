"""Dry-run-first service with atomic claims and no ambiguous resend."""

from __future__ import annotations

from datetime import date
import hashlib
import logging
import time

from .adapters import WhatsAppAdapter
from .errors import LiveSendingDisabled
from .formatter import render_plan_message
from .models import DeliveryIdentity, DeliveryOutcome, DeliveryStatus

logger = logging.getLogger(__name__)


class DietPlanDeliveryService:
    def __init__(self, store, adapter: WhatsAppAdapter | None = None, *, dry_run: bool = True,
                 live_send_enabled: bool = False, template_name: str = "messmate_diet_plan",
                 max_safe_retries: int = 2, sleep_fn=time.sleep) -> None:
        if max_safe_retries < 0:
            raise ValueError("max_safe_retries cannot be negative")
        self.store = store
        self.adapter = adapter
        self.dry_run = dry_run
        self.live_send_enabled = live_send_enabled
        self.template_name = template_name
        self.max_safe_retries = max_safe_retries
        self.sleep_fn = sleep_fn

    def deliver(self, recipient: str, plan_date: date, optimization_result) -> DeliveryOutcome:
        message = render_plan_message(plan_date, optimization_result)
        identity = DeliveryIdentity.for_recipient(recipient, plan_date)
        payload_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        record = self.store.prepare(identity, payload_hash, self.template_name)
        if self.dry_run:
            return DeliveryOutcome(identity, record.status, message, True, note="Dry run only; no WhatsApp request made.")
        if not self.live_send_enabled:
            raise LiveSendingDisabled("Live WhatsApp sending is disabled; explicit enablement is required.")
        if self.adapter is None:
            raise ValueError("a WhatsApp adapter is required for live delivery")
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
                    from .errors import RetryableWhatsAppRejection, WhatsAppSendError
                    if isinstance(exc, RetryableWhatsAppRejection) and attempt < self.max_safe_retries:
                        delay = min(max(exc.retry_after_seconds or (2 ** attempt), 0), 30)
                        self.sleep_fn(delay)
                        continue
                    raise
        except Exception as exc:
            from .errors import UncertainWhatsAppSend, WhatsAppSendError
            if isinstance(exc, WhatsAppSendError):
                status = DeliveryStatus.UNCERTAIN if isinstance(exc, UncertainWhatsAppSend) else DeliveryStatus.FAILED
                finalized = self.store.finish(lease, status, error_code=exc.code)
                logger.warning("WhatsApp send ended with status=%s code=%s", status.value, exc.code)
                if not finalized:
                    status = DeliveryStatus.UNCERTAIN
                return DeliveryOutcome(identity, status, message, False, sent=False, note=f"Send status: {status.value}.")
            self.store.finish(lease, DeliveryStatus.UNCERTAIN, error_code="unexpected_adapter_error")
            logger.error("WhatsApp adapter raised an unexpected error; state marked uncertain")
            return DeliveryOutcome(identity, DeliveryStatus.UNCERTAIN, message, False, note="Outcome uncertain; manual review required.")
        finalized = self.store.finish(lease, DeliveryStatus.ACCEPTED, provider_message_id=acceptance.provider_message_id)
        if not finalized:
            logger.warning("Provider acceptance could not be persisted; delivery remains uncertain")
            return DeliveryOutcome(identity, DeliveryStatus.UNCERTAIN, message, False, sent=True,
                                   provider_message_id=acceptance.provider_message_id,
                                   note="Provider accepted the request, but local state could not be finalized; manual review required.")
        logger.info("WhatsApp provider accepted delivery")
        return DeliveryOutcome(identity, DeliveryStatus.ACCEPTED, message, False, sent=True,
                               provider_message_id=acceptance.provider_message_id, note="Accepted by Cloud API; not confirmed delivered.")


def service_from_environment(store, *, dry_run: bool = True, adapter: WhatsAppAdapter | None = None) -> DietPlanDeliveryService:
    """Live mode requires both a code-level opt-in and explicit env flag."""
    import os
    enabled = os.getenv("MESSMATE_WHATSAPP_LIVE_SEND_ENABLED", "false").casefold() == "true"
    if adapter is None and not dry_run and enabled:
        from .adapters import WhatsAppCloudAPIAdapter
        adapter = WhatsAppCloudAPIAdapter.from_env()
    return DietPlanDeliveryService(store, adapter, dry_run=dry_run, live_send_enabled=enabled,
                                   template_name=os.getenv("WHATSAPP_TEMPLATE_NAME", "messmate_diet_plan"))
