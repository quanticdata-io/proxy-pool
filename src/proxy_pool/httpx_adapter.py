"""httpx clients that rotate through a :class:`ProxyPool`.

httpx binds a proxy to a client, not to a request, so these wrappers keep one
underlying ``httpx.Client`` / ``httpx.AsyncClient`` per proxy (created on first
use, closed with the wrapper). Needs ``pip install "proxy-pool[httpx]"``
(``httpx[socks]`` for SOCKS proxies).
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from .bans import BanDetector
from .pool import Outcome, ProxyPool
from .proxy import Proxy

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})

_RETRYABLE = (httpx.TransportError,)


class _Base:
    def __init__(
        self,
        pool: ProxyPool,
        *,
        detector: BanDetector | None = None,
        max_attempts: int = 3,
        retry_methods: frozenset[str] = IDEMPOTENT_METHODS,
        **client_kwargs: Any,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if "proxy" in client_kwargs or "proxies" in client_kwargs or "mounts" in client_kwargs:
            raise ValueError("the pool chooses the proxy: do not pass proxy=, proxies= or mounts=")
        self.pool = pool
        self.detector = detector or BanDetector()
        self.max_attempts = max_attempts
        self.retry_methods = frozenset(m.upper() for m in retry_methods)
        self.client_kwargs = client_kwargs

    def _attempts(self, method: str) -> int:
        return self.max_attempts if method.upper() in self.retry_methods else 1

    def _classify(self, response: httpx.Response) -> Outcome:
        if BanDetector.is_proxy_error(response.status_code):
            return Outcome.ERROR
        body = response.content if self.detector.markers else None
        if self.detector.is_ban(response.status_code, body):
            return Outcome.BAN
        return Outcome.SUCCESS


class PooledClient(_Base):
    """Synchronous client. Extra keyword arguments go to every ``httpx.Client``."""

    def __init__(self, pool: ProxyPool, **kwargs: Any) -> None:
        super().__init__(pool, **kwargs)
        self._clients: dict[Proxy, httpx.Client] = {}

    def _client(self, proxy: Proxy) -> httpx.Client:
        client = self._clients.get(proxy)
        if client is None:
            client = httpx.Client(proxy=proxy.url, **self.client_kwargs)
            self._clients[proxy] = client
        return client

    def request(
        self, method: str, url: str, *, session_key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        attempts = self._attempts(method)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            proxy = self.pool.acquire(session_key)
            try:
                response = self._client(proxy).request(method, url, **kwargs)
            except _RETRYABLE as exc:
                self.pool.report(proxy, Outcome.ERROR)
                last_error = exc
                continue
            outcome = self._classify(response)
            self.pool.report(proxy, outcome)
            if outcome is Outcome.SUCCESS or attempt == attempts:
                return response
            response.close()
        assert last_error is not None
        raise last_error

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self) -> PooledClient:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


class AsyncPooledClient(_Base):
    """Asynchronous client. Extra keyword arguments go to every ``httpx.AsyncClient``."""

    def __init__(self, pool: ProxyPool, **kwargs: Any) -> None:
        super().__init__(pool, **kwargs)
        self._clients: dict[Proxy, httpx.AsyncClient] = {}

    def _client(self, proxy: Proxy) -> httpx.AsyncClient:
        client = self._clients.get(proxy)
        if client is None:
            client = httpx.AsyncClient(proxy=proxy.url, **self.client_kwargs)
            self._clients[proxy] = client
        return client

    async def request(
        self, method: str, url: str, *, session_key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        attempts = self._attempts(method)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            proxy = self.pool.acquire(session_key)
            try:
                response = await self._client(proxy).request(method, url, **kwargs)
            except _RETRYABLE as exc:
                self.pool.report(proxy, Outcome.ERROR)
                last_error = exc
                continue
            outcome = self._classify(response)
            self.pool.report(proxy, outcome)
            if outcome is Outcome.SUCCESS or attempt == attempts:
                return response
            await response.aclose()
        assert last_error is not None
        raise last_error

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> AsyncPooledClient:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()
