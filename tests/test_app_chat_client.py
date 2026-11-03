from __future__ import annotations

import json
import threading
from urllib.error import HTTPError

import pytest

import dbzscc.app.chat_client as chat_client_module
from dbzscc.app.chat_client import (
    ChatMessage,
    ChatProviderError,
    OpenAICompatibleClient,
    is_loopback_endpoint,
)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_model_listing_and_chat_completion_use_openai_compatible_contract(monkeypatch) -> None:
    monkeypatch.setenv("DBZSCC_LLM_API_KEY", "test-local-key")
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "authorization": request.get_header("Authorization"),
                "timeout": timeout,
                "body": json.loads(request.data) if request.data else None,
            }
        )
        if request.full_url.endswith("/models"):
            response = {"data": [{"id": "local-model"}]}
        else:
            response = {"choices": [{"message": {"content": "  Antwort  "}}]}
        return _Response(json.dumps(response).encode("utf-8"))

    monkeypatch.setattr(chat_client_module, "urlopen", fake_urlopen)
    client = OpenAICompatibleClient("http://127.0.0.1:8080/v1")

    assert client.list_models() == ("local-model",)
    answer = client.complete(
        "local-model",
        (ChatMessage("user", "Hallo"), ChatMessage("assistant", "Guten Tag")),
    )

    assert answer == "Antwort"
    assert [call["method"] for call in calls] == ["GET", "POST"]
    assert all(call["authorization"] == "Bearer test-local-key" for call in calls)
    assert calls[1]["body"]["messages"] == [
        {"role": "user", "content": "Hallo"},
        {"role": "assistant", "content": "Guten Tag"},
    ]
    assert calls[1]["body"]["stream"] is False


def test_credentials_are_not_sent_to_plain_http_remote_hosts(monkeypatch) -> None:
    monkeypatch.setenv("DBZSCC_LLM_API_KEY", "must-not-leak")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response(b"{}")

    monkeypatch.setattr(chat_client_module, "urlopen", fake_urlopen)
    client = OpenAICompatibleClient("http://example.invalid/v1")
    client._request("GET", "http://example.invalid/v1/models")

    assert captured["authorization"] is None
    assert captured["timeout"] == client.timeout_seconds


def test_rejects_credentials_and_unsupported_endpoint_parts() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleClient("http://user:password@localhost:8080/v1")
    with pytest.raises(ValueError):
        OpenAICompatibleClient("https://example.com/v1?token=secret")
    with pytest.raises(ValueError):
        OpenAICompatibleClient("file:///tmp/model")


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:8080/v1", True),
        ("http://[::1]:8080/v1", True),
        ("http://localhost:8080/v1", True),
        ("http://example.invalid/v1", False),
        ("http://localhost.attacker.invalid/v1", False),
        ("http://127.0.0.1.attacker.invalid/v1", False),
        ("http://0.0.0.0:8080/v1", False),
        ("file:///tmp/model", False),
    ],
)
def test_loopback_endpoint_allowlist(endpoint: str, expected: bool) -> None:
    assert is_loopback_endpoint(endpoint) is expected


