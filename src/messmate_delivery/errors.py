"""Safe delivery errors; messages intentionally omit request contents/secrets."""


class DeliveryError(RuntimeError):
    pass


class LiveSendingDisabled(DeliveryError):
    pass


class PlanNotDeliverable(DeliveryError):
    """The optimization result failed complete-plan safety validation."""

    def __init__(self, safe_reason: str = "plan_validation_failed") -> None:
        self.safe_reason = safe_reason
        super().__init__(safe_reason)


class DeliveryLeaseLost(DeliveryError):
    pass


class ChannelSendError(DeliveryError):
    """Sanitized channel error; never stores a body, URL, token, or recipient."""

    channel = "messaging"
    uncertain = False

    def __init__(self, code: str, *, retryable: bool = False, retry_after_seconds: float | None = None) -> None:
        self.code = code[:80]
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{self.channel} send failed ({self.code})")


class PermanentChannelRejection(ChannelSendError):
    """Provider definitely rejected a request; do not retry it automatically."""


class RetryableChannelRejection(ChannelSendError):
    """Provider definitely rejected a request and supplied retry guidance."""

    def __init__(self, code: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(code, retryable=True, retry_after_seconds=retry_after_seconds)


class UncertainChannelSend(ChannelSendError):
    """The provider may have accepted a request; resending could duplicate it."""

    uncertain = True


class WhatsAppSendError(DeliveryError):
    """Backward-compatible WhatsApp error carrying sanitized API metadata."""

    channel = "WhatsApp"
    uncertain = False

    def __init__(self, code: str, *, retryable: bool = False, retry_after_seconds: float | None = None) -> None:
        self.code = code[:80]
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{self.channel} send failed ({self.code})")


class PermanentWhatsAppRejection(WhatsAppSendError):
    pass


class RetryableWhatsAppRejection(WhatsAppSendError):
    def __init__(self, code: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(code, retryable=True, retry_after_seconds=retry_after_seconds)


class UncertainWhatsAppSend(WhatsAppSendError):
    """Request outcome is ambiguous; sending again could duplicate the message."""

    uncertain = True
