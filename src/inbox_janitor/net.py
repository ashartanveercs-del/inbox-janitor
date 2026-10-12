"""The one place that talks HTTP. Adapters take an `HttpFn`, so tests pass a fake instead."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import NamedTuple, Protocol


class HttpResponse(NamedTuple):
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpFn(Protocol):
    def __call__(self, method: str, url: str, *, headers: Mapping[str, str],
                 body: bytes | None, timeout: float) -> HttpResponse: ...


def urllib_send(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                timeout: float) -> HttpResponse:
    """An HTTP error status is returned; a network failure raises OSError."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, _lower(response.headers), response.read())
    except urllib.error.HTTPError as err:
        return HttpResponse(err.code, _lower(err.headers or {}), err.read() or b"")
    except http.client.HTTPException as err:
        raise OSError(f"HTTP protocol error ({type(err).__name__})") from None


def _lower(headers) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(headers).items()}