def test_local_only_stream_rejects_remote_endpoint_before_opening(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a remote context request must not open a connection")

    monkeypatch.setattr(chat_client_module, "urlopen", forbidden)
    monkeypatch.setattr(chat_client_module, "build_opener", forbidden)

    with pytest.raises(ChatProviderError, match="Loopbackserver"):
        OpenAICompatibleClient("https://example.invalid/v1").stream_complete(
            "model",
            (ChatMessage("user", "question plus private files"),),
            lambda _text: None,
            threading.Event(),
            local_only=True,
        )


def test_local_only_stream_disables_proxies_and_redirects(monkeypatch) -> None:
    captured = {}
    chunks = _stream_bytes(
        'data: {"choices":[{"delta":{"content":"lokal"}}]}\n\n',
        "data: [DONE]\n\n",
    )

    class FakeOpener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return _ChunkedStreamResponse(chunks)

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener(handlers)

    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("local-only requests must use the no-proxy opener")

    monkeypatch.setattr(chat_client_module, "build_opener", fake_build_opener)
    monkeypatch.setattr(chat_client_module, "urlopen", forbidden_urlopen)

    answer = OpenAICompatibleClient("http://127.0.0.1:8080/v1").stream_complete(
        "local-model",
        (ChatMessage("user", "question plus private files"),),
        lambda _text: None,
        threading.Event(),
        local_only=True,
    )

    assert answer == "lokal"
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert captured["timeout"] == 90.0
    proxy_handlers = [
        handler
        for handler in captured["handlers"]
        if isinstance(handler, chat_client_module.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(
        isinstance(handler, chat_client_module._NoRedirectHandler)
        for handler in captured["handlers"]
    )
    assert chat_client_module._NoRedirectHandler().redirect_request(None, None, None, None) is None


def test_local_completion_disables_proxies_and_redirects(monkeypatch) -> None:
    captured = {}

    class FakeOpener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data)
            return _Response(
                json.dumps({"choices": [{"message": {"content": "  geplant  "}}]}).encode()
            )

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener(handlers)

    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("local-only requests must use the no-proxy opener")

    monkeypatch.setattr(chat_client_module, "build_opener", fake_build_opener)
    monkeypatch.setattr(chat_client_module, "urlopen", forbidden_urlopen)
    client = OpenAICompatibleClient("http://127.0.0.1:8080/v1", timeout_seconds=12.5)

    answer = client.complete_local("local-model", (ChatMessage("user", "Plan"),))

    assert answer == "geplant"
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert captured["timeout"] == 12.5
    assert captured["body"]["stream"] is False
    assert any(
        isinstance(handler, chat_client_module.ProxyHandler) and handler.proxies == {}
        for handler in captured["handlers"]
    )
    assert any(
        isinstance(handler, chat_client_module._NoRedirectHandler)
        for handler in captured["handlers"]
    )


def test_local_completion_rejects_remote_endpoint_before_opening(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a remote workflow request must not open a connection")

    monkeypatch.setattr(chat_client_module, "urlopen", forbidden)
    monkeypatch.setattr(chat_client_module, "build_opener", forbidden)

    with pytest.raises(ChatProviderError, match="Loopback-Endpunkt"):
        OpenAICompatibleClient("https://example.invalid/v1").complete_local(
            "model", (ChatMessage("user", "request"),)
        )


def test_provider_errors_do_not_expose_response_body_or_credentials(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise HTTPError("http://localhost/v1/models", 401, "unauthorized", {}, None)

    monkeypatch.setattr(chat_client_module, "urlopen", fail)
    client = OpenAICompatibleClient("http://localhost:8080/v1")

    with pytest.raises(ChatProviderError, match="HTTP 401") as error:
        client.list_models()
    assert "unauthorized" not in str(error.value)


class _ChunkedStreamResponse:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = iter(chunks)
        self._buffer = b""
        self._eof = False

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def readline(self) -> bytes:
        while b"\n" not in self._buffer and not self._eof:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                self._eof = True
        if b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line + b"\n"
        line, self._buffer = self._buffer, b""
        return line


def _stream_bytes(*events: str) -> tuple[bytes, ...]:
    wire = "".join(events).encode("utf-8")
    return tuple(wire[index : index + 7] for index in range(0, len(wire), 7))


def test_stream_complete_uses_sse_contract_and_emits_fake_chunk_deltas(monkeypatch) -> None:
    chunks = _stream_bytes(
        ": keep-alive\r\n\r\n",
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\r\n\r\n',
        'data: {"choices":[{"delta":{"content":"Hallo"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" Welt 🌍"}}]}\n\n',
        "data: [DONE]\n\n",
    )
    captured = {}

    def fake_urlopen(request, timeout):
        captured["method"] = request.get_method()
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        return _ChunkedStreamResponse(chunks)

    monkeypatch.setattr(chat_client_module, "urlopen", fake_urlopen)
    client = OpenAICompatibleClient("http://127.0.0.1:8080/v1", timeout_seconds=12.5)
    deltas: list[str] = []

    answer = client.stream_complete(
        "local-model",
        (ChatMessage("user", "Hallo"),),
        deltas.append,
        threading.Event(),
    )

    assert answer == "Hallo Welt 🌍"
    assert deltas == ["Hallo", " Welt 🌍"]
    assert captured == {
        "method": "POST",
        "accept": "text/event-stream",
        "timeout": 12.5,
        "body": {
            "model": "local-model",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": True,
        },
    }


def test_stream_complete_stops_without_returning_partial_content_on_cancel(monkeypatch) -> None:
    chunks = _stream_bytes(
        'data: {"choices":[{"delta":{"content":"Teilantwort"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" zu spät"}}]}\n\n',
        "data: [DONE]\n\n",
    )
    monkeypatch.setattr(chat_client_module, "urlopen", lambda *args, **kwargs: _ChunkedStreamResponse(chunks))
    cancel_event = threading.Event()
    deltas: list[str] = []

    def cancel_after_first_delta(text: str) -> None:
        deltas.append(text)
        cancel_event.set()

    with pytest.raises(chat_client_module.ChatStreamCancelled):
        OpenAICompatibleClient("http://127.0.0.1:8080/v1").stream_complete(
            "local-model", (ChatMessage("user", "Hallo"),), cancel_after_first_delta, cancel_event
        )

    assert deltas == ["Teilantwort"]


def test_stream_complete_rejects_partial_http_error_and_empty_answer(monkeypatch) -> None:
    def fail_with_http_error(*args, **kwargs):
        raise HTTPError("http://localhost/v1/chat/completions", 503, "unavailable", {}, None)

    monkeypatch.setattr(chat_client_module, "urlopen", fail_with_http_error)
    client = OpenAICompatibleClient("http://localhost:8080/v1")
    with pytest.raises(ChatProviderError, match="HTTP 503"):
        client.stream_complete(
            "local-model",
            (ChatMessage("user", "Hallo"),),
            lambda _text: None,
            threading.Event(),
        )

    partial_chunks = _stream_bytes(
        'data: {"choices":[{"delta":{"content":"unvollständig"}}]}\n\n'
    )
    monkeypatch.setattr(
        chat_client_module,
        "urlopen",
        lambda *args, **kwargs: _ChunkedStreamResponse(partial_chunks),
    )
    with pytest.raises(ChatProviderError, match="\\[DONE\\]"):
        client.stream_complete(
            "local-model",
            (ChatMessage("user", "Hallo"),),
            lambda _text: None,
            threading.Event(),
        )

    empty_chunks = _stream_bytes("data: [DONE]\n\n")
    monkeypatch.setattr(
        chat_client_module,
        "urlopen",
        lambda *args, **kwargs: _ChunkedStreamResponse(empty_chunks),
    )
    with pytest.raises(ChatProviderError, match="leere Antwort"):
        client.stream_complete(
            "local-model",
            (ChatMessage("user", "Hallo"),),
            lambda _text: None,
            threading.Event(),
        )


def test_stream_complete_reports_timeout_without_exposing_transport_details(monkeypatch) -> None:
    captured = {}

    def timeout(request, timeout):
        captured["timeout"] = timeout
        raise TimeoutError("private socket detail")

    monkeypatch.setattr(chat_client_module, "urlopen", timeout)
    client = OpenAICompatibleClient("http://127.0.0.1:8080/v1", timeout_seconds=4.0)

    with pytest.raises(ChatProviderError, match="Zeitlimit") as error:
        client.stream_complete(
            "local-model",
            (ChatMessage("user", "Hallo"),),
            lambda _text: None,
            threading.Event(),
        )

    assert captured["timeout"] == 4.0
    assert "private socket detail" not in str(error.value)
