"""Telegram Bot API adapter for the shared delivery interface."""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .adapters import AdapterAcceptance
from .errors import PermanentChannelRejection, RetryableChannelRejection, UncertainChannelSend

_MAX_TEXT_CHARACTERS = 4096
_MAX_ERROR_BODY_BYTES = 65536
_CHAT_ID_PATTERN = re.compile(r"(?:-?\d+|@[A-Za-z0-9_]{5,32})\Z")


class TelegramBotAPIAdapter:
    """Send plain-text messages through Telegram Bot API ``sendMessage``.

    It structurally implements the existing ``WhatsAppAdapter`` protocol. The
    template argument is accepted for interface compatibility and unused.
    """

    def __init__(self, *, bot_token: str, timeout: float = 20) -> None:
        if not bot_token or not bot_token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._bot_token = bot_token.strip()
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "TelegramBotAPIAdapter":
        """Load the bot token from the process environment, never a file."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        return cls(bot_token=token)

    def send(self, recipient: str, message: str, template_name: str) -> AdapterAcceptance:
        del template_name  # Telegram sends text directly; no template is involved.
        chat_id = recipient.strip()
        if not _CHAT_ID_PATTERN.fullmatch(chat_id):
            raise ValueError("TELEGRAM_CHAT_ID must be a numeric ID or @username")
        if not isinstance(message, str) or not 1 <= len(message) <= _MAX_TEXT_CHARACTERS:
            raise ValueError("Telegram message text must contain 1–4096 characters")

        # The Bot API requires its token in the HTTPS path. Never log this URL,
        # attach request objects to errors, or include provider response text.
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        request = Request(
            url,
            data=json.dumps({"chat_id": chat_id, "text": message}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise UncertainChannelSend("telegram_invalid_provider_response")
            if payload.get("ok") is not True:
                _raise_provider_error(_safe_error_code(payload), payload)
            result = payload.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                raise UncertainChannelSend("telegram_missing_message_id")
            return AdapterAcceptance(str(message_id))
        except HTTPError as exc:
            try:
                payload = _read_error_payload(exc)
                _raise_provider_error(exc.code, payload)
            except (PermanentChannelRejection, RetryableChannelRejection, UncertainChannelSend):
                raise
            except Exception:
                raise UncertainChannelSend("telegram_provider_response_ambiguous") from None
        except (PermanentChannelRejection, RetryableChannelRejection, UncertainChannelSend):
            raise
        except (URLError, TimeoutError, OSError):
            raise UncertainChannelSend("telegram_transport_ambiguous") from None
        except (ValueError, UnicodeDecodeError):
            raise UncertainChannelSend("telegram_invalid_provider_response") from None
        except Exception:
            # urllib/http.client has additional failure types (for example
            # IncompleteRead) that may carry response bytes or request details.
            raise UncertainChannelSend("telegram_transport_ambiguous") from None


def _read_error_payload(error: HTTPError) -> dict[str, Any]:
    try:
        raw = error.read(_MAX_ERROR_BODY_BYTES)
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        # Error bodies are diagnostic input only; parsing failures must not
        # expose raw provider exceptions or change HTTP status classification.
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_error_code(payload: dict[str, Any]) -> int:
    code = payload.get("error_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else 0


def _retry_after(payload: dict[str, Any]) -> float | None:
    parameters = payload.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _raise_provider_error(http_status: int, payload: dict[str, Any]) -> None:
    provider_code = _safe_error_code(payload)
    if http_status == 429 or provider_code == 429:
        raise RetryableChannelRejection("telegram_http_429", _retry_after(payload)) from None
    if http_status >= 500 or provider_code >= 500:
        raise UncertainChannelSend("telegram_http_5xx") from None
    status = http_status if 400 <= http_status < 500 else provider_code
    raise PermanentChannelRejection(f"telegram_http_{status or 'rejected'}") from None
