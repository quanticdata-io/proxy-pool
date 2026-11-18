"""A ``requests.Session`` that takes a new proxy from the pool for each try.

Needs ``pip install "proxy-pool[requests]"`` (and ``requests[socks]`` for SOCKS proxies).
"""

from __future__ import annotations

from typing import Any

import requests

from .bans import BanDetector
from .pool import Outcome, ProxyPool

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})


class PooledSession(requests.Session):
    """``requests.Session`` whose requests go through proxies from a :class:`ProxyPool`.

    Each try takes a proxy from the pool. A connection error or a ban (see
    :class:`BanDetector`) is reported to the pool and the request is sent again
    through another proxy, up to ``max_attempts`` tries in total. The last
    response is returned even if it is a ban, so the caller can inspect it.

    Only idempotent methods are retried by default: a POST is sent once unless
    ``retry_methods`` includes it.

    Pass ``session_key="..."`` to a request (or ``sticky_key`` to the session)
    to keep the same proxy for that key, for example one key per logged-in
    account.
    """

    def __init__(
        self,
        pool: ProxyPool,
        *,
        detector: BanDetector | None = None,
        max_attempts: int = 3,
        sticky_key: str | None = None,
        retry_methods: frozenset[str] = IDEMPOTENT_METHODS,
    ) -> None:
        super().__init__()
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.pool = pool
        self.detector = detector or BanDetector()
        self.max_attempts = max_attempts
        self.sticky_key = sticky_key
        self.retry_methods = frozenset(m.upper() for m in retry_methods)

    def request(
        self, method: str | bytes, url: str | bytes, *args: Any, session_key: str | None = None, **kwargs: Any
    ) -> requests.Response:
        verb = method.decode() if isinstance(method, bytes) else method
        attempts = self.max_attempts if verb.upper() in self.retry_methods else 1
        key = session_key if session_key is not None else self.sticky_key
        if kwargs.get("proxies"):
            raise ValueError("PooledSession chooses the proxy: do not pass proxies=")
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            proxy = self.pool.acquire(key)
            kwargs["proxies"] = {"http": proxy.url, "https": proxy.url}
            try:
                response = super().request(method, url, *args, **kwargs)
            except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:  # fmt: skip
                self.pool.report(proxy, Outcome.ERROR)
                last_error = exc
                continue
            if BanDetector.is_proxy_error(response.status_code):
                self.pool.report(proxy, Outcome.ERROR)
                if attempt < attempts:
                    response.close()
                    continue
                return response
            body = response.content if self.detector.markers else None
            if self.detector.is_ban(response.status_code, body):
                self.pool.report(proxy, Outcome.BAN)
                if attempt < attempts:
                    response.close()
                    continue
                return response
            self.pool.report(proxy, Outcome.SUCCESS)
            return response
        assert last_error is not None
        raise last_error

    def get(self, url: str | bytes, *, session_key: str | None = None, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("allow_redirects", True)
        return self.request("GET", url, session_key=session_key, **kwargs)

    def head(self, url: str | bytes, *, session_key: str | None = None, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("allow_redirects", False)
        return self.request("HEAD", url, session_key=session_key, **kwargs)

    def post(
        self,
        url: str | bytes,
        data: Any = None,
        json: Any = None,
        *,
        session_key: str | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        return self.request("POST", url, data=data, json=json, session_key=session_key, **kwargs)
