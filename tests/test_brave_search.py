from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone

import pytest

from dbzscc.app import brave_search
from dbzscc.app.brave_search import (
    BRAVE_API_HOST,
    BRAVE_API_KEY_ENV,
    BRAVE_API_URL,
    BraveSearchClient,
    BraveSearchHttpError,
    InvalidSearchRequestError,
    InvalidSearchResponseError,
    MissingApiKeyError,
    RateLimitedError,
    RedirectRejectedError,
    SearchTransportError,
    TransportResponse,
    _NoRedirectHandler,
    _validate_api_endpoint,
)

FAKE_KEY = "not-a-real-brave-token-for-tests"
FIXED_TIME = datetime(2026, 11, 3, 12, 45, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, response: TransportResponse | None = None) -> None:
        self.response = response or TransportResponse(200, {}, b'{"web":{"results":[]}}')
        self.calls: list[dict[str, object]] = []

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> TransportResponse:
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.response


def make_client(
    transport: FakeTransport,
    *,
    api_key: str | None = FAKE_KEY,
) -> BraveSearchClient:
    return BraveSearchClient(
        transport,
        api_key_provider=lambda: api_key,
        clock=lambda: FIXED_TIME,
    )


def test_search_sends_only_query_and_explicit_options_and_returns_provenance() -> None:
    payload = {
        "query": {"more_results_available": True},
        "web": {
            "results": [
                {
                    "title": "Brave docs",
                    "url": "https://search.brave.com/help",
                    "description": "A useful snippet.",
                },
                {"title": "Bad scheme", "url": "javascript:alert(1)"},
                {"title": "Credentials URL", "url": "https://user:pass@example.org/"},
                {"title": "Valid without snippet", "url": "https://example.org/"},
            ]
        },
    }
    transport = FakeTransport(TransportResponse(200, {"X-RateLimit-Remaining": "1, 99"}, json.dumps(payload).encode()))
    client = make_client(transport)

    result = client.search(
        "  Berlin public transit  ",
        count=4,
        offset=2,
        country="de",
        search_lang="DE",
        freshness="pw",
    )

    request = transport.calls[0]
    assert request["url"] == BRAVE_API_URL
    assert request["params"] == {
        "q": "Berlin public transit",
        "count": "4",
        "offset": "2",
        "country": "DE",
        "search_lang": "de",
        "freshness": "pw",
    }
    assert request["headers"] == {
        "Accept": "application/json",
        "X-Subscription-Token": FAKE_KEY,
    }
    assert request["timeout"] == 8.0
    assert result.query == "Berlin public transit"
    assert result.more_results_available is True
    assert result.offset == 2
    assert result.fetched_at == FIXED_TIME
    assert result.rate_limit.remaining == "1, 99"
    assert [(source.title, source.url, source.snippet, source.fetched_at) for source in result.results] == [
        ("Brave docs", "https://search.brave.com/help", "A useful snippet.", FIXED_TIME),
        ("Valid without snippet", "https://example.org/", "", FIXED_TIME),
    ]
    assert all(FAKE_KEY not in source.title + source.url + source.snippet for source in result.results)


def test_api_key_is_requested_each_time_and_not_retained_in_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {BRAVE_API_KEY_ENV: "fake-first-key"}
    monkeypatch.setattr(brave_search.os, "environ", environment)
    transport = FakeTransport()
    client = BraveSearchClient(
        transport,
        clock=lambda: FIXED_TIME,
    )

    client.search("first query")
    environment[BRAVE_API_KEY_ENV] = "fake-rotated-key"
    client.search("second query")

    assert transport.calls[0]["headers"]["X-Subscription-Token"] == "fake-first-key"
    assert transport.calls[1]["headers"]["X-Subscription-Token"] == "fake-rotated-key"
    assert "fake-rotated-key" not in repr(client.search("third query"))


def test_missing_or_blank_key_fails_before_transport() -> None:
    for missing_key in (None, "  "):
        transport = FakeTransport()
        client = make_client(transport, api_key=missing_key)
        with pytest.raises(MissingApiKeyError) as captured:
            client.search("some current information")
        assert BRAVE_API_KEY_ENV in str(captured.value)
        assert FAKE_KEY not in str(captured.value)
        assert transport.calls == []


def test_api_key_with_header_newline_is_rejected_without_request() -> None:
    transport = FakeTransport()
    client = make_client(transport, api_key="fake-key\nInjected: value")

    with pytest.raises(MissingApiKeyError):
        client.search("query")

    assert transport.calls == []


