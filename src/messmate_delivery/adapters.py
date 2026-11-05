"""Mock and isolated WhatsApp Cloud API adapters."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import PermanentWhatsAppRejection, RetryableWhatsAppRejection, UncertainWhatsAppSend


@dataclass(frozen=True)
class AdapterAcceptance:
    provider_message_id: str


class WhatsAppAdapter(Protocol):
    def send(self, recipient: str, message: str, template_name: str) -> AdapterAcceptance: ...


class MockWhatsAppAdapter:
    def __init__(self, provider_message_id: str = "wamid.mock") -> None:
        self.provider_message_id = provider_message_id
        self.calls: list[tuple[str, str, str]] = []

    def send(self, recipient: str, message: str, template_name: str) -> AdapterAcceptance:
        self.calls.append((recipient, message, template_name))
        return AdapterAcceptance(self.provider_message_id)


class WhatsAppCloudAPIAdapter:
    """Real adapter. Uses an approved WhatsApp template with one BODY variable."""

    def __init__(self, *, access_token: str, phone_number_id: str, template_name: str,
                 graph_version: str, language_code: str = "en", timeout: float = 20) -> None:
        if not all((access_token, phone_number_id, template_name)):
            raise ValueError("Cloud API credentials and template are required")
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._template_name = template_name
        self._language = language_code
        self._version = graph_version
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "WhatsAppCloudAPIAdapter":
        return cls(access_token=os.environ["WHATSAPP_ACCESS_TOKEN"],
                   phone_number_id=os.environ["WHATSAPP_PHONE_NUMBER_ID"],
                   template_name=os.environ["WHATSAPP_TEMPLATE_NAME"],
                   language_code=os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en"),
                   graph_version=os.environ["WHATSAPP_GRAPH_VERSION"])

    def send(self, recipient: str, message: str, template_name: str) -> AdapterAcceptance:
        body = {"messaging_product": "whatsapp", "to": recipient, "type": "template", "template": {
            "name": template_name or self._template_name, "language": {"code": self._language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": message}]}]}}
        url = f"https://graph.facebook.com/{self._version}/{self._phone_number_id}/messages"
        req = Request(url, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # 429 and documented throttling are definite rejections; 5xx may
            # have accepted the request before failing, so mark uncertain.
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                raise RetryableWhatsAppRejection("http_429", float(retry_after) if retry_after else None) from None
            if exc.code >= 500:
                raise UncertainWhatsAppSend("http_5xx") from None
            raise PermanentWhatsAppRejection(f"http_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise UncertainWhatsAppSend("transport_ambiguous") from None
        except (ValueError, UnicodeDecodeError):
            raise UncertainWhatsAppSend("invalid_provider_response") from None
        messages = payload.get("messages") if isinstance(payload, dict) else None
        message_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise UncertainWhatsAppSend("missing_provider_message_id")
        return AdapterAcceptance(message_id)
