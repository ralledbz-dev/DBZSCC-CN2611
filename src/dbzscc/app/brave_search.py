"""Qt-free, explicitly invoked Brave Web Search client.

Only the query and explicitly supplied search options are sent to Brave. This
module never reads conversation history, project files, or application state.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

BRAVE_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_API_HOST = "api.search.brave.com"
MAX_RESULT_COUNT = 20
MAX_OFFSET = 9
MAX_QUERY_LENGTH = 400
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 8.0


class BraveSearchError(Exception):
    """Base class for safe, user-presentable Brave Search failures."""


class InvalidSearchRequestError(BraveSearchError, ValueError):
    """The query or a search option is outside the supported bounds."""


class MissingApiKeyError(BraveSearchError):
    """The required API key is missing or blank."""


class SearchTransportError(BraveSearchError):
    """A network or transport failure occurred; details are intentionally redacted."""


class InvalidSearchResponseError(BraveSearchError):
    """Brave returned an oversized, invalid, or unexpected response."""


@dataclass(frozen=True)
class RateLimitInfo:
    """Rate-limit headers returned by Brave, preserved as non-secret text."""

    limit: str | None = None
    policy: str | None = None
    remaining: str | None = None
    reset: str | None = None


class BraveSearchHttpError(BraveSearchError):
    """A non-success HTTP status; response bodies are not exposed."""

    def __init__(
        self,
        status_code: int,
        *,
        rate_limit: RateLimitInfo,
    ) -> None:
        self.status_code = status_code
        self.rate_limit = rate_limit
        super().__init__(f"Brave Search API meldete HTTP {status_code}.")


class RateLimitedError(BraveSearchHttpError):
    """Brave rejected the request because a rate or quota limit was reached."""

    def __init__(
        self,
        *,
        rate_limit: RateLimitInfo,
        retry_after_seconds: float | None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(429, rate_limit=rate_limit)


class RedirectRejectedError(BraveSearchHttpError):
    """The API endpoint attempted a redirect, which this client never follows."""


@dataclass(frozen=True)
class BraveSearchSource:
    """A search result and the time at which the result set was received."""

    title: str
    url: str
    snippet: str
    fetched_at: datetime


@dataclass(frozen=True)
class BraveSearchResponse:
    """Typed result set with query provenance and pagination availability."""

    query: str
    results: tuple[BraveSearchSource, ...]
    fetched_at: datetime
    more_results_available: bool
    offset: int
    rate_limit: RateLimitInfo


@dataclass(frozen=True)
class TransportResponse:
    """Small transport-neutral HTTP response used by the client and fake tests."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class BraveTransport(Protocol):
    """Minimal injectable GET transport; tests provide an in-memory fake."""

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> TransportResponse:
        """Perform one request and return a bounded response."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects instead of forwarding a credential-bearing request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _UrllibTransport:
    """Direct HTTPS transport with no environment proxy and no redirect handling."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def get(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> TransportResponse:
        _validate_api_endpoint(url)
        query = urllib.parse.urlencode(params)
        request_url = f"{url}?{query}"
        request = urllib.request.Request(
            request_url,
            headers=dict(headers),
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {
                    str(name): str(value) for name, value in response.headers.items()
                }
                return TransportResponse(
                    status_code=int(response.status),
                    headers=response_headers,
                    body=body,
                )
        except urllib.error.HTTPError as exc:
            try:
                response_headers = {
                    str(name): str(value) for name, value in (exc.headers or {}).items()
                }
                return TransportResponse(
                    status_code=int(exc.code),
                    headers=response_headers,
                    body=b"",
                )
            finally:
                exc.close()
        # An injected transport can include the credential in arbitrary exception text.
        except Exception:  # noqa: BLE001
            # Do not surface URL, headers, proxy values, or low-level exception text.
            raise SearchTransportError("Die Verbindung zur Brave-Suche ist fehlgeschlagen.") from None


class BraveSearchClient:
    """Search Brave using only an explicit query and optional bounded filters.

    ``BRAVE_SEARCH_API_KEY`` is read at the time of each ``search`` call. The
    key is sent only in the official subscription header and is never included
    in results, exceptions, or application logs.
    """

    def __init__(
        self,
        transport: BraveTransport | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        api_key_provider: Callable[[], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0.1 <= timeout_seconds <= 60.0
        ):
            raise InvalidSearchRequestError("Der Timeout muss zwischen 0,1 und 60 Sekunden liegen.")
        self._transport = transport if transport is not None else _UrllibTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._api_key_provider = api_key_provider or (lambda: os.environ.get(BRAVE_API_KEY_ENV))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def search(
        self,
        query: str,
        *,
        count: int = 10,
        offset: int = 0,
        country: str | None = None,
        search_lang: str | None = None,
        freshness: str | None = None,
    ) -> BraveSearchResponse:
        """Submit one query; no context or history is read or added automatically."""
        clean_query = _validate_query(query)
        clean_count = _validate_integer("count", count, minimum=1, maximum=MAX_RESULT_COUNT)
        clean_offset = _validate_integer("offset", offset, minimum=0, maximum=MAX_OFFSET)
        params: dict[str, str] = {
            "q": clean_query,
            "count": str(clean_count),
            "offset": str(clean_offset),
        }
        if country is not None:
            params["country"] = _validate_country(country)
        if search_lang is not None:
            params["search_lang"] = _validate_search_language(search_lang)
        if freshness is not None:
            params["freshness"] = _validate_freshness(freshness)

        api_key = self._api_key_provider()
        if not isinstance(api_key, str) or not api_key.strip():
            raise MissingApiKeyError(
                f"Der Brave-API-Schlüssel fehlt; bitte {BRAVE_API_KEY_ENV} setzen."
            )
        if "\r" in api_key or "\n" in api_key:
            raise MissingApiKeyError(f"{BRAVE_API_KEY_ENV} enthält keinen gültigen Schlüssel.")

        _validate_api_endpoint(BRAVE_API_URL)
        try:
            response = self._transport.get(
                url=BRAVE_API_URL,
                params=params,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
                timeout=self._timeout_seconds,
            )
        except BraveSearchError:
            raise
        # An injected transport's exception text may contain the API key.
        except Exception:  # noqa: BLE001
            raise SearchTransportError(
                "Die Verbindung zur Brave-Suche ist fehlgeschlagen."
            ) from None

        fetched_at = _utc_datetime(self._clock())
        rate_limit = _rate_limit_info(response.headers)
        if 300 <= response.status_code < 400:
            raise RedirectRejectedError(response.status_code, rate_limit=rate_limit)
        if response.status_code == 429:
            raise RateLimitedError(
                rate_limit=rate_limit,
                retry_after_seconds=_retry_after_seconds(response.headers, fetched_at),
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise BraveSearchHttpError(response.status_code, rate_limit=rate_limit)
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise InvalidSearchResponseError("Die Brave-Antwort überschreitet die erlaubte Größe.")

        payload = _decode_payload(response.body)
        web = payload.get("web")
        if not isinstance(web, dict) or not isinstance(web.get("results"), list):
            raise InvalidSearchResponseError("Die Brave-Antwort enthält keine gültige Web-Ergebnisliste.")
        sources = _parse_sources(web["results"], fetched_at, clean_count)
        query_info = payload.get("query")
        more_results = (
            query_info.get("more_results_available")
            if isinstance(query_info, dict)
            else False
        )
        if not isinstance(more_results, bool):
            more_results = False
        return BraveSearchResponse(
            query=clean_query,
            results=sources,
            fetched_at=fetched_at,
            more_results_available=more_results,
            offset=clean_offset,
            rate_limit=rate_limit,
        )


def _validate_api_endpoint(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise SearchTransportError("Die Brave-API-Adresse ist nicht zulässig.") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != BRAVE_API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/res/v1/web/search"
        or parsed.query
        or parsed.fragment
    ):
        raise SearchTransportError("Die Brave-API-Adresse ist nicht zulässig.")


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise InvalidSearchRequestError("Die Suchanfrage muss Text sein.")
    clean = query.strip()
    if not clean:
        raise InvalidSearchRequestError("Bitte eine Suchanfrage eingeben.")
    if len(clean) > MAX_QUERY_LENGTH:
        raise InvalidSearchRequestError(
            f"Die Suchanfrage darf höchstens {MAX_QUERY_LENGTH} Zeichen enthalten."
        )
    return clean


def _validate_integer(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidSearchRequestError(f"{name} muss zwischen {minimum} und {maximum} liegen.")
    return value


def _validate_country(country: str) -> str:
    if not isinstance(country, str) or re.fullmatch(r"[A-Za-z]{2}", country.strip()) is None:
        raise InvalidSearchRequestError("country muss ein zweistelliger Ländercode sein.")
    return country.strip().upper()


def _validate_search_language(language: str) -> str:
    if not isinstance(language, str) or re.fullmatch(r"[A-Za-z]{2}", language.strip()) is None:
        raise InvalidSearchRequestError("search_lang muss ein zweistelliger Sprachcode sein.")
    return language.strip().lower()


def _validate_freshness(freshness: str) -> str:
    if not isinstance(freshness, str):
        raise InvalidSearchRequestError("freshness ist kein unterstützter Zeitraum.")
    clean = freshness.strip()
    if clean in {"pd", "pw", "pm", "py"}:
        return clean
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})to(\d{4}-\d{2}-\d{2})", clean)
    if match is not None:
        try:
            start = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            end = datetime.strptime(match.group(2), "%Y-%m-%d").date()
        except ValueError:
            pass
        else:
            if start <= end:
                return clean
    raise InvalidSearchRequestError("freshness muss pd/pw/pm/py oder ein gültiger Datumsbereich sein.")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for header_name, value in headers.items():
        if str(header_name).lower() == target:
            return str(value)
    return None


def _rate_limit_info(headers: Mapping[str, str]) -> RateLimitInfo:
    return RateLimitInfo(
        limit=_header(headers, "X-RateLimit-Limit"),
        policy=_header(headers, "X-RateLimit-Policy"),
        remaining=_header(headers, "X-RateLimit-Remaining"),
        reset=_header(headers, "X-RateLimit-Reset"),
    )


def _retry_after_seconds(headers: Mapping[str, str], now: datetime) -> float | None:
    retry_after = _header(headers, "Retry-After")
    if retry_after:
        try:
            seconds = float(retry_after.strip())
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                retry_at = None
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - now).total_seconds()
            else:
                seconds = -1.0
        if math.isfinite(seconds) and seconds >= 0:
            return seconds

    reset = _header(headers, "X-RateLimit-Reset")
    if reset:
        first_window = reset.split(",", maxsplit=1)[0].strip()
        try:
            seconds = float(first_window)
        except ValueError:
            return None
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidSearchResponseError("Die lokale Suchzeit konnte nicht bestimmt werden.")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decode_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise InvalidSearchResponseError("Brave lieferte keine gültige JSON-Antwort.") from None
    if not isinstance(payload, dict):
        raise InvalidSearchResponseError("Die Brave-Antwort hat ein unerwartetes Format.")
    return payload


def _valid_source_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return url


def _parse_sources(
    values: list[object], fetched_at: datetime, count: int
) -> tuple[BraveSearchSource, ...]:
    sources: list[BraveSearchSource] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = _valid_source_url(item.get("url"))
        if not isinstance(title, str) or not title.strip() or url is None:
            continue
        snippet_value = item.get("description", "")
        snippet = snippet_value.strip() if isinstance(snippet_value, str) else ""
        sources.append(
            BraveSearchSource(
                title=title.strip(),
                url=url,
                snippet=snippet,
                fetched_at=fetched_at,
            )
        )
        if len(sources) >= count:
            break
    return tuple(sources)