@pytest.mark.parametrize(
    ("query", "options"),
    [
        ("  ", {}),
        ("x" * 401, {}),
        ("query", {"count": 0}),
        ("query", {"count": 21}),
        ("query", {"count": True}),
        ("query", {"offset": -1}),
        ("query", {"offset": 10}),
        ("query", {"country": "USA"}),
        ("query", {"search_lang": "eng"}),
        ("query", {"freshness": "tomorrow"}),
        ("query", {"freshness": "2026-11-30to2026-11-01"}),
        ("query", {"freshness": "2026-02-30to2026-03-01"}),
    ],
)
def test_invalid_search_inputs_fail_before_transport(query: str, options: dict[str, object]) -> None:
    transport = FakeTransport()
    client = make_client(transport)

    with pytest.raises(InvalidSearchRequestError):
        client.search(query, **options)  # type: ignore[arg-type]

    assert transport.calls == []


def test_timeout_has_a_bounded_range() -> None:
    with pytest.raises(InvalidSearchRequestError):
        BraveSearchClient(timeout_seconds=0)
    with pytest.raises(InvalidSearchRequestError):
        BraveSearchClient(timeout_seconds=61)


def test_rate_limit_headers_are_typed_and_request_is_not_retried() -> None:
    transport = FakeTransport(
        TransportResponse(
            429,
            {
                "X-RateLimit-Limit": "1, 15000",
                "X-RateLimit-Policy": "1;w=1, 15000;w=2592000",
                "X-RateLimit-Remaining": "0, 1000",
                "X-RateLimit-Reset": "1, 1200",
            },
            b'{"error":"body is not exposed"}',
        )
    )
    client = make_client(transport)

    with pytest.raises(RateLimitedError) as captured:
        client.search("query")

    assert captured.value.status_code == 429
    assert captured.value.retry_after_seconds == 1.0
    assert captured.value.rate_limit.limit == "1, 15000"
    assert len(transport.calls) == 1
    assert "body is not exposed" not in str(captured.value)


def test_retry_after_header_takes_precedence_over_rate_limit_reset() -> None:
    transport = FakeTransport(
        TransportResponse(429, {"Retry-After": "3", "X-RateLimit-Reset": "1, 30"}, b"")
    )
    with pytest.raises(RateLimitedError) as captured:
        make_client(transport).search("query")
    assert captured.value.retry_after_seconds == 3.0


def test_other_http_errors_do_not_expose_response_body_or_api_key() -> None:
    transport = FakeTransport(TransportResponse(401, {}, FAKE_KEY.encode()))

    with pytest.raises(BraveSearchHttpError) as captured:
        make_client(transport).search("query")

    assert captured.value.status_code == 401
    assert FAKE_KEY not in str(captured.value)


def test_redirect_is_rejected_and_never_followed() -> None:
    transport = FakeTransport(TransportResponse(302, {"Location": "https://other.example/"}, b""))

    with pytest.raises(RedirectRejectedError) as captured:
        make_client(transport).search("query")

    assert captured.value.status_code == 302
    assert len(transport.calls) == 1
    assert _NoRedirectHandler().redirect_request(None, None, 302, "Found", {}, "https://other.example/") is None


def test_endpoint_allowlist_accepts_only_official_https_endpoint() -> None:
    _validate_api_endpoint(BRAVE_API_URL)
    _validate_api_endpoint(f"https://{BRAVE_API_HOST}:443/res/v1/web/search")

    for url in (
        "http://api.search.brave.com/res/v1/web/search",
        "https://api.search.brave.com.evil.example/res/v1/web/search",
        "https://other.example/res/v1/web/search",
        "https://user@api.search.brave.com/res/v1/web/search",
        "https://api.search.brave.com/res/v1/llm/context",
        "https://api.search.brave.com/res/v1/web/search?redirect=1",
    ):
        with pytest.raises(SearchTransportError):
            _validate_api_endpoint(url)


def test_malformed_json_and_result_shapes_are_typed_errors() -> None:
    for body in (b"not json", b"[]", b'{"web":{"results":"not-a-list"}}'):
        transport = FakeTransport(TransportResponse(200, {}, body))
        with pytest.raises(InvalidSearchResponseError):
            make_client(transport).search("query")


def test_oversized_response_is_rejected() -> None:
    from dbzscc.app.brave_search import MAX_RESPONSE_BYTES

    transport = FakeTransport(TransportResponse(200, {}, b" " * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(InvalidSearchResponseError, match="Größe"):
        make_client(transport).search("query")


def test_unexpected_transport_exception_is_redacted() -> None:
    class BrokenTransport:
        def get(self, **kwargs: object) -> TransportResponse:
            raise RuntimeError(f"synthetic failure containing {FAKE_KEY}")

    client = BraveSearchClient(
        BrokenTransport(),
        api_key_provider=lambda: FAKE_KEY,
        clock=lambda: FIXED_TIME,
    )

    with pytest.raises(SearchTransportError) as captured:
        client.search("query")

    assert FAKE_KEY not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
