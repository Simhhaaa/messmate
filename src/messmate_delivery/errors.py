"""Safe delivery errors; messages intentionally omit request contents/secrets."""


class DeliveryError(RuntimeError):
    pass


class LiveSendingDisabled(DeliveryError):
    pass


class DeliveryLeaseLost(DeliveryError):
    pass


class WhatsAppSendError(DeliveryError):
    """Base exception carrying only sanitized API metadata."""

    def __init__(self, code: str, *, retryable: bool = False, retry_after_seconds: float | None = None) -> None:
        self.code = code[:80]
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"WhatsApp send failed ({self.code})")


class PermanentWhatsAppRejection(WhatsAppSendError):
    pass


class RetryableWhatsAppRejection(WhatsAppSendError):
    def __init__(self, code: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(code, retryable=True, retry_after_seconds=retry_after_seconds)


class UncertainWhatsAppSend(WhatsAppSendError):
    """Request outcome is ambiguous; sending again could duplicate the message."""
