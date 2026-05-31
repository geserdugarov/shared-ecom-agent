from __future__ import annotations

from collections.abc import Iterable, Mapping

import httpx
from pyqwest import FullResponse, Headers


def _normalize_headers(
    headers: Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, Headers):
        return list(headers.items())
    if isinstance(headers, Mapping):
        return [(str(k), str(v)) for k, v in headers.items()]
    return [(str(k), str(v)) for k, v in headers]


def _to_pyqwest_headers(headers: httpx.Headers) -> Headers:
    return Headers(list(headers.multi_items()))


class _HttpxStreamResponse:
    def __init__(self, ctx: object) -> None:
        self._ctx = ctx
        self._resp: httpx.Response | None = None
        self.status: int = 0
        self.headers = Headers()
        self.content: Iterable[bytes] = iter(())
        self.trailers = Headers()

    def __enter__(self) -> _HttpxStreamResponse:
        resp = self._ctx.__enter__()
        self._resp = resp
        self.status = resp.status_code
        self.headers = _to_pyqwest_headers(resp.headers)
        self.content = resp.iter_bytes()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._ctx.__exit__(exc_type, exc, tb)


class HttpxSyncClient:
    """Sync HTTP client adapter with the subset used by connectrpc."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            http2=False,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        *,
        url: str,
        headers: Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        timeout: float | None = None,
    ) -> FullResponse:
        resp = self._client.get(
            url=url,
            headers=_normalize_headers(headers),
            timeout=timeout,
        )
        return FullResponse(
            status=resp.status_code,
            headers=_to_pyqwest_headers(resp.headers),
            content=resp.content,
            trailers=Headers(),
        )

    def post(
        self,
        *,
        url: str,
        headers: Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        content: bytes | Iterable[bytes] | None = None,
        timeout: float | None = None,
    ) -> FullResponse:
        resp = self._client.post(
            url=url,
            headers=_normalize_headers(headers),
            content=content,
            timeout=timeout,
        )
        return FullResponse(
            status=resp.status_code,
            headers=_to_pyqwest_headers(resp.headers),
            content=resp.content,
            trailers=Headers(),
        )

    def stream(
        self,
        *,
        method: str,
        url: str,
        headers: Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        content: bytes | Iterable[bytes] | None = None,
        timeout: float | None = None,
    ) -> _HttpxStreamResponse:
        ctx = self._client.stream(
            method=method,
            url=url,
            headers=_normalize_headers(headers),
            content=content,
            timeout=timeout,
        )
        return _HttpxStreamResponse(ctx)
