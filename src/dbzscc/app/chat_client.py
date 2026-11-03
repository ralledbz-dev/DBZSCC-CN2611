"""OpenAI-compatible HTTP adapter; it performs no implicit network requests."""

from __future__ import annotations

import ipaddress
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
    urlopen,
)

from .config import AppSettings


class ChatProviderError(RuntimeError):
    """A safe, user-facing provider communication or response error."""


class ChatStreamCancelled(RuntimeError):
    """The caller explicitly cancelled an in-flight chat stream."""


def is_loopback_endpoint(endpoint: str) -> bool:
    """Return whether a validated HTTP(S) endpoint targets a loopback address."""
    try:
        normalized = AppSettings(model_endpoint=endpoint).validated_endpoint()
        host = urlsplit(normalized).hostname
        if not host:
            return False
        if host.casefold() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent a local context request from being redirected to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def _open_url(request: Request, timeout: float, *, local_only: bool):
    if not local_only:
        return urlopen(request, timeout=timeout)
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    return opener.open(request, timeout=timeout)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("Unsupported chat message role.")
        if not isinstance(self.content, str):
            raise TypeError("Chat message content must be text.")


class OpenAICompatibleClient:
    """Minimal adapter for llama.cpp server and compatible chat endpoints.

    A request is only made when an explicit method is called.
    Optional credentials are read from ``DBZSCC_LLM_API_KEY`` and are sent only
    over HTTPS or to a loopback endpoint. The key is never persisted or logged.
    """

    def __init__(self, endpoint: str, *, timeout_seconds: float = 90.0) -> None:
        settings = AppSettings(model_endpoint=endpoint)
        self.endpoint = settings.validated_endpoint()
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self.timeout_seconds = timeout_seconds

    def list_models(self) -> tuple[str, ...]:
        payload = self._request("GET", f"{self.endpoint}/models")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ChatProviderError("Die Runtime lieferte keine gültige Modellliste.")
        models = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        return tuple(models)

    def complete(self, model: str, messages: tuple[ChatMessage, ...]) -> str:
        selected_model = model.strip()
        if not selected_model:
            raise ValueError("Bitte zuerst ein Modell auswählen oder eintragen.")
        if not messages:
            raise ValueError("Mindestens eine Chat-Nachricht ist erforderlich.")
        payload = self._request(
            "POST",
            f"{self.endpoint}/chat/completions",
            {
                "model": selected_model,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "stream": False,
            },
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatProviderError(
                "Die Runtime-Antwort enthält keine Chat-Nachricht."
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ChatProviderError("Die Runtime lieferte eine leere Antwort.")
        return content.strip()

    def complete_local(self, model: str, messages: tuple[ChatMessage, ...]) -> str:
        """Complete one request with redirects and proxies disabled on loopback."""
        if not is_loopback_endpoint(self.endpoint):
            raise ChatProviderError(
                "Lokale Workflow-Anfragen sind nur an einen Loopback-Endpunkt erlaubt."
            )
        selected_model = model.strip()
        if not selected_model:
            raise ValueError("Bitte zuerst ein Modell auswählen oder eintragen.")
        if not messages:
            raise ValueError("Mindestens eine Chat-Nachricht ist erforderlich.")
        payload = self._request(
            "POST",
            f"{self.endpoint}/chat/completions",
            {
                "model": selected_model,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "stream": False,
            },
            local_only=True,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatProviderError(
                "Die Runtime-Antwort enthält keine Chat-Nachricht."
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ChatProviderError("Die Runtime lieferte eine leere Antwort.")
        return content.strip()

    def stream_complete(
        self,
        model: str,
        messages: tuple[ChatMessage, ...],
        on_delta: Callable[[str], None],
        cancel_event: threading.Event,
        *,
        local_only: bool = False,
    ) -> str:
        """Stream one text completion, returning only after the SSE [DONE] marker."""
        if local_only and not is_loopback_endpoint(self.endpoint):
            raise ChatProviderError(
                "Projektdateien dürfen ausschließlich an einen Loopbackserver gesendet werden."
            )
        selected_model = model.strip()
        if not selected_model:
            raise ValueError("Bitte zuerst ein Modell auswählen oder eintragen.")
        if not messages:
            raise ValueError("Mindestens eine Chat-Nachricht ist erforderlich.")
        body = {
            "model": selected_model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": True,
        }
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        api_key = os.environ.get("DBZSCC_LLM_API_KEY")
        if api_key and self._may_send_api_key():
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        parts: list[str] = []
        pending_data: list[str] = []
        completed = False
        try:
            with _open_url(
                request, timeout=self.timeout_seconds, local_only=local_only
            ) as response:
                while True:
                    if cancel_event.is_set():
                        raise ChatStreamCancelled("Chat-Stream abgebrochen.")
                    raw_line = response.readline()
                    if cancel_event.is_set():
                        raise ChatStreamCancelled("Chat-Stream abgebrochen.")
                    if not raw_line:
                        break
                    try:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError as exc:
                        raise ChatProviderError(
                            "Die Runtime lieferte ungültiges UTF-8 im Chat-Stream."
                        ) from exc
                    if not line:
                        if not pending_data:
                            continue
                        event_data = "\n".join(pending_data)
                        pending_data.clear()
                        if event_data.strip() == "[DONE]":
                            completed = True
                            break
                        self._consume_stream_event(event_data, parts, on_delta)
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if separator and field == "data":
                        pending_data.append(value.removeprefix(" "))
            if cancel_event.is_set():
                raise ChatStreamCancelled("Chat-Stream abgebrochen.")
            if not completed:
                raise ChatProviderError(
                    "Der Chat-Stream endete vor dem Abschlussmarker [DONE]."
                )
        except ChatStreamCancelled:
            raise
        except HTTPError as exc:
            if cancel_event.is_set():
                raise ChatStreamCancelled("Chat-Stream abgebrochen.") from exc
            raise ChatProviderError(
                f"Die Runtime antwortete mit HTTP {exc.code}."
            ) from exc
        except (HTTPException, URLError, TimeoutError, OSError) as exc:
            if cancel_event.is_set():
                raise ChatStreamCancelled("Chat-Stream abgebrochen.") from exc
            raise ChatProviderError(
                "Die Streaming-Anfrage ist fehlgeschlagen oder hat das Zeitlimit überschritten."
            ) from exc

        answer = "".join(parts).strip()
        if not answer:
            raise ChatProviderError("Die Runtime lieferte eine leere Antwort.")
        return answer

    @staticmethod
    def _consume_stream_event(
        event_data: str,
        parts: list[str],
        on_delta: Callable[[str], None],
    ) -> None:
        try:
            payload = json.loads(event_data)
        except json.JSONDecodeError as exc:
            raise ChatProviderError("Die Runtime lieferte ein ungültiges Stream-Ereignis.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            raise ChatProviderError("Das Chat-Stream-Ereignis hat ein ungültiges Format.")
        choices = payload["choices"]
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if delta is None:
            return
        if not isinstance(delta, dict):
            raise ChatProviderError("Das Chat-Stream-Ereignis hat ein ungültiges Format.")
        content = delta.get("content")
        if content is None:
            return
        if not isinstance(content, str):
            raise ChatProviderError("Die Runtime lieferte einen nicht-textuellen Chat-Delta.")
        if content:
            parts.append(content)
            on_delta(content)

    def _request(
        self,
        method: str,
        url: str,
        body: dict | None = None,
        *,
        local_only: bool = False,
    ) -> dict:
        if local_only:
            request_parts = urlsplit(url)
            endpoint_parts = urlsplit(self.endpoint)
            request_origin = (
                request_parts.scheme,
                request_parts.hostname,
                request_parts.port,
            )
            endpoint_origin = (
                endpoint_parts.scheme,
                endpoint_parts.hostname,
                endpoint_parts.port,
            )
            if not is_loopback_endpoint(self.endpoint) or request_origin != endpoint_origin:
                raise ChatProviderError(
                    "Lokale Workflow-Anfragen sind nur an denselben Loopback-Endpunkt erlaubt."
                )
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        api_key = os.environ.get("DBZSCC_LLM_API_KEY")
        if api_key and self._may_send_api_key():
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with _open_url(
                request, timeout=self.timeout_seconds, local_only=local_only
            ) as response:
                response_bytes = response.read()
        except HTTPError as exc:
            raise ChatProviderError(
                f"Die Runtime antwortete mit HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ChatProviderError(
                "Die Runtime ist nicht erreichbar. Prüfe Adresse und Dienststatus."
            ) from exc
        try:
            decoded = response_bytes.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChatProviderError("Die Runtime lieferte kein gültiges JSON.") from exc
        if not isinstance(payload, dict):
            raise ChatProviderError("Die Runtime lieferte eine unerwartete Antwort.")
        return payload

    def _may_send_api_key(self) -> bool:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.endpoint)
        host = (parsed.hostname or "").casefold()
        return parsed.scheme == "https" or host in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
